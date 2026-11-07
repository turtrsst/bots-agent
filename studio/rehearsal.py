"""Rehearsal (gladi bersih) tanpa LLM.

Menjalankan urutan 7 langkah yang sama dengan rapat otonom, tetapi "otak" bot
diganti template deterministik. Tujuannya:

* memvalidasi tool, struktur folder, render, audit, dan upload tanpa biaya API;
* memberi pengguna gambaran alur sebelum menyalakan LLM;
* menjadi jalur uji end-to-end di CI (``pytest``).

Yang **tidak** diuji di sini adalah pemilihan pembicara oleh LLM dan kualitas
naskah — itu tetap butuh model sungguhan.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from typing import Any

from .config import Settings
from .office import RunSummary
from .report import collect_files, write_run_report
from .tools import StudioTools

MARKER_A_ROLL = "[CREATOR A-ROLL]"
MARKER_B_ROLL = "[AI B-ROLL]"
BANNED_PHRASES = ("halo guys", "hai guys", "di video kali ini")


def _say(transcript: list[dict[str, Any]], name: str, content: str) -> None:
    transcript.append({"role": "assistant", "name": name, "content": content})
    first, *rest = content.strip().splitlines() or [""]
    print(f"  ▸ {name}: {first}", flush=True)
    for line in rest[:4]:
        if line.strip():
            print(f"    {line}", flush=True)
    if len(rest) > 4:
        print("    …", flush=True)


def _topic_core(topic: str) -> str:
    core = re.sub(r"\s+", " ", (topic or "").strip().strip(".!"))
    if not core:
        core = "Cara membangun tim AI Agent untuk mempercepat kerja Content Creator"
    return core[:1].upper() + core[1:]


def _titles(topic: str) -> list[str]:
    core = _topic_core(topic)
    suffixes = [
        " — Ini yang Tidak Dibilang Orang",
        ": 3 Kesalahan yang Harus Dihindari",
        " Tanpa Buang Waktu",
    ]
    titles: list[str] = []
    for suffix in suffixes:
        budget = 55 - len(suffix)
        if len(core) <= budget:
            trimmed = core
        else:
            cut = core[: budget - 1]
            trimmed = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip() + "…"
        titles.append(trimmed + suffix)
    return titles


def build_brief(topic: str) -> dict[str, Any]:
    core = _topic_core(topic)
    titles = _titles(topic)
    return {
        "topic": core,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "rehearsal-template (tanpa LLM)",
        "titles": [
            {"title": title, "chars": len(title), "hook": hook}
            for title, hook in zip(
                titles,
                [
                    "Rasa penasaran + rasa rugi (loss aversion)",
                    "Daftar kesalahan yang relatable",
                    "Janji efisiensi waktu yang konkret",
                ],
                strict=True,
            )
        ],
        "thumbnails": [
            {"concept": "Wajah close-up + teks 3 kata di kiri atas", "emotion": "kaget"},
            {"concept": "Split screen 'cara lama vs cara AI'", "emotion": "kontras"},
            {"concept": "Screenshot workflow + panah merah", "emotion": "penasaran"},
        ],
        "audience": "Content creator solo yang produksi video mingguan dan mulai kewalahan",
        "keywords": ["ai agent", "produksi video", "otomasi konten", "workflow creator"],
        "promise": f"Penonton paham langkah konkret soal {core.lower()} dalam 8 menit.",
    }


def build_script(topic: str) -> str:
    core = _topic_core(topic)
    return f"""# NASKAH PRODUKSI — {core}

Target durasi: 7–9 menit | Format: talking head + b-roll
Penanda: {MARKER_A_ROLL} = direkam Creator (manusia), {MARKER_B_ROLL} = visual AI/b-roll.

## 00:00–00:14 — HOOK
{MARKER_A_ROLL} "Kalau kamu masih mengerjakan {core.lower()} sendirian, kamu membuang 6 jam per video."
{MARKER_B_ROLL} Montase timeline editing penuh, jam berputar cepat, teks "6 JAM → 40 MENIT".

