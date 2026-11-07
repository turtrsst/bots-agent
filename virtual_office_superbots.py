#!/usr/bin/env python3
"""Virtual Creator Office — 6 Super Bot AutoGen yang rapat, berdebat, dan eksekusi tool nyata.

Contoh:

    python virtual_office_superbots.py --rehearsal -t "Cara membangun tim AI Agent"
    python virtual_office_superbots.py -t "Workflow editing video 2026"

Tanpa argumen, topik ditanyakan interaktif seperti skrip aslinya.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

# ---------------------------------------------------------------- bootstrap
with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover - kosmetik Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_dotenv_safely(root: Path) -> None:
    env_file = root / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if env_file.exists():
        load_dotenv(env_file, override=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="virtual_office_superbots.py",
        description="Ruang rapat otonom 6 Super Bot (AutoGen GroupChat) untuk produksi video YouTube.",
    )
    parser.add_argument("-t", "--topic", help="Ide kasar / topik konten. Kosong = tanya interaktif.")
    parser.add_argument("--rehearsal", action="store_true", help="Gladi bersih tanpa LLM (tanpa biaya API).")
    parser.add_argument("--root", help="Folder studio (default: folder script ini).")
    parser.add_argument("--model", help="Model LLM (default: gpt-4o / STUDIO_MODEL).")
    parser.add_argument("--max-rounds", type=int, help="Batas putaran group chat (default 40).")
    parser.add_argument("--mock-creator", action="store_true", help="Buat file A-Roll dummy otomatis.")
    parser.add_argument("--real-render", action="store_true", help="Pakai ffmpeg asli (bukan file dummy).")
    parser.add_argument(
        "--faceless",
        action="store_true",
        help="Mode faceless: tanpa rekaman Creator, narasi memakai TTS (ElevenLabs/Edge).",
    )
    parser.add_argument("--doctor", action="store_true", help="Cek konfigurasi + kapabilitas media lalu keluar.")
    parser.add_argument("--status", action="store_true", help="Tampilkan antrean daemon + memori lalu keluar.")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Jalankan 24/7: scheduler (STUDIO_SCHEDULE), file watcher input_creator/, notifikasi Telegram.",
    )
    parser.add_argument("--serve", action="store_true", help="Jalankan bridge HTTP untuk webhook n100n.")
    parser.add_argument("--host", default="0.0.0.0", help="Host HTTP bridge (default 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8100, help="Port HTTP bridge (default 8100).")
    parser.add_argument("--schedule", help="Override jadwal ideasi, contoh: '07:00,16:00'.")
    return parser.parse_args(argv)


def prompt_topic() -> str:
    default = "Cara membangun tim AI Agent untuk mempercepat kerja Content Creator"
    if not sys.stdin or not getattr(sys.stdin, "isatty", lambda: False)():
        return default
    try:
        raw = input("Masukkan ide kasar / topik konten yang ingin dibahas tim bot Anda: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return raw or default


def print_banner(settings, rehearsal: bool) -> None:
    from studio.office import AGENT_SPECS

    print("\n" + "═" * 68)
    print("🚀 AUTONOMOUS VIRTUAL CREATOR OFFICE" + ("  [REHEARSAL / TANPA LLM]" if rehearsal else ""))
    print("═" * 68)
    print(f"Konfigurasi : {settings.describe()}")
    print(f"Studio root : {settings.paths.root}")
    print("\nPeserta rapat & tool yang dipegang:")
    for spec in AGENT_SPECS:
        tools = ", ".join(spec.tools) or "—"
        print(f"  • {spec.name:<24} {spec.role}")
        print(f"    └─ tool: {tools}")
    print("═" * 68 + "\n")


def print_summary(summary) -> None:
    print("\n" + "═" * 68)
    print(f"📋 STATUS AKHIR : {summary.status}")
    print(f"⏱️  Durasi       : {summary.elapsed_seconds:.1f} detik")
    print(f"💬 Pesan        : {summary.rounds}")
    print(f"▶️  YouTube      : {summary.youtube_url or '-'}")
    print(f"🧾 Transkrip    : ./{summary.transcript_path}")
    print("📂 Berkas:")
    for item in summary.files:
        print(f"   - {item}")
    print("═" * 68 + "\n")


def doctor(settings) -> int:
    ok = True
    print("🔍 Pemeriksaan konfigurasi\n")
    print(f"  root          : {settings.paths.root}")
    print(f"  model         : {settings.model}")
    print(f"  endpoint      : {settings.base_url or 'https://api.openai.com/v1'}")
    print(f"  api key       : {settings.masked_api_key()}")
    print(f"  persona files : {len(list(settings.paths.agents.glob('*.md')))} ditemukan")
    try:
        import autogen  # noqa: F401

        print(f"  pyautogen     : {autogen.__version__}")
    except ImportError:
        ok = False
        print("  pyautogen     : TIDAK TERPASANG (pip install -r requirements.txt)")
    try:
        import googleapiclient  # noqa: F401

        print("  google api    : tersedia")
    except ImportError:
        print("  google api    : tidak ada (upload YouTube akan simulasi)")
    from studio.daemon import parse_schedule
    from studio.media import detect_capabilities
    from studio.memory import MemoryStore

    caps = detect_capabilities(settings)
    print(f"  ffmpeg        : {caps.ffmpeg or 'tidak ada (render hanya simulasi)'}")
    print(f"  ffprobe       : {caps.ffprobe or 'tidak ada'}")
    print(f"  auto-editor   : {caps.auto_editor or 'tidak ada (potong jeda dilewati)'}")
    print(f"  whisper       : {'tersedia' if caps.whisper else 'tidak ada (subtitle dilewati)'}")
    print(f"  remotion      : {'siap' if (caps.npx and settings.remotion_dir) else 'tidak siap'}")
    print(f"  real render   : {'AKTIF' if settings.real_render else 'nonaktif (output simulasi)'}")
    print(f"  async gate    : {'AKTIF (tanpa input terminal)' if settings.async_gate else 'nonaktif (interaktif)'}")
    print(f"  mode produksi : {'faceless (TTS)' if not settings.require_creator_aroll else 'butuh A-Roll Creator'}")
    from studio.broll import resolve_provider as resolve_broll
    from studio.tts import resolve_provider

    print(f"  mesin TTS     : {resolve_provider(settings)} (permintaan: {settings.tts_provider})")
    print(f"  suara Edge    : {settings.edge_tts_voice} (rate {settings.tts_rate})")
    print(f"  visual B-Roll : {resolve_broll(settings)}" + (" (Pexels siap)" if settings.pexels_api_key else ""))
    thumbs = "aktif" if settings.thumbnails_enabled else "nonaktif"
    print(f"  thumbnail     : {thumbs} (Remotion Still 1280x720)")
    radar = "YouTube Data API" if settings.youtube_api_key else "yt-dlp fallback"
    print(f"  radar tren    : Google Trends RSS + Google News RSS + {radar}")
    print(f"  jadwal ideasi : {settings.schedule or 'nonaktif'}")
    print(f"  notifikasi    : {'telegram' if settings.telegram_token and settings.telegram_chat_id else 'konsol'}")
    print(f"  client_secret : {'ada' if (settings.paths.root / 'client_secret.json').exists() else 'tidak ada (upload simulasi)'}")
    try:
        stats = MemoryStore(settings.memory_db_path).stats()
        print(f"  memori        : {stats['runs']} rapat, {stats['videos']} video ({stats['db']})")
    except Exception as exc:  # noqa: BLE001 - doctor tidak boleh crash
        print(f"  memori        : GAGAL dibuka ({type(exc).__name__}: {exc})")
    if parse_schedule(settings.schedule) and not settings.api_key:
        print("  catatan       : jadwal aktif tetapi API key kosong → job akan gagal, isi .env")
    if not settings.api_key:
        print("\n⚠️  API key belum diset — mode LLM tidak bisa jalan, pakai --rehearsal.")
    return 0 if ok else 1


def print_status(settings) -> int:
    from studio.daemon import Daemon

    daemon = Daemon(settings)
    print(daemon.status_text())
    runs = daemon.memory.recent_runs(5)
    if runs:
        print("\n🧠 Rapat terakhir:")
        for run in runs:
            print(f"  - {run['created_at'][:16]} | {run['status']:<26} | {run['topic'][:70]}")
    else:
        print("\n🧠 Memori masih kosong — belum ada rapat yang tercatat.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    studio_root = Path(args.root).expanduser().resolve() if args.root else ROOT
    load_dotenv_safely(studio_root)

    from studio.config import Settings

    settings = Settings.from_env(
        root=studio_root,
        model=args.model,
        max_rounds=args.max_rounds,
        mock_creator=args.mock_creator or None,
        real_render=args.real_render or None,
    )
    if args.schedule is not None:
        settings = settings.with_changes(schedule=args.schedule)
    if args.faceless:
        settings = settings.with_changes(require_creator_aroll=False)
    settings.paths.ensure()

    if args.doctor:
        return doctor(settings)
    if args.status:
        return print_status(settings)

    if args.daemon or args.serve:
        return run_long_lived(settings, args)

    topic = args.topic or prompt_topic()
    print_banner(settings, args.rehearsal)

    if args.rehearsal:
        from studio.rehearsal import run_rehearsal

        summary = run_rehearsal(settings, topic)
        print_summary(summary)
        print("ℹ️  Rehearsal hanya menguji alur + tool. Untuk debat antar-bot sungguhan, isi API key")
        print("   lalu jalankan tanpa --rehearsal.\n")
        return 0

    from studio.office import build_office

    try:
        office = build_office(settings, require_creator_aroll=settings.require_creator_aroll)
    except RuntimeError as exc:
        print(f"❌ {exc}\n")
        return 2

    fallbacks = [
        office.agents[key].name
        for key, persona in office.persona_sources.items()
        if persona.fallback_used
    ]
    if fallbacks:
        print(f"⚠️  Persona fallback dipakai untuk: {', '.join(fallbacks)} (file .md belum ada).\n")

    try:
        summary = office.run(topic)
    except KeyboardInterrupt:
        print("\n⏹️  Rapat dihentikan manual oleh Creator.")
        return 130

    print_summary(summary)
    _notify_stage_result(settings, summary)
    if not summary.completed:
        print("⚠️  Rapat berhenti sebelum PIPELINE_COMPLETE. Cek transkrip untuk melihat titik macetnya.\n")
        return 1
    return 0


def _notify_stage_result(settings, summary) -> None:
    """Kirim laporan naskah/upload ke Telegram juga saat dijalankan manual (bukan daemon)."""
    from studio.notify import build_notifier, notify_script_ready, notify_youtube_uploaded

    notifier = build_notifier(settings)
    if summary.awaiting_creator or summary.completed:
        notify_script_ready(
            notifier, settings.paths, require_aroll=settings.require_creator_aroll
        )
    if summary.completed:
        notify_youtube_uploaded(notifier, settings.paths)


def run_long_lived(settings, args) -> int:
    """Mode 24/7: daemon scheduler/watcher atau bridge HTTP untuk n100n."""
    import asyncio

    from studio.daemon import Daemon

    # Di mode daemon tidak ada terminal: jangan pernah menunggu input().
    settings = settings.with_changes(async_gate=True)
    daemon = Daemon(settings)
    print_banner(settings, rehearsal=False)
    print(daemon.status_text() + "\n")

    if args.topic:
        job = daemon.submit_ideation(
            args.topic, source="cli", require_aroll=settings.require_creator_aroll
        )
        print(f"🧠 Job ideasi awal diantrekan: {job.id}\n")

    if args.serve:
        from studio.api import run_server

        print(f"🌐 Bridge HTTP di http://{args.host}:{args.port}  → POST /kra/n100n-trigger\n")
        try:
            run_server(daemon, args.host, args.port)
        except KeyboardInterrupt:
            print("\n⏹️  Bridge dihentikan.")
            return 130
        return 0

    print("🤖 Daemon jalan. Ctrl-C untuk berhenti. Semua kabar dikirim lewat notifikasi.\n")
    try:
        asyncio.run(daemon.run_forever())
    except KeyboardInterrupt:
        print("\n⏹️  Daemon dihentikan manual.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
