"""Tool nyata yang dieksekusi para Super Bot.

Semua tool mengembalikan **string** (AutoGen meneruskan hasilnya ke LLM) dan
selalu jujur soal apa yang benar-benar terjadi di disk:

* render memakai ffmpeg asli bila ``STUDIO_REAL_RENDER=1``, kalau tidak hasilnya
  ditandai ``simulation`` di sidecar report — Damon bisa membedakannya;
* upload memakai YouTube Data API v3 asli bila ``client_secret.json`` tersedia,
  kalau tidak hanya menulis receipt simulasi.

Semua path divalidasi supaya bot tidak bisa menulis keluar dari folder studio.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from .config import Settings, env_flag

SIMULATION_MARKER = b"STUDIO_SIMULATED_MP4"
TODO_FILENAME = "creator_todo_list.md"
RECEIPT_FILENAME = "youtube_publish_receipt.json"

# Nama tool harus UNIK per bot. AutoGen merutekan pesan `tool_calls` ke agen yang
# memiliki fungsi tersebut (func_call_filter); kalau dua bot berbagi nama fungsi
# yang sama, eksekusi bisa mendarat di bot yang salah.
TOOL_NAMES: tuple[str, ...] = (
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
        ffmpeg = shutil.which("ffmpeg")
        engine = "simulation"
        notes: list[str] = []
        started = time.monotonic()

        if self.settings.real_render and ffmpeg and SIMULATION_MARKER not in source.read_bytes()[:64]:
            engine = "ffmpeg"
            result = self._run_ffmpeg(ffmpeg, source, target)
            if result is not None:
                notes.append(result)
                engine = "simulation" if SIMULATION_MARKER in target.read_bytes()[:64] else "ffmpeg"
        else:
            if self.settings.real_render and ffmpeg:
                notes.append("input adalah file dummy simulasi; ffmpeg tidak dijalankan")
            elif not self.settings.real_render:
                notes.append("STUDIO_REAL_RENDER tidak aktif; output dummy")
            else:
                notes.append("ffmpeg tidak tersedia di PATH; output dummy")

        if not target.exists() or target.stat().st_size == 0:
            target.write_bytes(SIMULATION_MARKER + b"|" + source.read_bytes()[: 2 * 1024 * 1024])
            engine = "simulation"

        probe = self._probe_media(target) if engine == "ffmpeg" else {}
        report: dict[str, Any] = {
            "file": self._rel(target),
            "engine": engine,
            "source": self._rel(source),
            "source_bytes": source.stat().st_size,
            "output_bytes": target.stat().st_size,
            "sha256": self._sha256(target),
            "rendered_at": _now(),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "target_spec": {"resolution": "1920x1080", "fps": 60, "audio_lufs": -14.0},
            "audio_normalized": engine == "ffmpeg",
            "thumbnails": [],
            "notes": notes,
        }
        report.update(probe)
        sidecar = target.with_name(target.stem + ".render.json")
        sidecar.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        self._log("render", file=self._rel(target), engine=engine, bytes=target.stat().st_size)

        verdict = "RENDER SELESAI" if engine == "ffmpeg" else "RENDER SELESAI (SIMULASI)"
        return f"{verdict}: {json.dumps(report, ensure_ascii=False)}"

    def _run_ffmpeg(self, ffmpeg: str, source: Path, target: Path) -> str | None:
        """Render 1080p60 + loudnorm -14 LUFS. Kembalikan catatan bila audio gagal."""
        base_cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-vf", "scale=1920:-2:flags=lanczos,fps=60",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        ]
        with_audio = base_cmd + [
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target),
        ]
        proc = subprocess.run(with_audio, capture_output=True, text=True)
        if proc.returncode == 0:
            return None

        without_audio = base_cmd + ["-an", "-movflags", "+faststart", str(target)]
        retry = subprocess.run(without_audio, capture_output=True, text=True)
        if retry.returncode == 0:
            return f"input tanpa audio valid; render ulang tanpa audio ({_short(proc.stderr, 200)})"
        return f"ffmpeg gagal (exit {retry.returncode}): {_short(retry.stderr)}"

    def _probe_media(self, path: Path) -> dict[str, Any]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe or not path.exists():
            return {}
        cmd = [
            ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                return {}
            data = json.loads(proc.stdout or "{}")
        except (subprocess.SubprocessError, json.JSONDecodeError):
            return {}
        video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
        audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
        return {
            "probe": {
                "duration_s": round(float(data.get("format", {}).get("duration", 0.0) or 0.0), 2),
                "width": video.get("width"),
                "height": video.get("height"),
                "fps": video.get("r_frame_rate"),
                "video_codec": video.get("codec_name"),
                "audio_codec": audio.get("codec_name"),
            }
        }

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
