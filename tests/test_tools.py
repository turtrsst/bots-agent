from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio.config import Settings, StudioPaths
from studio.tools import RECEIPT_FILENAME, SIMULATION_MARKER, TODO_FILENAME, StudioTools


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


# ---------------------------------------------------------------- fase 2: async
def test_async_gate_returns_awaiting_instead_of_blocking(settings: Settings) -> None:
    gated = settings.with_changes(async_gate=True)
    tools = StudioTools(gated)

    result = tools.request_and_check_creator_files("Rekam A-Roll", "a_roll_raw.mp4")

    assert "STATUS AWAITING" in result
    assert "AWAITING_CREATOR_UPLOAD" in result
    assert (gated.paths.workspace / TODO_FILENAME).exists()
    assert not (gated.paths.input_creator / "a_roll_raw.mp4").exists()


def test_async_gate_accepts_file_already_present(settings: Settings) -> None:
    gated = settings.with_changes(async_gate=True)
    gated.paths.ensure()
    uploaded = gated.paths.input_creator / "a_roll_raw.mp4"
    uploaded.write_bytes(b"footage")
    tools = StudioTools(gated)

    assert "STATUS UPLOADED" in tools.request_and_check_creator_files("Rekam", "a_roll_raw.mp4")


# ------------------------------------------------------------- fase 2: render
def test_real_render_runs_ffmpeg_pipeline(
    settings: Settings, fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = settings.with_changes(real_render=True)
    tools = StudioTools(real)
    from studio.media import Capabilities

    monkeypatch.setattr(
        "studio.tools.detect_capabilities", lambda _settings: Capabilities(ffmpeg=str(fake_ffmpeg))
    )
    (real.paths.input_creator / "a_roll_raw.mp4").write_bytes(b"real footage")

    result = tools.assemble_and_render_video("a_roll_raw.mp4", "final_render.mp4", hook_title="Hook")

    assert result.startswith("RENDER FISIK SELESAI")
    report = json.loads((real.paths.output_ready / "final_render.render.json").read_text(encoding="utf-8"))
    assert report["engine"] == "ffmpeg"
    assert report["target_spec"]["resolution"] == "1920x1080"
    assert {step["name"] for step in report["steps"]} >= {"normalize_audio", "conform", "finalize"}
    assert tools.inspect_rendered_output("final_render.mp4").startswith("AUDIT LULUS")


def test_real_render_without_ffmpeg_is_an_honest_error(settings: Settings) -> None:
    real = settings.with_changes(real_render=True)
    tools = StudioTools(real)
    (real.paths.input_creator / "a_roll_raw.mp4").write_bytes(b"real footage")

    result = tools.assemble_and_render_video("a_roll_raw.mp4", "final_render.mp4")

    assert result.startswith("ERROR RENDER")
    assert not (real.paths.output_ready / "final_render.mp4").exists()


def test_audit_rejects_simulated_artifact_in_production_mode(settings: Settings) -> None:
    real = settings.with_changes(real_render=True)
    tools = StudioTools(real)
    (real.paths.input_creator / "a_roll_raw.mp4").write_bytes(SIMULATION_MARKER + b"dummy")

    tools.assemble_and_render_video("a_roll_raw.mp4", "final_render.mp4")

    assert tools.inspect_rendered_output("final_render.mp4").startswith("AUDIT GAGAL")


def test_render_tool_exposes_remotion_hook(settings: Settings) -> None:
    import inspect

    signature = inspect.signature(StudioTools(settings).build_functions()["assemble_and_render_video"])
    assert "hook_title" in signature.parameters
    assert "remotion_composition" in signature.parameters
    assert signature.parameters["hook_title"].default == ""
