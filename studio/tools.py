"""Tool nyata yang dieksekusi para Super Bot (7 tool, satu pemilik per tool).

Semua tool mengembalikan **string** (AutoGen meneruskan hasilnya ke LLM) dan
selalu jujur soal apa yang benar-benar terjadi:

* ``research_current_trends`` memanggil YouTube Data API / Tavily / SerpAPI; tanpa
  kredensial ia mengatakan "RISET TIDAK TERSEDIA", bukan mengarang angka tren;
* render memakai pipeline ffmpeg/auto-editor/Whisper/Remotion bila
  ``STUDIO_REAL_RENDER=1``, kalau tidak hasilnya ditandai ``simulation`` di sidecar
  report — Damon bisa membedakannya dan menolak simulasi di mode produksi;
* ``request_and_check_creator_files`` bisa berjalan non-blocking
  (``STUDIO_ASYNC_GATE``) untuk mode daemon: Creator dikabari lewat Telegram;
* upload memakai YouTube Data API v3 asli bila ``client_secret.json`` tersedia,
  kalau tidak hanya menulis receipt simulasi.

Semua path divalidasi supaya bot tidak bisa menulis keluar dari folder studio.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from .config import Settings, env_flag
from .media import MediaError, MediaPipeline, detect_capabilities

SIMULATION_MARKER = b"STUDIO_SIMULATED_MP4"
TODO_FILENAME = "creator_todo_list.md"
RECEIPT_FILENAME = "youtube_publish_receipt.json"

# Nama tool harus UNIK per bot. AutoGen merutekan pesan `tool_calls` ke agen yang
# memiliki fungsi tersebut (func_call_filter); kalau dua bot berbagi nama fungsi
# yang sama, eksekusi bisa mendarat di bot yang salah.
TOOL_NAMES: tuple[str, ...] = (
    "research_current_trends",
    "save_content_brief",
    "save_production_script",
    "request_and_check_creator_files",
    "assemble_and_render_video",
    "inspect_rendered_output",
    "upload_to_youtube",
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _short(text: str, limit: int = 700) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _fmt_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


class StudioTools:
    """Kumpulan tool; satu instance dipakai bersama semua bot dalam satu run."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.paths = settings.paths.ensure()
        self._caps = None

    @property
    def capabilities(self):
        """Deteksi ffmpeg/auto-editor/whisper sekali per instance."""
        if self._caps is None:
            self._caps = detect_capabilities(self.settings)
        return self._caps

    def build_functions(self) -> dict[str, Callable[..., str]]:
        """Fungsi yang diekspos ke LLM, lengkap dengan schema ``Annotated``.

        AutoGen 0.2 menempelkan atribut ``_name``/``_description`` ke objek fungsi;
        itu gagal pada bound method, jadi tiap method dibungkus fungsi biasa
        (``functools.wraps`` menjaga signature & anotasi tetap terbaca AutoGen).
        """
        tools = self

        def save_content_brief(
            content: Annotated[
                str,
                "Isi lengkap content_brief.json (JSON valid: titles, thumbnails, angle, audience, keywords, promise)",
            ],
        ) -> str:
            """Simpan brief riset ke ./workspace/content_brief.json."""
            return tools.save_document("content_brief.json", content)

        def save_production_script(
            content: Annotated[
                str,
                "Isi lengkap script_production.md; setiap baris diawali [CREATOR A-ROLL] atau [AI B-ROLL]",
            ],
        ) -> str:
            """Simpan naskah final ke ./workspace/script_production.md."""
            return tools.save_document("script_production.md", content)

        functions: dict[str, Callable[..., str]] = {
            "save_content_brief": save_content_brief,
            "save_production_script": save_production_script,
        }
        for name in (
            "research_current_trends",
            "request_and_check_creator_files",
            "assemble_and_render_video",
            "inspect_rendered_output",
            "upload_to_youtube",
        ):
            impl = getattr(self, name)

            @functools.wraps(impl)
            def wrapper(*args: Any, __impl: Callable[..., str] = impl, **kwargs: Any) -> str:
                return __impl(*args, **kwargs)

            functions[name] = wrapper
        return functions

    # ------------------------------------------------------------------ util
    def _safe_path(self, base: Path, filename: str) -> Path:
        raw = (filename or "").strip().strip("\"'").strip()
        if not raw:
            raise ValueError("Nama file kosong.")
        target = (base / raw).resolve()
        base_resolved = base.resolve()
        if target != base_resolved and base_resolved not in target.parents:
            raise ValueError(f"Nama file '{filename}' keluar dari folder {base_resolved.name}/.")
        return target

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.paths.root))
        except ValueError:
            return str(path)

    def _log(self, action: str, **payload: Any) -> None:
        record = {"ts": _now(), "action": action, **payload}
        try:
            with (self.paths.workspace / "studio_log.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging tidak boleh menggagalkan pipeline

    def _banner(self, title: str, lines: list[str]) -> None:
        print("\n" + "=" * 68, flush=True)
        print(f"🔔 {title}", flush=True)
        for line in lines:
            print(f"   {line}", flush=True)
        print("=" * 68, flush=True)

    # ------------------------------------------------------------ 1. dokumen
    def save_document(
        self,
        filename: Annotated[str, "Nama file di folder ./workspace/ (contoh: content_brief.json, script_production.md)"],
        content: Annotated[str, "Isi lengkap dokumen (Markdown atau JSON) yang akan disimpan"],
    ) -> str:
        try:
            target = self._safe_path(self.paths.workspace, filename)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not (content or "").strip():
            return f"ERROR: isi dokumen '{filename}' kosong, tidak ada yang disimpan."
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        size = target.stat().st_size
        self._log("save_document", file=self._rel(target), bytes=size)
        return (
            f"TERSIMPAN: ./{self._rel(target)} ({_fmt_bytes(size)}, {len(content.splitlines())} baris). "
            "Isi file sudah final di disk."
        )

    # -------------------------------------------- 1b. riset tren nyata (Vito)
    def research_current_trends(
        self,
        topic: Annotated[
            str, "Topik/niche yang ingin dicek trennya; kosongkan untuk tren YouTube hari ini"
        ] = "",
    ) -> str:
        from .research import fetch_trends

        result = fetch_trends(self.settings, topic)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        artifact = self.paths.workspace / f"trend_research_{stamp}.json"
        artifact.write_text(
            json.dumps(result.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._log(
            "research",
            provider=result.provider,
            items=len(result.items),
            real=result.real,
            file=self._rel(artifact),
        )

        if not result.real:
            return (
                "RISET TIDAK TERSEDIA (jangan mengarang angka tren): "
                f"{result.warning} Data mentah: ./{self._rel(artifact)}. "
                "Tulis brief dari pengalaman dan tandai bagian tren sebagai 'tanpa data real-time'."
            )
        return (
            f"TREN NYATA (provider={result.provider}, diambil {result.fetched_at}, "
            f"{len(result.items)} hasil):\n{result.as_text()}\n\n"
            f"Data mentah: ./{self._rel(artifact)}\n"
            "Pakai judul/angle di atas sebagai acuan dan sebutkan angkanya apa adanya; "
            "jangan mengarang statistik lain."
        )

    # -------------------------------------------- 2. human-in-the-loop (Bima)
    def request_and_check_creator_files(
        self,
        required_files_info: Annotated[
            str, "Daftar aset/rekaman yang wajib dibuat Creator (manusia), boleh Markdown berisi checklist"
        ],
        expected_filename: Annotated[
            str, "Nama file yang ditunggu di folder ./input_creator/ (contoh: a_roll_raw.mp4)"
        ],
    ) -> str:
        try:
            target = self._safe_path(self.paths.input_creator, expected_filename)
        except ValueError as exc:
            return f"ERROR: {exc}"

        todo_path = self.paths.workspace / TODO_FILENAME
        todo_path.write_text(
            "# TUGAS CREATOR (HUMAN-IN-THE-LOOP)\n\n"
            f"_{_now()}_\n\n"
            f"{required_files_info.strip()}\n\n"
            "## Cara menyelesaikan\n\n"
            f"1. Rekam/ekspor file dengan nama **`{target.name}`**.\n"
            f"2. Taruh di folder `./input_creator/` (path lengkap: `{self._rel(target)}`).\n"
            "3. Kembali ke terminal dan tekan **Enter** (atau ketik `DONE`).\n\n"
            "Ketik `MOCK` di terminal untuk membuat file dummy dan meneruskan simulasi.\n",
            encoding="utf-8",
        )
        self._log("request_creator_files", file=self._rel(target))

        if self.settings.async_gate:
            if target.exists() and target.stat().st_size > 0:
                return (
                    f"STATUS UPLOADED: '{target.name}' sudah ada di ./input_creator/ "
                    f"({_fmt_bytes(target.stat().st_size)}). Pipeline produksi boleh dijalankan."
                )
            return (
                f"STATUS AWAITING: '{target.name}' belum ada. Instruksi sudah ditulis ke "
                f"./workspace/{TODO_FILENAME} dan akan dikirim ke Creator lewat notifikasi. "
                "Akhiri rapat sekarang dengan kata kunci AWAITING_CREATOR_UPLOAD."
            )

        if self.settings.mock_creator or env_flag("STUDIO_MOCK_CREATOR"):
            return self._mock_creator_file(target, reason="STUDIO_MOCK_CREATOR aktif")

        self._banner(
            "BIMA (PM) MENUNGGU UPLOAD DARI ANDA",
            [
                f"Instruksi lengkap : ./{self._rel(todo_path)}",
                f"Taruh file        : {target.name} → {self.paths.input_creator}",
                "Ketik DONE saat file siap, atau MOCK untuk file dummy.",
            ],
        )

        interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
        deadline = time.monotonic() + self.settings.creator_timeout if self.settings.creator_timeout else None

        while True:
            if target.exists() and target.stat().st_size > 0:
                return (
                    f"STATUS UPLOADED: '{target.name}' terverifikasi di ./input_creator/ "
                    f"({_fmt_bytes(target.stat().st_size)}). Raka_Producer boleh mulai render."
                )
            if deadline is not None and time.monotonic() > deadline:
                return (
                    f"STATUS TIMEOUT: '{target.name}' belum ada setelah {self.settings.creator_timeout:.0f} detik. "
                    f"Instruksi ada di ./{self._rel(todo_path)}. Hentikan pipeline dan laporkan ke Creator."
                )
            if not interactive:
                return (
                    "STATUS BLOCKED: tidak ada TTY untuk menunggu Creator dan file belum ada. "
                    "Jalankan ulang dengan --mock-creator di CI, atau taruh file secara manual."
                )
            try:
                answer = input(
                    f"[BIMA] Taruh '{target.name}' di ./input_creator/ lalu ketik DONE "
                    "(MOCK untuk dummy, SKIP untuk batalkan): "
                ).strip().upper()
            except (EOFError, KeyboardInterrupt):
                return "STATUS BLOCKED: input dihentikan Creator. Pipeline dijeda."

            if answer in {"MOCK", "DUMMY"}:
                return self._mock_creator_file(target, reason="Creator memilih MOCK")
            if answer in {"SKIP", "CANCEL", "BATAL"}:
                return "STATUS CANCELLED: Creator membatalkan unggahan. Jangan lanjut ke render."
            if answer in {"DONE", "OK", "SIAP"}:
                if not (target.exists() and target.stat().st_size > 0):
                    print(f"⚠️  File '{target.name}' belum ditemukan di {self.paths.input_creator}.", flush=True)
                continue
            print("Perintah tidak dikenal. Ketik DONE, MOCK, atau SKIP.", flush=True)

    def _mock_creator_file(self, target: Path, *, reason: str) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(SIMULATION_MARKER + b"|dummy A-roll for rehearsal|")
        self._log("mock_creator_file", file=self._rel(target), reason=reason)
        self._banner(
            "MODE SIMULASI AKTIF",
            [
                f"File dummy dibuat: ./{self._rel(target)} ({_fmt_bytes(target.stat().st_size)})",
                f"Alasan: {reason}",
                "Render akan ditandai 'simulation', bukan video asli.",
            ],
        )
        return (
            f"STATUS UPLOADED (SIMULASI): file dummy '{target.name}' dibuat di ./input_creator/. "
            "Ini BUKAN rekaman asli Creator; sampaikan terus terang di ruang rapat."
        )

    # ------------------------------------------------------ 3. render (Raka)
    def assemble_and_render_video(
        self,
        raw_video_filename: Annotated[str, "Nama file mentah Creator di ./input_creator/ (contoh: a_roll_raw.mp4)"],
        output_filename: Annotated[str, "Nama file hasil render di ./output_ready/ (contoh: final_render.mp4)"],
        hook_title: Annotated[str, "Teks judul/hook untuk intro Remotion (opsional, boleh kosong)"] = "",
        remotion_composition: Annotated[str, "ID composition Remotion di repo KRA (opsional, kosongkan bila tidak dipakai)"] = "",
    ) -> str:
        try:
            source = self._safe_path(self.paths.input_creator, raw_video_filename)
            target = self._safe_path(self.paths.output_ready, output_filename)
        except ValueError as exc:
            return f"ERROR: {exc}"

        if not source.exists():
            return f"ERROR RENDER: input '{self._rel(source)}' tidak ditemukan. Jangan mengarang hasil render."
        if source.stat().st_size == 0:
            return f"ERROR RENDER: input '{self._rel(source)}' 0 byte (korup)."

        target.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        dummy_input = SIMULATION_MARKER in source.read_bytes()[:64]
        caps = self.capabilities

        if self.settings.real_render and not dummy_input:
            if not caps.can_render:
                return (
                    "ERROR RENDER: STUDIO_REAL_RENDER aktif tetapi ffmpeg tidak ditemukan di PATH "
                    f"({caps.summary()}). Render tidak dijalankan."
                )
            self._banner(
                "RAKA MERENDER VIDEO (FFMPEG)",
                [
                    f"input  : ./{self._rel(source)}",
                    f"output : ./{self._rel(target)}",
                    f"engine : {caps.summary()}",
                ],
            )
            pipeline = MediaPipeline(self.settings, caps)
            try:
                result = pipeline.render(
                    source,
                    target,
                    hook_title=hook_title,
                    remotion_composition=remotion_composition,
                )
            except MediaError as exc:
                self._log("render", file=self._rel(target), engine="ffmpeg", status="error", error=str(exc))
                return f"ERROR RENDER: {exc}"

            report = result.as_dict()
            report.update(
                {
                    "file": self._rel(target),
                    "source": self._rel(source),
                    "source_bytes": source.stat().st_size,
                    "sha256": self._sha256(target),
                    "rendered_at": _now(),
                    "target_spec": {
                        "resolution": f"{pipeline.width}x{pipeline.height}",
                        "fps": pipeline.fps,
                        "audio_lufs": -14.0,
                    },
                }
            )
            self._write_sidecar(target, report)
            self._log(
                "render",
                file=self._rel(target),
                engine="ffmpeg",
                bytes=target.stat().st_size,
                steps=[step.name for step in result.steps if step.status == "ok"],
                failed=[step.name for step in result.steps if step.status == "failed"],
            )
            return f"RENDER FISIK SELESAI: {json.dumps(report, ensure_ascii=False)}"

        notes: list[str] = []
        if self.settings.real_render and dummy_input:
            notes.append("input adalah file dummy simulasi; ffmpeg tidak dijalankan")
        elif not self.settings.real_render:
            notes.append("STUDIO_REAL_RENDER tidak aktif; output dummy")
        if self.settings.real_render and not caps.can_render:
            notes.append("ffmpeg tidak tersedia di PATH; output dummy")

        target.write_bytes(SIMULATION_MARKER + b"|" + source.read_bytes()[: 2 * 1024 * 1024])
        report = {
            "file": self._rel(target),
            "engine": "simulation",
            "source": self._rel(source),
            "source_bytes": source.stat().st_size,
            "output_bytes": target.stat().st_size,
            "sha256": self._sha256(target),
            "rendered_at": _now(),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "target_spec": {"resolution": "1920x1080", "fps": self.settings.render_fps, "audio_lufs": -14.0},
            "audio_normalized": False,
            "thumbnails": [],
            "notes": notes,
        }
        self._write_sidecar(target, report)
        self._log("render", file=self._rel(target), engine="simulation", bytes=target.stat().st_size)
        return f"RENDER SELESAI (SIMULASI): {json.dumps(report, ensure_ascii=False)}"

    def _write_sidecar(self, target: Path, report: dict[str, Any]) -> Path:
        sidecar = target.with_name(target.stem + ".render.json")
        sidecar.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return sidecar

    # ------------------------------------------------------ 4. audit (Damon)
    def inspect_rendered_output(
        self,
        video_filename: Annotated[str, "Nama file di ./output_ready/ yang akan diaudit (contoh: final_render.mp4)"],
    ) -> str:
        try:
            target = self._safe_path(self.paths.output_ready, video_filename)
        except ValueError as exc:
            return f"AUDIT GAGAL: {exc}"

        if not target.exists():
            return (
                f"AUDIT GAGAL: '{video_filename}' tidak ada di ./output_ready/. "
                "Raka_Producer belum benar-benar merender — jangan percaya klaimnya."
            )
        size = target.stat().st_size
        if size == 0:
            return f"AUDIT GAGAL: '{video_filename}' 0 byte (korup)."

        head = target.read_bytes()[:64]
        simulated = SIMULATION_MARKER in head
        sidecar = target.with_name(target.stem + ".render.json")
        report: dict[str, Any] = {}
        if sidecar.exists():
            try:
                report = json.loads(sidecar.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                report = {}

        evidence: dict[str, Any] = {
            "file": self._rel(target),
            "bytes": size,
            "sha256": self._sha256(target),
            "sidecar_report": self._rel(sidecar) if sidecar.exists() else None,
            "render_engine": report.get("engine", "unknown"),
            "simulation_artifact": simulated,
        }
        if report.get("probe"):
            evidence["probe"] = report["probe"]

        problems: list[str] = []
        if not sidecar.exists():
            problems.append("laporan render (.render.json) tidak ditemukan")
        if not simulated and b"ftyp" not in head[:16]:
            problems.append("header bukan MP4 yang valid")
        probe = report.get("probe") or {}
        if probe and probe.get("height") and int(probe["height"]) < 1080:
            problems.append(f"resolusi {probe.get('width')}x{probe.get('height')} di bawah 1080p")

        if problems:
            self._log("audit_video", verdict="FAIL", problems=problems)
            return f"AUDIT GAGAL: {'; '.join(problems)}. BUKTI: {json.dumps(evidence, ensure_ascii=False)}"

        if simulated and self.settings.real_render:
            self._log("audit_video", verdict="FAIL", problems=["simulation_in_production_mode"])
            return (
                "AUDIT GAGAL: mode real render aktif, tetapi output ini artefak SIMULASI "
                "(bukan hasil ffmpeg). Jalankan render ulang dengan file asli."
            )

        warning = (
            " Catatan: ini artefak SIMULASI (bukan rekaman asli), pipeline tetap boleh lanjut untuk uji alur."
            if simulated
            else ""
        )
        self._log("audit_video", verdict="PASS", file=self._rel(target), bytes=size)
        return (
            f"AUDIT LULUS: '{video_filename}' ada ({_fmt_bytes(size)}), engine={evidence['render_engine']}. "
            f"BUKTI FISIK: {json.dumps(evidence, ensure_ascii=False)}{warning}"
        )

    # -------------------------------------------------- 5. upload (Sora)
    def upload_to_youtube(
        self,
        video_filename: Annotated[str, "Nama file video final di ./output_ready/"],
        title: Annotated[str, "Judul SEO YouTube, idealnya <= 55 karakter"],
        description: Annotated[str, "Deskripsi lengkap dengan Chapters, baris pertama harus 00:00"],
        tags: Annotated[list[str], "Daftar tag YouTube (3-12 tag relevan)"],
        privacy_status: Annotated[str, "Privasi video: 'unlisted' (default) atau 'private'"] = "unlisted",
    ) -> str:
        try:
            video = self._safe_path(self.paths.output_ready, video_filename)
        except ValueError as exc:
            return f"ERROR UPLOAD: {exc}"
        if not video.exists() or video.stat().st_size == 0:
            return f"ERROR UPLOAD: '{video_filename}' tidak ada / 0 byte di ./output_ready/."

        title = (title or "").strip()
        description = (description or "").strip()
        tag_list = [str(tag).strip() for tag in (tags or []) if str(tag).strip()][:15]
        privacy = (privacy_status or "unlisted").strip().lower()
        if privacy not in {"unlisted", "private", "public"}:
            privacy = "unlisted"

        warnings: list[str] = []
        if not title:
            return "ERROR UPLOAD: judul kosong."
        if len(title) > 100:
            return f"ERROR UPLOAD: judul {len(title)} karakter, batas YouTube 100. Pendekkan dulu."
        if len(title) > 55:
            warnings.append(f"judul {len(title)} karakter, berpotensi terpotong di mobile (target <= 55)")
        timestamps = [line.strip() for line in description.splitlines() if line.strip().startswith(tuple("0123456789"))]
        if not timestamps or not timestamps[0].startswith("00:00"):
            warnings.append("chapters tidak diawali 00:00")
        if len(tag_list) < 3:
            warnings.append("tag kurang dari 3")

        client_secret = self.paths.root / "client_secret.json"
        if not client_secret.exists():
            receipt = {
                "status": "UPLOADED_SIMULATION",
                "video_path": self._rel(video),
                "title": title,
                "privacy": privacy,
                "tags": tag_list,
                "chapters_detected": len(timestamps),
                "warnings": warnings,
                "studio_url": "https://studio.youtube.com/",
                "next_step": "Taruh client_secret.json (OAuth Desktop App) di root proyek untuk upload live.",
                "created_at": _now(),
            }
            (self.paths.output_ready / RECEIPT_FILENAME).write_text(
                json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            self._log("upload_youtube", mode="simulation", title=title)
            return f"UPLOAD YOUTUBE (SIMULASI): {json.dumps(receipt, ensure_ascii=False)}"

        try:
            response = self._youtube_insert(video, title, description, tag_list, privacy)
        except Exception as exc:  # noqa: BLE001 - error API apa pun harus jadi pesan bot
            self._log("upload_youtube", mode="live", status="error", error=str(exc))
            return (
                f"ERROR UPLOAD LIVE: {type(exc).__name__}: {_short(str(exc), 300)}. "
                "Jangan klaim video sudah tayang."
            )

        video_id = response.get("id", "")
        receipt = {
            "status": "UPLOADED_LIVE",
            "video_id": video_id,
            "url": f"https://youtu.be/{video_id}",
            "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
            "title": title,
            "privacy": privacy,
            "tags": tag_list,
            "warnings": warnings,
            "created_at": _now(),
        }
        (self.paths.output_ready / RECEIPT_FILENAME).write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._log("upload_youtube", mode="live", status="ok", video_id=video_id)
        return f"UPLOAD YOUTUBE SUKSES: {json.dumps(receipt, ensure_ascii=False)}"

    def _youtube_insert(
        self, video: Path, title: str, description: str, tags: list[str], privacy: str
    ) -> dict[str, Any]:
        """Upload asli via YouTube Data API v3 (import lazy: dependensi opsional)."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        scopes = ["https://www.googleapis.com/auth/youtube.upload"]
        token_path = self.paths.root / "token.json"
        creds: Credentials | None = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.paths.root / "client_secret.json"), scopes
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

        youtube = build("youtube", "v3", credentials=creds)
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": os.environ.get("STUDIO_YOUTUBE_CATEGORY", "22"),
                "defaultLanguage": os.environ.get("STUDIO_YOUTUBE_LANGUAGE", "id"),
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        return request.execute()

    # --------------------------------------------------------------- helper
    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()[:16]
