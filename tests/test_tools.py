from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio.config import Settings, StudioPaths
from studio.tools import RECEIPT_FILENAME, SIMULATION_MARKER, StudioTools


@pytest.fixture
def tools(settings: Settings) -> StudioTools:
    return StudioTools(settings)


def test_save_document_writes_file(tools: StudioTools, settings: Settings) -> None:
    result = tools.save_document("content_brief.json", '{"titles": ["A"]}')
    assert "TERSIMPAN" in result
    target = settings.paths.workspace / "content_brief.json"
    assert target.read_text(encoding="utf-8") == '{"titles": ["A"]}'
    assert "content_brief.json" in result


def test_save_document_rejects_path_traversal(tools: StudioTools, settings: Settings) -> None:
    result = tools.save_document("../../escape.md", "x")
    assert result.startswith("ERROR")
    assert not (settings.paths.root.parent / "escape.md").exists()


def test_save_document_rejects_empty_content(tools: StudioTools, settings: Settings) -> None:
    assert tools.save_document("x.md", "   ").startswith("ERROR")
    assert not (settings.paths.workspace / "x.md").exists()


def test_creator_gate_mock_creates_file_and_todo(tools: StudioTools, settings: Settings) -> None:
    result = tools.request_and_check_creator_files("Rekam A-Roll", "a_roll_raw.mp4")
    assert "STATUS UPLOADED" in result
    uploaded = settings.paths.input_creator / "a_roll_raw.mp4"
    assert uploaded.exists() and uploaded.stat().st_size > 0
    todo = (settings.paths.workspace / "creator_todo_list.md").read_text(encoding="utf-8")
    assert "a_roll_raw.mp4" in todo and "TUGAS CREATOR" in todo


def test_creator_gate_blocks_without_tty(tmp_path: Path, monkeypatch) -> None:
    paths = StudioPaths.from_root(tmp_path)
    local = Settings(paths=paths, api_key="sk-test", mock_creator=False)
    studio = StudioTools(local)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    result = studio.request_and_check_creator_files("Rekam A-Roll", "a_roll.mp4")
    assert result.startswith("STATUS BLOCKED")


def test_render_creates_output_and_sidecar(tools: StudioTools, settings: Settings) -> None:
    (settings.paths.input_creator / "a_roll_raw.mp4").write_bytes(SIMULATION_MARKER + b"|raw|")
    result = tools.assemble_and_render_video("a_roll_raw.mp4", "final_render.mp4")
    assert "RENDER SELESAI" in result
    output = settings.paths.output_ready / "final_render.mp4"
    assert output.exists() and output.stat().st_size > 0
    sidecar = settings.paths.output_ready / "final_render.render.json"
    report = json.loads(sidecar.read_text(encoding="utf-8"))
    assert report["engine"] == "simulation"
    assert report["target_spec"]["resolution"] == "1920x1080"


def test_render_missing_input_returns_error(tools: StudioTools) -> None:
    assert tools.assemble_and_render_video("nope.mp4", "final_render.mp4").startswith("ERROR")


def test_audit_fails_when_file_missing(tools: StudioTools) -> None:
    result = tools.inspect_rendered_output("final_render.mp4")
    assert result.startswith("AUDIT GAGAL")
    assert "belum benar-benar merender" in result


def test_audit_passes_simulated_output(tools: StudioTools, settings: Settings) -> None:
    (settings.paths.input_creator / "a_roll_raw.mp4").write_bytes(SIMULATION_MARKER + b"|raw|")
    tools.assemble_and_render_video("a_roll_raw.mp4", "final_render.mp4")
    result = tools.inspect_rendered_output("final_render.mp4")
    assert result.startswith("AUDIT LULUS")
    assert "SIMULASI" in result


def test_audit_fails_without_sidecar_report(tools: StudioTools, settings: Settings) -> None:
    (settings.paths.output_ready / "manual.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)
    result = tools.inspect_rendered_output("manual.mp4")
    assert result.startswith("AUDIT GAGAL")
    assert "laporan render" in result


def test_upload_simulation_writes_receipt(tools: StudioTools, settings: Settings) -> None:
    (settings.paths.input_creator / "a_roll_raw.mp4").write_bytes(SIMULATION_MARKER + b"|raw|")
    tools.assemble_and_render_video("a_roll_raw.mp4", "final_render.mp4")
    result = tools.upload_to_youtube(
        "final_render.mp4",
        "Judul Uji yang Pendek",
        "00:00 Intro\n00:30 Bagian 2\n01:10 Bagian 3",
        ["ai", "agent", "otomasi"],
        "unlisted",
    )
    assert "UPLOAD YOUTUBE (SIMULASI)" in result
    receipt = json.loads((settings.paths.output_ready / RECEIPT_FILENAME).read_text(encoding="utf-8"))
    assert receipt["status"] == "UPLOADED_SIMULATION"
    assert receipt["privacy"] == "unlisted"


def test_upload_rejects_overlong_title(tools: StudioTools, settings: Settings) -> None:
    (settings.paths.output_ready / "final_render.mp4").write_bytes(b"x" * 10)
    result = tools.upload_to_youtube("final_render.mp4", "T" * 120, "00:00", ["a"])
    assert result.startswith("ERROR UPLOAD")


def test_upload_requires_existing_video(tools: StudioTools) -> None:
    result = tools.upload_to_youtube("missing.mp4", "Judul", "00:00", ["a"])
    assert result.startswith("ERROR UPLOAD")
