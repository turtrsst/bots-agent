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
    parser.add_argument("--doctor", action="store_true", help="Cek konfigurasi lalu keluar.")
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
    import shutil

    print(f"  ffmpeg        : {shutil.which('ffmpeg') or 'tidak ada (render simulasi)'}")
    print(f"  client_secret : {'ada' if (settings.paths.root / 'client_secret.json').exists() else 'tidak ada (upload simulasi)'}")
    if not settings.api_key:
        print("\n⚠️  API key belum diset — mode LLM tidak bisa jalan, pakai --rehearsal.")
    return 0 if ok else 1


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
    settings.paths.ensure()

    if args.doctor:
        return doctor(settings)

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
        office = build_office(settings)
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
    if not summary.completed:
        print("⚠️  Rapat berhenti sebelum PIPELINE_COMPLETE. Cek transkrip untuk melihat titik macetnya.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
