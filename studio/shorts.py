"""Pemotong YouTube Shorts vertikal 9:16 dari master 16:9.

Padanan `kra_shorts_generator.py` (spec Creator) di dalam paket studio.

Teknik **Aesthetic Blurred Background Stack**: latar memakai salinan master yang
di-*scale-crop* penuh ke 1080x1920 lalu di-blur, sementara video utama di-*scale*
utuh (tidak ada bagian yang terpotong) dan diletakkan di tengah.

FFmpeg saja — tanpa dependensi Python baru. Kalau ffmpeg tidak ada, fungsi
mengembalikan laporan `SKIP_TANPA_FFMPEG` (jujur), bukan mengaku sukses.

    >>> report = generate_youtube_short(settings, duration=45)
    >>> report.status
    'SHORTS_READY'
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings

SHORTS_DIRNAME = "shorts"
MANIFEST_FILENAME = "shorts_published.json"        # di workspace/: jejak upload Shorts
RECEIPT_FILENAME = "shorts_publish_receipt.json"   # di output_ready/: receipt terakhir
HASHTAG = "#Shorts"
DEFAULT_MASTER = "final_render.mp4"
DEFAULT_DURATION = 45
MIN_DURATION = 30
MAX_DURATION = 58  # batas YouTube Shorts lama; 60 detik masih diterima
SHORT_WIDTH = 1080
SHORT_HEIGHT = 1920
SHORT_TIMEOUT = 900

Runner = Callable[..., Any]


@dataclass
class ShortsReport:
    status: str = "SHORTS_GAGAL"
    output: Path | None = None
    master: Path | None = None
    duration: int = DEFAULT_DURATION
    size_mb: float = 0.0
    command: list[str] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "SHORTS_READY"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "file": self.output.name if self.output else "",
            "disk_file": str(self.output) if self.output else "",
            "master": self.master.name if self.master else "",
            "duration_seconds": self.duration,
            "size_mb": self.size_mb,
            "resolution": f"{SHORT_WIDTH}x{SHORT_HEIGHT}",
            "error": self.error,
            "notes": self.notes,
            "elapsed_seconds": round(self.elapsed, 2),
        }


def _default_run(cmd: list[str], *, timeout: float = SHORT_TIMEOUT) -> Any:
    import subprocess

    return subprocess.run(  # noqa: S603 - argumen dibangun sendiri, tanpa shell
        cmd, check=True, capture_output=True, timeout=timeout
    )


def build_filter_complex(blur: int = 30) -> str:
    """Rantai filter ffmpeg: latar blur penuh + video utama utuh di tengah."""
    return (
        f"[0:v]scale={SHORT_WIDTH}:{SHORT_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={SHORT_WIDTH}:{SHORT_HEIGHT},boxblur={blur}:{blur}[bg];"
        f"[0:v]scale={SHORT_WIDTH}:{SHORT_HEIGHT}:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
    )


def build_command(
    master: Path,
    output: Path,
    *,
    duration: int = DEFAULT_DURATION,
    crf: int = 19,
    blur: int = 30,
) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(master),
        "-t",
        str(int(duration)),
        "-filter_complex",
        build_filter_complex(blur),
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(int(crf)),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output),
    ]


def _clamp(duration: int) -> int:
    return max(MIN_DURATION, min(MAX_DURATION, int(duration)))


def generate_youtube_short(
    settings: Settings,
    *,
    master: str = DEFAULT_MASTER,
    duration: int = DEFAULT_DURATION,
    output_name: str = "",
    run: Runner | None = None,
) -> ShortsReport:
    """Potong ``duration`` detik awal master → Shorts vertikal 1080x1920."""
    if not settings.shorts_enabled:
        return ShortsReport(status="DIMATIKAN", notes=["STUDIO_SHORTS=0 → Shorts dilewati"])

    started = time.monotonic()
    output_dir = settings.paths.output_ready / SHORTS_DIRNAME
    master_path = Path(master)
    if not master_path.is_absolute():
        master_path = settings.paths.output_ready / master_path.name
    if not master_path.is_file() or master_path.stat().st_size == 0:
        return ShortsReport(
            status="SHORTS_GAGAL",
            master=master_path,
            error=f"master '{master_path.name}' tidak ada di {settings.paths.output_ready}",
            elapsed=time.monotonic() - started,
        )
    if shutil.which("ffmpeg") is None and run is None:
        return ShortsReport(
            status="SKIP_TANPA_FFMPEG",
            master=master_path,
            notes=["ffmpeg tidak ada di PATH; Shorts butuh ffmpeg"],
            elapsed=time.monotonic() - started,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    name = Path(output_name).name or f"short_{int(time.time())}.mp4"
    if not name.startswith("short_"):
        name = f"short_{name}"
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    output = output_dir / name

    seconds = _clamp(duration)
    command = build_command(master_path, output, duration=seconds, crf=settings.render_crf)
    runner = run or _default_run
    try:
        runner(command)
    except Exception as exc:  # noqa: BLE001 - ffmpeg gagal tidak boleh mematikan pipeline
        stderr = getattr(exc, "stderr", b"") or b""
        detail = stderr.decode("utf-8", errors="ignore") if isinstance(stderr, bytes) else str(stderr)
        return ShortsReport(
            status="SHORTS_GAGAL",
            master=master_path,
            output=output,
            duration=seconds,
            command=command,
            error=f"{type(exc).__name__}: {(detail or str(exc))[-400:]}",
            elapsed=time.monotonic() - started,
        )

    if not output.is_file() or output.stat().st_size == 0:
        return ShortsReport(
            status="SHORTS_GAGAL",
            master=master_path,
            output=output,
            duration=seconds,
            command=command,
            error="ffmpeg selesai tetapi file output tidak terbentuk",
            elapsed=time.monotonic() - started,
        )

    return ShortsReport(
        status="SHORTS_READY",
        output=output,
        master=master_path,
        duration=seconds,
        size_mb=round(output.stat().st_size / (1024 * 1024), 2),
        command=command,
        elapsed=time.monotonic() - started,
    )


# --------------------------------------------------------------- katalog Shorts
def shorts_dir(settings: Settings) -> Path:
    return settings.paths.output_ready / SHORTS_DIRNAME


def latest_short(settings: Settings) -> Path | None:
    """File `.mp4` terbaru di `output_ready/shorts/` (atau ``None``)."""
    folder = shorts_dir(settings)
    if not folder.is_dir():
        return None
    files = [
        path
        for path in folder.glob("*.mp4")
        if path.is_file() and path.stat().st_size > 0
    ]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def manifest_path(settings: Settings) -> Path:
    return settings.paths.workspace / MANIFEST_FILENAME


def load_manifest(settings: Settings) -> dict[str, Any]:
    path = manifest_path(settings)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def published_record(settings: Settings, short: Path) -> dict[str, Any] | None:
    """Catatan upload sebelumnya untuk file ini (idempotensi cron 17:00)."""
    record = load_manifest(settings).get(short.name)
    if not isinstance(record, dict):
        return None
    if int(record.get("size_bytes", -1)) != short.stat().st_size:
        return None  # file berubah → anggap versi baru
    return record


def record_publish(
    settings: Settings,
    short: Path,
    *,
    status: str,
    title: str,
    video_id: str = "",
    url: str = "",
    privacy: str = "unlisted",
) -> dict[str, Any]:
    manifest = load_manifest(settings)
    record = {
        "file": short.name,
        "size_bytes": short.stat().st_size,
        "status": status,
        "title": title,
        "video_id": video_id,
        "url": url,
        "privacy": privacy,
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    manifest[short.name] = record
    path = manifest_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


def ensure_shorts_hashtag(title: str, *, limit: int = 100) -> str:
    """Pastikan judul memuat `#Shorts` dan tidak melebihi batas YouTube."""
    clean = " ".join(str(title or "").split())
    if not clean:
        clean = "Fakta Menarik AI"
    if HASHTAG.lower() not in clean.lower():
        clean = f"{clean} {HASHTAG}"
    if len(clean) > limit:
        keep = clean[:limit].rstrip()
        if HASHTAG.lower() not in keep.lower():
            keep = keep[: limit - len(HASHTAG) - 1].rstrip() + " " + HASHTAG
        clean = keep
    return clean


def default_short_title(settings: Settings) -> str:
    """Judul Shorts dari brief Vito → judul episode terakhir → fallback jujur."""
    brief_path = settings.paths.workspace / "content_brief.json"
    if brief_path.is_file():
        try:
            brief = json.loads(brief_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            brief = {}
        for key in ("titles", "title_options"):
            items = brief.get(key) or []
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and item.get("title"):
                    return ensure_shorts_hashtag(str(item["title"]))
                if isinstance(item, str) and item.strip():
                    return ensure_shorts_hashtag(item)
        if brief.get("title"):
            return ensure_shorts_hashtag(str(brief["title"]))
    try:
        from .memory import MemoryStore

        memory = MemoryStore(settings.memory_db_path)
        episodes = memory.recent_episodes(1)
        if episodes and episodes[0].get("primary_title"):
            return ensure_shorts_hashtag(str(episodes[0]["primary_title"]))
    except Exception:  # noqa: BLE001 - judul default tidak boleh mematikan publish
        pass
    return ensure_shorts_hashtag("Fakta Menarik AI")


def default_short_description(title: str, *, duration: int = DEFAULT_DURATION) -> str:
    clean = str(title or "").replace(HASHTAG, "").strip()
    return (
        f"{clean}\n\n"
        f"Potongan {int(duration)} detik dari kantor virtual AI otonom KRA: enam agent "
        "menyusun naskah, merender, dan mengaudit sendiri.\n\n"
        "#shorts #ai #teknologi"
    )


def format_publish_caption(report: dict[str, Any]) -> str:
    status = report.get("status", "")
    title = report.get("title", "")
    if status == "UPLOADED_LIVE":
        return (
            "📱 <b>[KRA SHORTS PUBLISHED]</b>\n"
            f"<b>{title}</b>\n"
            f"🔗 {report.get('url')}\n"
            f"Privasi: {report.get('privacy')} · file: <code>{report.get('file')}</code>"
        )
    if status == "UPLOADED_SIMULATION":
        return (
            "📱 <b>[KRA SHORTS]</b> UPLOAD SIMULASI (BUKAN LIVE)\n"
            f"<b>{title}</b>\n"
            "client_secret.json belum ada, jadi tidak ada video yang benar-benar tayang."
        )
    if status == "SUDAH_TAYANG":
        return (
            "☑️ Shorts terbaru sudah pernah dipublikasikan "
            f"(<code>{report.get('file')}</code>, {report.get('published_at', '-')}). "
            "Tidak di-upload ulang."
        )
    if status == "TIDAK_ADA_SHORTS":
        return "📭 Belum ada file Shorts di <code>output_ready/shorts/</code>."
    return f"⚠️ <b>[KRA SHORTS]</b> {status}: {report.get('error', '')}"


def format_shorts_caption(report: ShortsReport) -> str:
    if report.ok:
        return (
            "📱 <b>[KRA SHORTS]</b> Shorts vertikal siap: "
            f"<code>{report.output.name}</code> ({report.size_mb} MB, {report.duration}s)\n"
            f"Lokasi: <code>output_ready/{SHORTS_DIRNAME}/</code> — upload manual atau lewat Sora."
        )
    if report.status == "DIMATIKAN":
        return "📴 Shorts dilewati (STUDIO_SHORTS=0)."
    return f"⚠️ <b>[KRA SHORTS]</b> {report.status}: {report.error or '; '.join(report.notes)}"
