"""Self-healing tool executor: retry, diagnosa error, dan laporan fatal.

Padanan `kra_error_handler.py` (spec Creator) di dalam paket studio. Tiga bagian:

1. :func:`classify_error` — mengubah exception/pesan menjadi kategori yang bisa
   ditindaklanjuti (memori render penuh, file korup, kuota API, token kedaluwarsa,
   jaringan, ffmpeg, remotion, tidak dikenal) beserta saran fallback;
2. :func:`resilient_tool` — dekorator auto-retry dengan jeda bertahap (backoff);
3. :func:`call_with_resilience` — dipakai `StudioTools.build_functions()` supaya
   **tidak ada** exception tool yang bisa mematikan GroupChat. Tool yang aman
   diulang (render, TTS, B-Roll, riset, thumbnail) dicoba hingga 3×; tool yang
   tidak idempoten (upload, simpan file, kirim pesan) hanya sekali.

Saat semua percobaan habis, tool mengembalikan string
``DIAGNOSTIC CRITICAL FAILURE: …`` + kode kategori, dan laporan HTML dikirim ke
Creator. Persona Damon memakai string itu sebagai sinyal untuk memerintahkan
Clara memperbaiki naskah atau Raka memakai metode render alternatif.
"""

from __future__ import annotations

import functools
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_RETRIES = 3
DEFAULT_DELAY = 4.0
MAX_DELAY = 30.0

CRITICAL_PREFIX = "DIAGNOSTIC CRITICAL FAILURE"
RETRY_PREFIX = "DIAGNOSTIC RETRY"


@dataclass(frozen=True)
class Diagnosis:
    category: str
    label: str
    advice: str


#: Kategori → (label manusia, fallback yang boleh diperintahkan Damon).
ERROR_PLAYBOOK: dict[str, Diagnosis] = {
    "MEMORI_RENDER_PENUH": Diagnosis(
        "MEMORI_RENDER_PENUH",
        "Render kehabisan memori",
        "Turunkan fps/resolusi sementara, render per-babak, atau pakai fallback concat FFmpeg.",
    ),
    "FILE_KORUP": Diagnosis(
        "FILE_KORUP",
        "File input rusak / 0 byte",
        "Minta Bima memeriksa ulang A-Roll, atau lanjutkan mode faceless (TTS).",
    ),
    "KUOTA_API": Diagnosis(
        "KUOTA_API",
        "Kuota / rate limit API habis",
        "Tunda panggilan, pakai sumber tanpa key (RSS/yt-dlp/Flux), lalu ulangi nanti.",
    ),
    "TOKEN_KEDALUWARSA": Diagnosis(
        "TOKEN_KEDALUWARSA",
        "Kredensial OAuth/API kedaluwarsa",
        "Hapus token.json lalu otorisasi ulang, atau jalankan upload simulasi sampai token siap.",
    ),
    "JARINGAN": Diagnosis(
        "JARINGAN",
        "Jaringan tidak tersedia",
        "Ulangi dengan backoff; bila tetap gagal, pakai aset cache lokal.",
    ),
    "FFMPEG": Diagnosis(
        "FFMPEG",
        "FFmpeg gagal memproses media",
        "Periksa stderr terakhir; coba filter lebih sederhana atau ganti container input.",
    ),
    "REMOTION": Diagnosis(
        "REMOTION",
        "Remotion/node gagal me-render",
        "Render ulang tanpa komposisi Remotion: pakai slide FFmpeg (fallback story builder).",
    ),
    "TIDAK_DIKENAL": Diagnosis(
        "TIDAK_DIKENAL",
        "Penyebab belum dikenali",
        "Minta Raka mengulang dengan parameter lebih sederhana; simpan log untuk audit.",
    ),
}

_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # urutan penting: kategori paling spesifik lebih dulu
    (
        "MEMORI_RENDER_PENUH",
        ("out of memory", "cannot allocate memory", "std::bad_alloc", "killed", "heap limit"),
    ),
    (
        "TOKEN_KEDALUWARSA",
        ("invalid_grant", "invalid token", "token has been expired", "unauthorized", "401", "403"),
    ),
    (
        "KUOTA_API",
        ("quota", "rate limit", "429", "too many requests", "resource_exhausted"),
    ),
    ("REMOTION", ("remotion", "chromium", "puppeteer", "composition")),
    ("FFMPEG", ("ffmpeg", "ffprobe", "filter_complex", "libx264", "encoder")),
    (
        "FILE_KORUP",
        ("no such file", "invalid data", "moov atom", "corrupt", "0 byte", "file korup"),
    ),
    ("JARINGAN", ("connection", "timed out", "timeout", "network", "urlerror", "dns")),
)

#: Tool yang aman diulang: murni komputasi / baca data, tidak mengubah katalog.
RETRY_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "research_current_trends",
        "scan_viral_trends_and_youtube_outliers",
        "assemble_and_render_video",
        "assemble_story_from_script",
        "inspect_rendered_output",
        "get_recent_content_memory",
        "check_topic_similarity_in_memory",
    }
)

#: Tool dengan efek samping: retry bisa menggandakan upload/pesan/tulisan.
NO_RETRY_TOOLS: frozenset[str] = frozenset(
    {
        "upload_to_youtube",
        "upload_youtube_short",
        "send_direct_alert_to_creator",
        "save_content_brief",
        "save_production_script",
        "save_scene_breakdown",
        "request_and_check_creator_files",
        "register_new_episode_to_memory",
    }
)