## 00:14–00:40 — JANJI & KONTEKS
{MARKER_A_ROLL} "Dalam 8 menit saya bedah alur kerja yang saya pakai: riset, naskah, produksi, publish."
{MARKER_B_ROLL} Animasi 4 ikon tahap produksi muncul satu per satu.

## 00:40–02:30 — MASALAH UTAMA
{MARKER_A_ROLL} "Masalahnya bukan kurang ide. Masalahnya kamu jadi penulis, editor, dan publisher sekaligus."
{MARKER_B_ROLL} Rekaman layar tab browser menumpuk, kursor pindah-pindah kacau.

## 02:30–04:30 — SOLUSI 1: RISET & NASKAH
{MARKER_A_ROLL} "Saya pisahkan riset dan naskah. Brief dulu, baru naskah — jangan dibalik."
{MARKER_B_ROLL} Grafik sederhana 'brief → naskah → gatekeeper'.

## 04:30–06:15 — SOLUSI 2: PRODUKSI & QC
{MARKER_A_ROLL} "Setiap output diperiksa bot lain sebelum lanjut. Tidak ada file lolos tanpa bukti."
{MARKER_B_ROLL} Ilustrasi checklist audit dengan centang hijau.

## 06:15–07:30 — BUKTI / STUDI KASUS
{MARKER_A_ROLL} "Minggu lalu saya uji ini untuk satu video: waktu editing turun dari 5 jam ke 55 menit."
{MARKER_B_ROLL} Grafik before/after batang waktu.

