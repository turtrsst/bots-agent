from __future__ import annotations

import json

from studio.config import Settings
from studio.rehearsal import audit_script, build_brief, build_script, run_rehearsal


def test_brief_titles_fit_youtube_limit() -> None:
    brief = build_brief("Cara membangun tim AI Agent untuk mempercepat kerja Content Creator")
    assert len(brief["titles"]) == 3
    assert len(brief["thumbnails"]) == 3
    for item in brief["titles"]:
        assert len(item["title"]) <= 55
        assert item["chars"] == len(item["title"])


def test_script_passes_damon_checklist() -> None:
    script = build_script("Otomasi produksi video")
    assert audit_script(script) == []
    assert "[CREATOR A-ROLL]" in script and "[AI B-ROLL]" in script


def test_audit_flags_banned_smalltalk() -> None:
    problems = audit_script("Halo guys, di video kali ini kita bahas sesuatu.")
    assert any("halo guys" in problem for problem in problems)


def test_rehearsal_runs_full_pipeline(settings: Settings) -> None:
    summary = run_rehearsal(settings, "Otomasi produksi video mingguan")

    assert summary.completed is True
    assert summary.status == "PIPELINE_COMPLETE"
    assert "SIMULASI" in summary.youtube_url

    workspace = settings.paths.workspace
    output = settings.paths.output_ready
    brief = json.loads((workspace / "content_brief.json").read_text(encoding="utf-8"))
    assert brief["titles"]
    assert (workspace / "script_production.md").exists()
    assert (workspace / "creator_todo_list.md").exists()
    assert (workspace / "run_transcript.md").exists()
    assert (output / "final_render.mp4").exists()
    assert (output / "youtube_publish_receipt.json").exists()
    assert any(item.endswith("final_render.mp4") for item in summary.files)
    assert any("PIPELINE_COMPLETE" in (message.get("content") or "") for message in summary.messages)