def classify_error(error: Any) -> Diagnosis:
    """Petakan exception/pesan error ke satu kategori playbook."""
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    lowered = text.lower()
    for category, needles in _KEYWORDS:
        if any(needle in lowered for needle in needles):
            return ERROR_PLAYBOOK[category]
    return ERROR_PLAYBOOK["TIDAK_DIKENAL"]


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def build_diagnostic(
    tool_name: str,
    error: BaseException,
    *,
    attempts: int,
    trace: str = "",
) -> str:
    """Laporan HTML (Telegram) untuk kegagalan fatal satu tool."""
    diagnosis = classify_error(error)
    message = _tail(f"{type(error).__name__}: {error}", 300)
    last_frame = ""
    for line in reversed((trace or "").strip().splitlines()):
        if line.strip().startswith("File "):
            last_frame = _tail(line.strip(), 160)
            break
    lines = [
        "❌ <b>[KRA AUTO-HEALING GAGAL]</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🛠️ <b>Tool:</b> <code>{tool_name}</code>",
        f"🔄 <b>Percobaan:</b> {attempts}×",
        f"🧭 <b>Kategori:</b> <code>{diagnosis.category}</code> — {diagnosis.label}",
        f"🚨 <b>Error:</b> <code>{message}</code>",
    ]
    if last_frame:
        lines.append(f"📍 <b>Lokasi:</b> <code>{last_frame}</code>")
    lines.append(f"🩹 <b>Saran fallback:</b> {diagnosis.advice}")
    lines.append("")
    lines.append(
        "<i>Pipeline dihentikan untuk tugas ini agar tidak merusak file. "
        "Damon memakai kode kategori di atas untuk memilih perbaikan.</i>"
    )
    return "\n".join(lines)


def critical_message(tool_name: str, error: BaseException, attempts: int) -> str:
    """String yang dikembalikan ke LLM saat semua percobaan gagal."""
    diagnosis = classify_error(error)
    return (
        f"{CRITICAL_PREFIX}: Gagal total pada {tool_name} setelah {attempts} percobaan. "
        f"KATEGORI={diagnosis.category} ({diagnosis.label}). "
        f"Error: {_tail(f'{type(error).__name__}: {error}', 300)}. "
        f"Saran: {diagnosis.advice} "
        "Laporkan ke Damon (Reality Checker) — jangan klaim tugas ini selesai."
    )


def _delay_for(attempt: int, base: float) -> float:
    return min(MAX_DELAY, max(0.0, base) * (2 ** (attempt - 1)))


def call_with_resilience(
    tool_name: str,
    func: Callable[..., Any],
    *args: Any,
    max_retries: int = DEFAULT_MAX_RETRIES,
    delay_seconds: float = DEFAULT_DELAY,
    notifier: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = None,
    retry: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Jalankan ``func`` dengan retry/backoff + diagnosa; tidak pernah melempar exception.

    ``retry=None`` menebak dari nama tool (lihat :data:`RETRY_SAFE_TOOLS`).
    """
    if retry is None:
        retryable = tool_name in RETRY_SAFE_TOOLS and tool_name not in NO_RETRY_TOOLS
    else:
        retryable = bool(retry)
    attempts_allowed = max(1, int(max_retries)) if retryable else 1
    report = log or (lambda message: print(message, flush=True))

    attempt = 0
    last_error: BaseException | None = None
    trace = ""
    while attempt < attempts_allowed:
        attempt += 1
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - semua error tool harus jadi pesan
            last_error = exc
            trace = traceback.format_exc()
            diagnosis = classify_error(exc)
            report(
                f"⚠️  [KRA RESILIENCE] {tool_name} gagal "
                f"(percobaan {attempt}/{attempts_allowed}) [{diagnosis.category}]: "
                f"{type(exc).__name__}: {exc}"
            )
            if attempt < attempts_allowed:
                sleep(_delay_for(attempt, delay_seconds))

    assert last_error is not None  # hanya tercapai bila ada exception
    fatal = build_diagnostic(tool_name, last_error, attempts=attempt, trace=trace)
    if notifier is not None:
        sender = getattr(notifier, "send", None)
        if callable(sender):
            try:
                sender(fatal)
            except Exception as exc:  # noqa: BLE001 - notifikasi tidak boleh melempar
                report(f"⚠️  Gagal mengirim laporan diagnosa: {type(exc).__name__}: {exc}")
    return critical_message(tool_name, last_error, attempt)


def resilient_tool(
    max_retries: int = DEFAULT_MAX_RETRIES,
    delay_seconds: float = DEFAULT_DELAY,
    *,
    notifier: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    name: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Dekorator auto-retry untuk fungsi mandiri (tanpa instance StudioTools).

    >>> @resilient_tool(max_retries=3, delay_seconds=0, sleep=lambda _s: None)
    ... def flaky() -> str:
    ...     return "ok"
    >>> flaky()
    'ok'
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return call_with_resilience(
                name or func.__name__,
                func,
                *args,
                max_retries=max_retries,
                delay_seconds=delay_seconds,
                notifier=notifier,
                sleep=sleep,
                retry=True,
                **kwargs,
            )

        return wrapper

    return decorator