## 07:30–08:10 — CTA
{MARKER_A_ROLL} "Kalau kamu mau template alur ini, komentar 'WORKFLOW'. Saya kirim versi teksnya."
{MARKER_B_ROLL} End screen: video terkait + tombol subscribe.
"""


def audit_script(script: str) -> list[str]:
    """Checklist Damon untuk gerbang 1 (deterministik di mode rehearsal)."""
    problems: list[str] = []
    lowered = script.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            problems.append(f"ada basa-basi '{phrase}'")
    if MARKER_A_ROLL not in script or MARKER_B_ROLL not in script:
        problems.append("penanda [CREATOR A-ROLL]/[AI B-ROLL] tidak lengkap")
    if not re.search(r"00:0\d", script):
        problems.append("tidak ada timecode hook di 15 detik pertama")
    if "cta" not in lowered:
        problems.append("tidak ada CTA")
    return problems


def build_description(topic: str) -> str:
    core = _topic_core(topic)
    return (
        f"{core} — bedah alur kerja lengkap dalam 8 menit.\n\n"
        "CHAPTERS\n"
        "00:00 Hook\n"
        "00:14 Janji & konteks\n"
        "00:40 Masalah utama\n"
        "02:30 Solusi 1: riset & naskah\n"
        "04:30 Solusi 2: produksi & QC\n"
        "06:15 Studi kasus\n"
        "07:30 CTA\n\n"
        "Template alur kerja: tulis 'WORKFLOW' di komentar.\n"
        "#aiproductivity #contentcreator #workflow"
    )


def run_rehearsal(settings: Settings, topic: str) -> RunSummary:
    """Jalankan pipeline deterministik memakai tool yang sama dengan bot asli."""
    settings = replace(settings, mock_creator=True)
    tools = StudioTools(settings)
    transcript: list[dict[str, Any]] = []
    started = time.monotonic()

    print("🎬 MODE REHEARSAL — tanpa LLM, memakai tool yang sama dengan bot asli.\n", flush=True)

    # 1. Vito
    brief = build_brief(topic)
    _say(
        transcript,
        "Vito_TrendStrategist",
        "Brief siap: 3 judul (<55 karakter), 3 konsep thumbnail, dan angle 'loss aversion'.",
    )
    print("    " + tools.save_document("content_brief.json", json.dumps(brief, indent=2, ensure_ascii=False)))
    _say(transcript, "Vito_TrendStrategist", "Clara_Scriptwriter, brief di ./workspace/content_brief.json sudah final.")

    # 2. Clara
    script = build_script(topic)
    print("    " + tools.save_document("script_production.md", script))
    _say(
        transcript,
        "Clara_Scriptwriter",
        "Naskah 8 menit selesai, hook 14 detik, A-Roll/B-Roll ditandai. Damon, mohon audit.",
    )

    # 3. Damon — gerbang 1
    problems = audit_script(script)
    if problems:
        _say(transcript, "Damon_RealityChecker", "SCRIPT REJECT: " + "; ".join(problems))
    else:
        _say(
            transcript,
            "Damon_RealityChecker",
            "SCRIPT PASS: tanpa sapaan basa-basi, hook 14 detik, penanda A-Roll/B-Roll lengkap, CTA ada. "
            "Bima_StudioPM, tagih rekaman Creator.",
        )

    # 4. Bima — human-in-the-loop
    upload_status = tools.request_and_check_creator_files(
        required_files_info=(
            "## Checklist rekaman\n"
            "- [ ] A-Roll talking head semua segmen [CREATOR A-ROLL]\n"
            "- [ ] 2 b-roll tambahan (layar kerja, hasil akhir)\n"
            "- [ ] Audio bersih, tanpa klip"
        ),
        expected_filename="a_roll_raw.mp4",
    )
    print(f"    {upload_status[:300]}")
    _say(transcript, "Bima_StudioPM", f"{upload_status[:200]} Raka_Producer, silakan render.")

    # 5. Raka
    render_status = tools.assemble_and_render_video("a_roll_raw.mp4", "final_render.mp4")
    print(f"    {render_status[:300]}")
    _say(transcript, "Raka_Producer", "Render selesai, bukti di ./output_ready/. Damon, audit dong.")

    # 6. Damon — gerbang 2
    audit = tools.inspect_rendered_output("final_render.mp4")
    print(f"    {audit[:300]}")
    passed = audit.startswith("AUDIT LULUS")
    _say(
        transcript,
        "Damon_RealityChecker",
        f"{'VIDEO PASS' if passed else 'VIDEO FAIL'}: {audit[:180]}",
    )

    # 7. Sora — hanya kalau Damon memberi VIDEO PASS
    if not passed:
        _say(transcript, "Sora_YouTubePublisher", "Saya tidak mengunggah: belum ada VIDEO PASS dari Damon.")
        upload = "UPLOAD DIBATALKAN: VIDEO FAIL"
    else:
        upload = tools.upload_to_youtube(
            video_filename="final_render.mp4",
            title=brief["titles"][0]["title"],
            description=build_description(topic),
            tags=["ai agent", "content creator", "otomasi konten", "workflow video", "produktivitas"],
            privacy_status="unlisted",
        )
    print(f"    {upload[:300]}")
    _say(transcript, "Sora_YouTubePublisher", upload[:400])
    if passed:
        _say(transcript, "Sora_YouTubePublisher", "Metadata SEO + chapters terpasang. PIPELINE_COMPLETE")

    elapsed = time.monotonic() - started
    summary = RunSummary(
        topic=_topic_core(topic),
        completed=passed,
        rounds=len(transcript),
        elapsed_seconds=elapsed,
        files=collect_files(settings.paths),
        youtube_url=_url_from_upload(upload),
        messages=transcript,
    )
    summary.transcript_path = write_run_report(
        settings.paths, summary, f"{settings.model} (rehearsal, tanpa LLM)"
    )
    return summary


def _url_from_upload(payload: str) -> str:
    match = re.search(r"https://youtu\.be/[\w-]+", payload)
    if match:
        return match.group(0)
    if "UPLOADED_SIMULATION" in payload:
        return "SIMULASI (belum ada client_secret.json)"
    return ""
