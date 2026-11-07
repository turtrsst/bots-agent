"""Dry-run berperingkat: cek lingkungan, memori, media, notifikasi, dan API.

Padanan protokol uji manual spec Creator, tapi dijalankan satu perintah:

    python virtual_office_superbots.py --dry-run          # tanpa mengirim pesan
    python virtual_office_superbots.py --dry-run --send   # kirim 1 pesan uji Telegram

Yang diuji **nyata** (bukan diklaim): modul Python yang bisa diimpor, kapabilitas
ffmpeg/Whisper/Remotion, pembukaan SQLite + FTS5, mesin TTS/B-Roll yang terpilih,
kanal notifikasi, lalu seluruh endpoint dashboard/webhook lewat `TestClient`
in-process (tanpa membuka port, tanpa jaringan).
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from typing import Any

from .config import Settings

OK = "✅"
WARN = "⚠️"
FAIL = "❌"


@dataclass
class Check:
    name: str
    status: str
    detail: str

    @property
    def critical(self) -> bool:
        return self.status == FAIL


def _phase(title: str) -> None:
    print(f"\n{title}")
    print("─" * 68)


def _row(check: Check) -> None:
    print(f"  {check.status} {check.name:<26} {check.detail}")


def check_environment(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("python", OK, f"{platform.python_version()} ({sys.executable})"))

    try:
        import autogen

        checks.append(Check("pyautogen", OK, getattr(autogen, "__version__", "?")))
    except ImportError:
        checks.append(Check("pyautogen", FAIL, "belum terpasang (pip install -r requirements.txt)"))

    from .media import detect_capabilities

    caps = detect_capabilities(settings)
    checks.append(Check("ffmpeg", OK if caps.ffmpeg else WARN, caps.ffmpeg or "tidak ada (render simulasi)"))
    checks.append(Check("ffprobe", OK if caps.ffprobe else WARN, caps.ffprobe or "tidak ada"))
    checks.append(Check("whisper", OK if caps.whisper else WARN, "tersedia" if caps.whisper else "tidak ada"))
    remotion_ready = bool(caps.npx and settings.remotion_dir)
    checks.append(
        Check(
            "remotion",
            OK if remotion_ready else WARN,
            f"npx={caps.npx or '-'} dir={settings.remotion_dir or '-'}",
        )
    )
    checks.append(
        Check(
            "shorts",
            OK if (settings.shorts_enabled and caps.ffmpeg) else WARN,
            f"aktif {settings.shorts_duration}s" if settings.shorts_enabled else "STUDIO_SHORTS=0",
        )
    )
    return checks


def check_memory(settings: Settings) -> list[Check]:
    from .memory import SIMILARITY_THRESHOLD, MemoryStore, token_similarity

    try:
        memory = MemoryStore(settings.memory_db_path)
        stats = memory.stats()
    except Exception as exc:  # noqa: BLE001 - dry-run harus melaporkan, bukan crash
        return [Check("sqlite memori", FAIL, f"{type(exc).__name__}: {exc}")]

    checks = [
        Check(
            "sqlite memori",
            OK,
            f"{stats['runs']} rapat / {stats['episodes']} episode ({stats['db']})",
        ),
        Check(
            "katalog FTS5",
            OK if memory.fts else WARN,
            "aktif" if memory.fts else "tidak ada → pemindaian penuh (fitur tetap jalan)",
        ),
    ]
    score = token_similarity("kantor AI tanpa manusia", "kantor AI berjalan tanpa manusia")
    checks.append(
        Check(
            "skor kemiripan",
            OK if score >= SIMILARITY_THRESHOLD else FAIL,
            f"contoh duplikat = {score:.2f} (ambang {SIMILARITY_THRESHOLD})",
        )
    )
    return checks


def check_engines(settings: Settings) -> list[Check]:
    from .broll import resolve_provider as resolve_broll
    from .research import default_transport  # noqa: F401 - memastikan modul riset bisa diimpor
    from .thumbnail import primary_thumbnail
    from .tts import resolve_provider as resolve_tts

    radar = "YouTube Data API" if settings.youtube_api_key else "yt-dlp/RSS (tanpa key)"
    checks = [
        Check("mesin TTS", OK, f"{resolve_tts(settings)} (minta: {settings.tts_provider})"),
        Check("visual B-Roll", OK, resolve_broll(settings)),
        Check("radar tren", OK, radar),
    ]
    thumb = primary_thumbnail(settings)
    checks.append(
        Check(
            "thumbnail",
            OK if thumb else WARN,
            thumb.name if thumb else "belum dirender (Sora upload tanpa thumbnail)",
        )
    )
    return checks


def check_notifier(settings: Settings, *, send: bool = False) -> list[Check]:
    from .notify import build_notifier

    notifier = build_notifier(settings)
    name = type(notifier).__name__
    checks = [
        Check(
            "kanal notifikasi",
            OK if name != "NullNotifier" else WARN,
            f"{name} (mode {settings.notify})",
        )
    ]
    if send:
        text = "🧪 <b>[KRA DRY-RUN]</b> Uji koneksi Telegram berhasil."
        sender = getattr(notifier, "send_html", None) or getattr(notifier, "send", None)
        try:
            ok = bool(sender(text)) if callable(sender) else False
        except Exception as exc:  # noqa: BLE001
            ok = False
            checks.append(Check("kirim uji Telegram", FAIL, f"{type(exc).__name__}: {exc}"))
            return checks
        checks.append(
            Check(
                "kirim uji Telegram",
                OK if ok else WARN,
                "terkirim" if ok else "kanal tidak mengonfirmasi (cek token/chat id)",
            )
        )
    else:
        checks.append(Check("kirim uji Telegram", WARN, "dilewati — pakai --send untuk mengirim"))
    return checks


def check_api(settings: Settings) -> list[Check]:
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
        from fastapi.testclient import TestClient
    except ImportError:
        return [Check("API/dashboard", WARN, "fastapi/httpx belum terpasang (pip install -r requirements-daemon.txt)")]

    from .api import create_app
    from .daemon import Daemon
    from .memory import MemoryStore
    from .notify import NullNotifier

    checks: list[Check] = []
    daemon = Daemon(
        settings,
        notifier=NullNotifier(),
        memory=MemoryStore(settings.memory_db_path),
        office_factory=lambda *args, **kwargs: None,
    )
    app = create_app(daemon, autostart=False)
    try:
        with TestClient(app) as client:
            health = client.get("/health")
            checks.append(
                Check(
                    "GET /health",
                    OK if health.status_code == 200 else FAIL,
                    f"{health.status_code} daemon={health.json().get('daemon_running')}",
                )
            )
            page = client.get("/")
            ok_page = page.status_code == 200 and "KRA CONTROL CENTER" in page.text
            checks.append(
                Check(
                    "dashboard /",
                    OK if ok_page else FAIL,
                    f"{page.status_code}, {len(page.text) // 1024} KB",
                )
            )
            state = client.get("/api/state")
            checks.append(
                Check(
                    "GET /api/state",
                    OK if state.status_code == 200 else FAIL,
                    str(state.json().get("status")) if state.status_code == 200 else str(state.status_code),
                )
            )
            gate = client.get("/kra/schedule/2-check-gate")
            checks.append(
                Check(
                    "gate n100n",
                    OK if gate.status_code == 200 else FAIL,
                    f"ready_to_produce={gate.json().get('ready_to_produce')}"
                    if gate.status_code == 200
                    else str(gate.status_code),
                )
            )
            hook = client.post(
                "/kra/telegram-webhook",
                json={"message": {"chat": {"id": 1}, "text": "/help"}},
            )
            ok_hook = hook.status_code == 200 and hook.json().get("command") == "help"
            checks.append(
                Check(
                    "webhook /help",
                    OK if ok_hook else FAIL,
                    "dijawab" if ok_hook else f"{hook.status_code} {hook.text[:60]}",
                )
            )
            report = client.post("/kra/schedule/weekly-report?send=false")
            checks.append(
                Check(
                    "rekap mingguan",
                    OK if report.status_code == 200 else FAIL,
                    f"status={report.json().get('status')}" if report.status_code == 200 else str(report.status_code),
                )
            )
            shorts = client.post("/kra/schedule/publish-shorts")
            checks.append(
                Check(
                    "publish shorts",
                    OK if shorts.status_code == 200 else FAIL,
                    str(shorts.json().get("status")) if shorts.status_code == 200 else str(shorts.status_code),
                )
            )
    except Exception as exc:  # noqa: BLE001 - laporkan, jangan crash
        checks.append(Check("API/dashboard", FAIL, f"{type(exc).__name__}: {exc}"))
    return checks


def run_dry_run(settings: Settings, *, send: bool = False) -> int:
    """Jalankan semua fase; kembalikan exit code (0 = tidak ada kegagalan kritis)."""
    print("🧪 KRA DRY-RUN — validasi berperingkat (tanpa memanggil LLM)")
    print(f"   root: {settings.paths.root}")
    print(f"   {settings.describe()}")

    groups: list[tuple[str, list[Check]]] = []
    _phase("FASE 1 — Lingkungan & dependensi")
    groups.append(("lingkungan", check_environment(settings)))
    for check in groups[-1][1]:
        _row(check)

    _phase("FASE 2 — Memori episodik SQLite")
    groups.append(("memori", check_memory(settings)))
    for check in groups[-1][1]:
        _row(check)

    _phase("FASE 3 — Mesin riset, suara, visual, thumbnail")
    groups.append(("mesin", check_engines(settings)))
    for check in groups[-1][1]:
        _row(check)

    _phase("FASE 4 — Notifikasi")
    groups.append(("notifikasi", check_notifier(settings, send=send)))
    for check in groups[-1][1]:
        _row(check)

    _phase("FASE 5 — API, dashboard, webhook (in-process)")
    groups.append(("api", check_api(settings)))
    for check in groups[-1][1]:
        _row(check)

    checks = [check for _, items in groups for check in items]
    failures = [check for check in checks if check.critical]
    warnings = [check for check in checks if check.status == WARN]

    _phase("RINGKASAN")
    print(f"  {len(checks) - len(failures) - len(warnings)} OK · {len(warnings)} peringatan · {len(failures)} gagal")
    if failures:
        for check in failures:
            print(f"  {FAIL} {check.name}: {check.detail}")
        print("\n  Perbaiki kegagalan di atas sebelum menyerahkan ke scheduler n100n.")
        return 1
    print("\n  Sistem siap. Langkah berikut: --rehearsal (uji alur), lalu --serve / --daemon.")
    if warnings:
        print("  Peringatan wajar untuk mesin tanpa ffmpeg/remotion/Telegram — bukan penghalang.")
    return 0


def doctor_snapshot(settings: Settings) -> dict[str, Any]:
    """Ringkasan ringkas untuk `--status`/diagnostik lain (dipakai test)."""
    checks = check_environment(settings) + check_memory(settings) + check_engines(settings)
    return {
        "ok": not any(check.critical for check in checks),
        "checks": [{"name": check.name, "status": check.status, "detail": check.detail} for check in checks],
    }
