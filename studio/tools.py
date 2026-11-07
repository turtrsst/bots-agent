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
import html
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
from .memory import tokenize
from .resilience import call_with_resilience
from .story import StoryBuilder, StoryError, validate_story
from .thumbnail import primary_thumbnail

SIMULATION_MARKER = b"STUDIO_SIMULATED_MP4"
TODO_FILENAME = "creator_todo_list.md"
RECEIPT_FILENAME = "youtube_publish_receipt.json"
RADAR_FILENAME = "vito_realtime_radar.json"
SCENE_FILENAME = "script_production.json"

# Nama tool harus UNIK per bot. AutoGen merutekan pesan `tool_calls` ke agen yang
# memiliki fungsi tersebut (func_call_filter); kalau dua bot berbagi nama fungsi
# yang sama, eksekusi bisa mendarat di bot yang salah.
TOOL_NAMES: tuple[str, ...] = (
    "research_current_trends",
    "scan_viral_trends_and_youtube_outliers",
    "get_recent_content_memory",
    "check_topic_similarity_in_memory",
    "register_new_episode_to_memory",
    "save_content_brief",
    "save_production_script",
    "save_scene_breakdown",
    "assemble_story_from_script",
    "request_and_check_creator_files",
    "assemble_and_render_video",
    "inspect_rendered_output",
    "upload_to_youtube",
    "upload_youtube_short",
    "send_direct_alert_to_creator",
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

    def __init__(
        self,
        settings: Settings,
        notifier: Any | None = None,
        radar: Callable[..., Any] | None = None,
        memory: Any | None = None,
    ) -> None:
        self.settings = settings
        self.paths = settings.paths.ensure()
        self._caps = None
        self._notifier = notifier
        self._radar = radar
        self._memory = memory

    @property
    def memory(self):
        """Katalog episode (memori episodik) — SQLite yang sama dengan riwayat rapat."""
        if self._memory is None:
            from .memory import MemoryStore

            self._memory = MemoryStore(self.settings.memory_db_path)
        return self._memory

    @property
    def radar(self):
        """Radar tren real-time (bisa di-inject saat test agar tanpa jaringan)."""
        if self._radar is None:
            from .research import scan_radar

            self._radar = scan_radar
        return self._radar

    @property
    def notifier(self):
        """Kanal notifikasi (Telegram/konsol) — dibangun sekali, bisa di-inject saat test."""
        if self._notifier is None:
            from .notify import build_notifier

            self._notifier = build_notifier(self.settings)
        return self._notifier

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
                "Isi lengkap content_brief.json (JSON valid: titles/title_options, thumbnails, "
                "thumbnail_badge, thumbnail_hooks, thumbnail_visual_prompt, angle, audience, "
                "keywords, promise)",
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
            "scan_viral_trends_and_youtube_outliers",
            "get_recent_content_memory",
            "check_topic_similarity_in_memory",
            "register_new_episode_to_memory",
            "save_scene_breakdown",
            "request_and_check_creator_files",
            "assemble_and_render_video",
            "assemble_story_from_script",
            "inspect_rendered_output",
            "upload_to_youtube",
            "upload_youtube_short",
            "send_direct_alert_to_creator",
        ):
            impl = getattr(self, name)

            @functools.wraps(impl)
            def wrapper(*args: Any, __impl: Callable[..., str] = impl, __name: str = name, **kwargs: Any) -> str:
                # Self-healing: exception tak terduga tidak boleh mematikan GroupChat.
                return call_with_resilience(
                    __name,
                    __impl,
                    *args,
                    notifier=self.notifier,
                    **kwargs,
                )

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

    # ------------------------------- 1a-bis. naskah mesin untuk Remotion (Clara)
    def save_scene_breakdown(
        self,
        content: Annotated[
            str,
            "JSON scene breakdown lengkap (project_id, title, fps, scenes[] dengan scene_id, "
            "type REMOTION_DYNAMIC/CREATOR_AROLL, narration_text, broll_asset/source_video, "
            "highlight_words)",
        ],
    ) -> str:
        """Validasi + simpan script_production.json yang dibaca Remotion & concat."""
        if not (content or "").strip():
            return "ERROR: isi scene breakdown kosong, tidak ada yang disimpan."
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            return (
                f"ERROR JSON: {exc}. Kirim objek JSON valid (bukan Markdown, bukan blok ```json)."
            )

        report = validate_story(data)
        if not report.ok or report.script is None:
            return (
                "ERROR SCHEMA: " + "; ".join(report.problems) + ". Perbaiki JSON lalu simpan ulang."
            )

        target = self.paths.workspace / SCENE_FILENAME
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._log(
            "save_scene_breakdown",
            file=self._rel(target),
            scenes=len(report.script.scenes),
            fps=report.script.fps,
        )
        warning = f" Catatan: {'; '.join(report.warnings)}." if report.warnings else ""
        return (
            f"TERSIMPAN: ./{self._rel(target)} ({len(report.script.scenes)} scene, "
            f"fps {report.script.fps}).{warning} Ini naskah mesin untuk Remotion + concat."
        )

    # -------------------------------------------- 1b. riset tren nyata (Vito)
    def scan_viral_trends_and_youtube_outliers(
        self,
        niche_query: Annotated[
            str,
            "Topik/niche yang ingin dipindai, mis. 'AI agent', 'misteri teknologi'",
        ],
        geo_region: Annotated[
            str,
            "Kode negara Google Trends: 'ID' untuk Indonesia, 'US' untuk global",
        ] = "ID",
    ) -> str:
        """Radar real-time: Google Trends + berita 48 jam + outlier YouTube."""
        niche = (niche_query or "").strip()
        if not niche:
            return "ERROR RADAR: niche_query kosong; sebutkan topik yang ingin dipindai."
        print(f"📡 [VITO RADAR] Memindai Google Trends ({geo_region}) & outlier YouTube: '{niche}'...", flush=True)
        radar = self.radar(self.settings, niche, geo=geo_region)
        payload = radar.as_dict()
        radar_path = self.paths.workspace / RADAR_FILENAME
        radar_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self._log(
            "scan_radar",
            niche=niche,
            real=radar.real,
            trends=len(radar.trends),
            news=len(radar.news),
            outliers=len(radar.outliers),
        )
        if not radar.real:
            return (
                "RISET TIDAK TERSEDIA: radar real-time gagal "
                f"({'; '.join(radar.attempts) or 'tanpa percobaan'}). "
                "Jangan mengarang tren; minta Creator memberi arah topik."
            )
        return (
            "DATA RISET REAL-TIME DITEMUKAN (arsip: workspace/"
            f"{RADAR_FILENAME}):\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

    def get_recent_content_memory(
        self,
        limit: Annotated[int, "Jumlah episode terakhir yang ditampilkan (default 15)"] = 15,
    ) -> str:
        """Riwayat episode KRA agar Vito tidak mengulang cerita lama."""
        return self.memory.episodes_text(max(1, min(int(limit or 15), 50)))

    def check_topic_similarity_in_memory(
        self,
        candidate_topic_and_title: Annotated[
            str,
            "Gabungan calon topik dan judul yang ingin diuji terhadap riwayat episode",
        ],
    ) -> str:
        """Bandingkan calon topik dengan katalog episode (ambang 55%)."""
        candidate = (candidate_topic_and_title or "").strip()
        if not tokenize(candidate):
            return "ERROR MEMORI: calon topik kosong; sebutkan topik + judul yang ingin diuji."
        matches = self.memory.check_topic_similarity(candidate)
        self._log("check_topic_memory", candidate=candidate[:120], matches=len(matches))
        if not matches:
            return (
                "TOPIC_UNIQUE_PASS: topik dan judul ini segar dan belum pernah "
                "diproduksi di KRA."
            )
        detail = json.dumps(matches, ensure_ascii=False, indent=2)
        return (
            "DUPLICATE_WARNING: calon topik ini terlalu mirip dengan konten KRA sebelumnya.\n"
            f"Rincian kemiripan:\n{detail}\n"
            "INSTRUKSI: ganti sudut pandang (angle) atau pilih topik lain dari radar; "
            "jangan melanjutkan topik ini."
        )

    def register_new_episode_to_memory(
        self,
        project_id: Annotated[str, "ID unik proyek cerita (mis. story_kra_20261107)"],
        topic_summary: Annotated[str, "Ringkasan topik dan sudut pandang cerita"],
        primary_title: Annotated[str, "Judul utama yang dipilih"],
        keywords: Annotated[str, "Kata kunci utama, dipisahkan koma"] = "",
    ) -> str:
        """Catat episode yang sudah lolos SCRIPT PASS ke memori episodik."""
        title = (primary_title or "").strip()
        if not title:
            return "ERROR MEMORI: judul utama kosong; episode tidak dicatat."
        all_titles = self._brief_titles()
        saved = self.memory.register_episode(
            project_id,
            topic_summary=(topic_summary or "").strip() or title,
            primary_title=title,
            keywords=keywords,
            all_titles=all_titles,
            status="SCRIPT_APPROVED",
        )
        self._log("register_episode", project_id=saved, title=title[:120])
        duplicate = self.memory.check_topic_similarity(f"{topic_summary} {title}")
        duplicate = [match for match in duplicate if match["project_id"] != saved]
        note = ""
        if duplicate:
            note = (
                f"\n⚠️ Catatan: masih ada {len(duplicate)} episode lama yang mirip "
                f"(tertinggi {duplicate[0]['similarity_percent']}). Sampaikan ke Vito di rapat "
                "berikutnya, bukan disembunyikan."
            )
        return (
            f"MEMORI TERSIMPAN: proyek '{saved}' ({title}) tercatat di katalog episode SQLite KRA."
            f"{note}"
        )

    def _brief_titles(self) -> list[str]:
        brief = self._read_json(self.paths.workspace / "content_brief.json")
        raw = brief.get("title_options") or brief.get("titles") or []
        titles: list[str] = []
        for entry in raw if isinstance(raw, list) else []:
            title = entry.get("title") if isinstance(entry, dict) else entry
            if str(title or "").strip():
                titles.append(str(title).strip())
        return titles

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _record_episode_upload(self, *, status: str, video_id: str, url: str, title: str) -> str:
        """Update katalog episode setelah upload (live atau simulasi)."""
        try:
            script = self._read_json(self.paths.workspace / SCENE_FILENAME)
            project_id = str(script.get("project_id") or "").strip()
            if not project_id:
                project_id = f"kra_{int(time.time())}"
            if self.memory.get_episode(project_id) is None:
                self.memory.register_episode(
                    project_id,
                    topic_summary=str(script.get("title") or title).strip() or title,
                    primary_title=title,
                    keywords=script.get("keywords") or "",
                )
            self.memory.mark_episode_published(
                project_id, video_id=video_id, url=url, title=title, status=status
            )
            return project_id
        except Exception as exc:  # noqa: BLE001 - memori tidak boleh menggagalkan upload
            print(f"⚠️  Gagal memperbarui memori episodik: {type(exc).__name__}: {exc}", flush=True)
            return ""

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
            report["probe"] = result.specs
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

    # ------------------------------- 3b. story Remotion + concat (Raka)
    def assemble_story_from_script(
        self,
        script_json_filename: Annotated[
            str, "Nama naskah JSON dari Clara di ./workspace/ (default script_production.json)"
        ] = SCENE_FILENAME,
        output_filename: Annotated[
            str, "Nama file hasil di ./output_ready/ (default final_render.mp4)"
        ] = "final_render.mp4",
        transition_name: Annotated[
            str, "Transisi ffmpeg-concat (directionalWarp, CrossZoom, ...); kosongkan untuk cut langsung"
        ] = "directionalWarp",
    ) -> str:
        """Rakit semua scene: Whisper → Remotion per scene → concat → ffprobe."""
        try:
            script_path = self._safe_path(self.paths.workspace, script_json_filename)
            target = self._safe_path(self.paths.output_ready, output_filename)
        except ValueError as exc:
            return f"ERROR: {exc}"

        if not script_path.exists():
            return (
                f"ERROR RENDER: naskah mesin '{self._rel(script_path)}' belum ada. "
                "Minta Clara menyimpan scene breakdown lebih dulu, atau pakai "
                "assemble_and_render_video untuk jalur satu file."
            )

        caps = self.capabilities
        if not caps.can_render:
            return f"ERROR RENDER: ffmpeg tidak ditemukan di PATH ({caps.summary()})."

        started = time.monotonic()
        self._banner(
            "RAKA MERAKIT STORY (REMOTION + CONCAT)",
            [
                f"naskah : ./{self._rel(script_path)}",
                f"output : ./{self._rel(target)}",
                f"engine : {caps.summary()}",
            ],
        )
        builder = StoryBuilder(self.settings, caps, notifier=self.notifier)
        try:
            result = builder.build(
                script_path,
                target,
                transition=transition_name.strip(),
                fallback_source="a_roll_raw.mp4",
            )
        except StoryError as exc:
            self._log("render_story", status="error", error=str(exc))
            return f"ERROR RENDER: {exc}"

        report = result.as_dict()
        report["probe"] = result.specs
        report.update(
            {
                "source_script": self._rel(script_path),
                "sha256": self._sha256(target),
                "rendered_at": _now(),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "target_spec": {
                    "resolution": "1920x1080",
                    "fps": result.fps,
                    "audio_lufs": -14.0,
                },
            }
        )
        self._write_sidecar(target, report)
        self._log(
            "render_story",
            file=self._rel(target),
            engine=result.engine,
            scenes=len(result.scenes),
            notes=result.notes[:5],
        )
        return f"RENDER STORY SELESAI: {json.dumps(report, ensure_ascii=False)}"

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
        probe = report.get("probe") or report.get("specs") or {}
        if probe:
            evidence["probe"] = probe

        problems: list[str] = []
        if not sidecar.exists():
            problems.append("laporan render (.render.json) tidak ditemukan")
        if not simulated and b"ftyp" not in head[:16]:
            problems.append("header bukan MP4 yang valid")
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

        thumb = primary_thumbnail(self.settings)
        client_secret = self.paths.root / "client_secret.json"
        if not client_secret.exists():
            project_id = self._record_episode_upload(
                status="UPLOADED_SIMULATION", video_id="", url="", title=title
            )
            receipt = {
                "project_id": project_id,
                "status": "UPLOADED_SIMULATION",
                "video_path": self._rel(video),
                "title": title,
                "privacy": privacy,
                "tags": tag_list,
                "chapters_detected": len(timestamps),
                "warnings": warnings,
                "video_url": "",
                "thumbnail": {
                    "status": "not_sent",
                    "file": thumb.name if thumb else "",
                    "note": "thumbnail hanya dipasang pada upload live",
                },
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
        thumbnail_status = response.get("_thumbnail_status", "not_attempted")
        live_url = f"https://youtu.be/{video_id}" if video_id else ""
        project_id = self._record_episode_upload(
            status="PUBLISHED", video_id=video_id, url=live_url, title=title
        )
        receipt = {
            "status": "UPLOADED_LIVE",
            "project_id": project_id,
            "thumbnail": {
                "status": thumbnail_status,
                "file": thumb.name if thumb else "",
            },
            "video_id": video_id,
            "url": f"https://youtu.be/{video_id}",
            "video_url": f"https://youtu.be/{video_id}",
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

    def upload_youtube_short(
        self,
        short_filename: Annotated[
            str,
            "Nama file Shorts di ./output_ready/shorts/; kosongkan untuk memakai file .mp4 terbaru",
        ] = "",
        title: Annotated[
            str,
            "Judul Shorts (otomatis diberi #Shorts bila belum ada); kosongkan untuk memakai brief Vito",
        ] = "",
        privacy_status: Annotated[str, "Privasi: 'unlisted' (default), 'private', atau 'public'"] = "unlisted",
        force: Annotated[bool, "True = upload ulang walau file ini sudah pernah tayang"] = False,
    ) -> str:
        """Publikasikan YouTube Shorts 9:16 dari ./output_ready/shorts/ (Sora)."""
        from .shorts import (
            RECEIPT_FILENAME as SHORTS_RECEIPT,
        )
        from .shorts import (
            default_short_description,
            default_short_title,
            ensure_shorts_hashtag,
            latest_short,
            published_record,
            record_publish,
        )

        if short_filename:
            try:
                short = self._safe_path(self.paths.output_ready, short_filename)
            except ValueError as exc:
                return f"ERROR UPLOAD SHORTS: {exc}"
        else:
            short = latest_short(self.settings)
        if short is None or not short.is_file() or short.stat().st_size == 0:
            return (
                "TIDAK ADA SHORTS: belum ada file .mp4 di ./output_ready/shorts/. "
                "Raka harus merender master dulu (auto-cutter berjalan setelah upload master)."
            )

        final_title = ensure_shorts_hashtag(title.strip() or default_short_title(self.settings))
        description = default_short_description(final_title, duration=self.settings.shorts_duration)
        tags = ["shorts", "ai", "teknologi", "otomasi"]
        privacy = (privacy_status or "unlisted").strip().lower()
        if privacy not in {"unlisted", "private", "public"}:
            privacy = "unlisted"

        previous = published_record(self.settings, short)
        if previous and previous.get("status") in {"UPLOADED_LIVE", "UPLOADED_SIMULATION"} and not force:
            payload = {
                "status": "SUDAH_TAYANG",
                "file": short.name,
                "title": previous.get("title", ""),
                "url": previous.get("url", ""),
                "published_at": previous.get("published_at", ""),
            }
            self._log("upload_youtube_short", mode="skip", file=short.name)
            return f"SHORTS SUDAH TAYANG: {json.dumps(payload, ensure_ascii=False)}"

        client_secret = self.paths.root / "client_secret.json"
        if not client_secret.exists():
            record_publish(
                self.settings,
                short,
                status="UPLOADED_SIMULATION",
                title=final_title,
                privacy=privacy,
            )
            receipt = {
                "status": "UPLOADED_SIMULATION",
                "file": short.name,
                "title": final_title,
                "privacy": privacy,
                "tags": tags,
                "video_url": "",
                "note": "client_secret.json tidak ada → tidak ada video yang benar-benar tayang",
                "next_step": "Taruh client_secret.json (OAuth Desktop) di root proyek untuk upload live.",
                "created_at": _now(),
            }
            target = self.paths.output_ready / SHORTS_RECEIPT
            target.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
            self._log("upload_youtube_short", mode="simulation", file=short.name)
            return f"UPLOAD SHORTS (SIMULASI): {json.dumps(receipt, ensure_ascii=False)}"

        try:
            response = self._youtube_insert(
                short, final_title, description, tags, privacy, apply_thumbnail=False
            )
        except Exception as exc:  # noqa: BLE001 - error API apa pun harus jadi pesan bot
            self._log("upload_youtube_short", mode="live", status="error", error=str(exc))
            return (
                f"ERROR UPLOAD SHORTS: {type(exc).__name__}: {_short(str(exc), 300)}. "
                "Jangan klaim Shorts sudah tayang."
            )

        video_id = str(response.get("id") or "")
        url = f"https://youtu.be/{video_id}" if video_id else ""
        record_publish(
            self.settings,
            short,
            status="UPLOADED_LIVE",
            title=final_title,
            video_id=video_id,
            url=url,
            privacy=privacy,
        )
        receipt = {
            "status": "UPLOADED_LIVE",
            "file": short.name,
            "title": final_title,
            "video_id": video_id,
            "url": url,
            "privacy": privacy,
            "tags": tags,
            "created_at": _now(),
        }
        target = self.paths.output_ready / SHORTS_RECEIPT
        target.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
        self._log("upload_youtube_short", mode="live", status="ok", video_id=video_id)
        return f"SHORTS TERPUBLIKASI: {json.dumps(receipt, ensure_ascii=False)}"

    def send_direct_alert_to_creator(
        self,
        bot_sender_name: Annotated[
            str,
            "Nama bot pengirim, mis. 'Bima_StudioPM' atau 'Sora_YouTubePublisher'",
        ],
        message_text: Annotated[
            str,
            "Isi laporan singkat atau link YouTube yang dikirim ke Telegram Creator",
        ],
    ) -> str:
        """Kirim pesan langsung ke Telegram Creator (Rama)."""
        from .notify import send_html

        text = (message_text or "").strip()
        if not text:
            return "ERROR ALERT: pesan kosong, tidak ada yang dikirim."
        sender = html.escape((bot_sender_name or "Super Bot KRA").strip())[:80]
        body = f"💬 <b>Pesan dari {sender}:</b>\n\n{html.escape(text)}"
        if send_html(self.notifier, body):
            self._log("telegram_alert", sender=sender, length=len(text))
            return "BERHASIL DIKIRIM KE TELEGRAM CREATOR"
        return (
            "GAGAL MENGIRIM KE TELEGRAM (cek STUDIO_TELEGRAM_TOKEN / STUDIO_TELEGRAM_CHAT_ID "
            "atau TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"
        )

    def _youtube_insert(
        self,
        video: Path,
        title: str,
        description: str,
        tags: list[str],
        privacy: str,
        *,
        apply_thumbnail: bool = True,
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
        response = request.execute()
        response["_thumbnail_status"] = (
            self._apply_thumbnail(youtube, str(response.get("id") or ""))
            if apply_thumbnail
            else "skipped_short"
        )
        return response

    def _apply_thumbnail(
        self, youtube: Any, video_id: str, *, media_factory: Callable[[str], Any] | None = None
    ) -> str:
        """Pasang `output_ready/thumbnails/thumb_1.jpg` ke video; gagal ≠ upload gagal."""
        thumb = primary_thumbnail(self.settings)
        if not video_id or thumb is None:
            return "no_thumbnail"
        try:
            if media_factory is None:
                from googleapiclient.http import MediaFileUpload

                media = MediaFileUpload(str(thumb), mimetype="image/jpeg", resumable=False)
            else:
                media = media_factory(str(thumb))
            youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
        except Exception as exc:  # noqa: BLE001 - thumbnail opsional
            self._log("youtube_thumbnail", status="error", error=f"{type(exc).__name__}: {exc}")
            print(f"⚠️  Gagal memasang thumbnail: {type(exc).__name__}: {exc}", flush=True)
            return f"failed: {type(exc).__name__}"
        self._log("youtube_thumbnail", status="ok", video_id=video_id, file=thumb.name)
        return "set"

    # --------------------------------------------------------------- helper
    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()[:16]
