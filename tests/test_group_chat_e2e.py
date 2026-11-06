"""Uji end-to-end GroupChat dengan LLM palsu (tanpa API key, tanpa biaya).

Yang diuji bukan kualitas tulisan, melainkan hal-hal yang paling sering rusak
saat orang menyalin skrip AutoGen dari artikel:

* apakah tool benar-benar dieksekusi dan oleh bot yang tepat;
* apakah bot pemilik tool mendapat giliran membacakan hasil tool;
* apakah loop maker-checker (Damon menolak Clara) berjalan;
* apakah rapat berhenti tepat setelah PIPELINE_COMPLETE, bukan kehabisan round.
"""

from __future__ import annotations

import json

from mock_llm import MockLLM, extract_tool_calls

from studio.config import Settings
from studio.office import build_office

EXPECTED_TOOLS = {
    "save_content_brief",
    "save_production_script",
    "request_and_check_creator_files",
    "assemble_and_render_video",
    "inspect_rendered_output",
    "upload_to_youtube",
}


def _contents(messages) -> list[str]:
    return [str(m.get("content") or "") for m in messages]


def test_full_pipeline_end_to_end(settings: Settings, monkeypatch) -> None:
    mock = MockLLM()
    mock.install(monkeypatch)
    office = build_office(settings)

    summary = office.run("Otomasi produksi video mingguan")

    assert summary.completed is True, "rapat harus berakhir dengan PIPELINE_COMPLETE"
    assert summary.status == "PIPELINE_COMPLETE"
    assert summary.rounds < settings.max_rounds, "terminasi harus dari kata kunci, bukan max_round"

    # 1. setiap tool benar-benar dipanggil
    called = set(extract_tool_calls(summary.messages))
    assert called == EXPECTED_TOOLS

    # 2. tool dieksekusi oleh bot pemiliknya (bukan executor/bot lain)
    owner = {
        "save_content_brief": "Vito_TrendStrategist",
        "save_production_script": "Clara_Scriptwriter",
        "request_and_check_creator_files": "Bima_StudioPM",
        "assemble_and_render_video": "Raka_Producer",
        "inspect_rendered_output": "Damon_RealityChecker",
        "upload_to_youtube": "Sora_YouTubePublisher",
    }
    for message in summary.messages:
        for call in message.get("tool_calls") or []:
            assert message.get("name") == owner[call["function"]["name"]]

    # 3. bot pemilik tool mendapat giliran lanjutan setelah hasil tool
    speakers = [m.get("name") for m in summary.messages]
    for tool_owner in ("Vito_TrendStrategist", "Clara_Scriptwriter", "Bima_StudioPM",
                       "Raka_Producer", "Sora_YouTubePublisher"):
        assert speakers.count(tool_owner) >= 2, f"{tool_owner} tidak pernah membacakan hasil tool"

    # 4. loop maker-checker: Damon menolak, Clara merevisi, lalu lolos
    text = "\n".join(_contents(summary.messages))
    reject_at = next(i for i, c in enumerate(_contents(summary.messages)) if "SCRIPT REJECT" in c)
    pass_at = next(i for i, c in enumerate(_contents(summary.messages)) if "SCRIPT PASS" in c)
    assert reject_at < pass_at
    assert speakers.index("Damon_RealityChecker") < speakers.index("Bima_StudioPM")
    # Clara menyimpan naskah dua kali: versi awal lalu versi revisi
    assert sum(1 for c in _contents(summary.messages) if "script_production.md" in c) >= 2

    # 5. urutan gerbang: Damon mengaudit video sebelum Sora mengunggah
    assert text.index("AUDIT LULUS") < text.index("UPLOAD YOUTUBE")
    assert text.index("VIDEO PASS") < text.index("PIPELINE_COMPLETE")

    # 6. bukti di disk
    workspace = settings.paths.workspace
    output = settings.paths.output_ready
    brief = json.loads((workspace / "content_brief.json").read_text(encoding="utf-8"))
    assert brief["titles"]
    assert "NASKAH v2" in (workspace / "script_production.md").read_text(encoding="utf-8")
    assert (settings.paths.input_creator / "a_roll_raw.mp4").exists()
    assert (output / "final_render.mp4").exists()
    receipt = json.loads((output / "youtube_publish_receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "UPLOADED_SIMULATION"
    assert (workspace / "run_transcript.md").exists()

    # 7. tidak ada percakapan yang menunggu input manusia
    assert mock.agents_used  # LLM benar-benar dipanggil
