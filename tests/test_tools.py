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


# ------------------------------------------------- notifikasi Telegram (tool)
class FakeNotifier:
    supports_inbound = False

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.html: list[str] = []
        self.documents: list[str] = []

    def send(self, text: str, *, silent: bool = False) -> bool:
        return self.ok

    def send_html(self, text: str, *, silent: bool = False) -> bool:
        self.html.append(text)
        return self.ok

    def send_document(self, path, caption: str = "") -> bool:
        self.documents.append(Path(path).name)
        return self.ok


def test_send_direct_alert_to_creator(settings: Settings) -> None:
    notifier = FakeNotifier()
    studio = StudioTools(settings, notifier=notifier)

    result = studio.send_direct_alert_to_creator("Sora_YouTubePublisher", "Video live: https://youtu.be/x")

    assert result == "BERHASIL DIKIRIM KE TELEGRAM CREATOR"
    assert notifier.html and "Sora_YouTubePublisher" in notifier.html[0]
    assert "https://youtu.be/x" in notifier.html[0]


def test_send_direct_alert_escapes_and_rejects_empty(settings: Settings) -> None:
    notifier = FakeNotifier()
    studio = StudioTools(settings, notifier=notifier)

    assert studio.send_direct_alert_to_creator("Bima", "   ").startswith("ERROR ALERT")
    assert not notifier.html

    studio.send_direct_alert_to_creator("Bima_StudioPM", "<script>alert(1)</script>")
    assert "&lt;script&gt;" in notifier.html[-1]


def test_send_direct_alert_reports_failure(settings: Settings) -> None:
    studio = StudioTools(settings, notifier=FakeNotifier(ok=False))
    result = studio.send_direct_alert_to_creator("Bima", "Halo")
    assert result.startswith("GAGAL MENGIRIM KE TELEGRAM")


def test_alert_tool_exposed_to_bots(settings: Settings) -> None:
    from studio.office import AGENT_SPECS

    owners = {
        spec.key for spec in AGENT_SPECS if "send_direct_alert_to_creator" in spec.tools
    }
    assert owners == {"bima", "sora"}


# ------------------------------------------------------------ thumbnail
def test_simulation_receipt_reports_thumbnail(settings: Settings) -> None:
    studio = StudioTools(settings)
    video = settings.paths.output_ready / "final_render.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 2048)

    result = studio.upload_to_youtube("final_render.mp4", "Judul uji", "00:00 intro", ["a", "b", "c"])

    assert "SIMULASI" in result
    receipt = json.loads((settings.paths.output_ready / RECEIPT_FILENAME).read_text(encoding="utf-8"))
    assert receipt["thumbnail"]["status"] == "not_sent"


def test_apply_thumbnail_sets_and_reports(settings: Settings) -> None:
    studio = StudioTools(settings)
    thumb = settings.paths.output_ready / "thumbnails" / "thumb_1.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"\xff\xd8\xff" + b"x" * 100)

    calls: list[tuple[str, str]] = []

    class Thumbnails:
        def set(self, *, videoId: str, media_body):
            calls.append(("set", videoId))

            class Exec:
                def execute(self_inner):
                    calls.append(("execute", videoId))
                    return {}

            return Exec()

    class YouTube:
        def thumbnails(self):
            return Thumbnails()

    status = studio._apply_thumbnail(YouTube(), "vid123", media_factory=lambda path: path)

    assert status == "set"
    assert calls == [("set", "vid123"), ("execute", "vid123")]


def test_apply_thumbnail_failure_does_not_raise(settings: Settings) -> None:
    studio = StudioTools(settings)
    thumb = settings.paths.output_ready / "thumbnails" / "thumb_1.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"\xff\xd8\xff")

    class YouTube:
        def thumbnails(self):
            raise RuntimeError("scope kurang")

    status = studio._apply_thumbnail(YouTube(), "vid", media_factory=lambda path: path)
    assert status.startswith("failed:")


def test_apply_thumbnail_without_file(settings: Settings) -> None:
    studio = StudioTools(settings)
    assert studio._apply_thumbnail(object(), "vid") == "no_thumbnail"


# ---------------------------------------------------- memori episodik (tool)
def _episode_tools(settings: Settings) -> StudioTools:
    from studio.memory import MemoryStore

    return StudioTools(settings, memory=MemoryStore(settings.memory_db_path))


def test_memory_tools_flow(settings: Settings) -> None:
    studio = _episode_tools(settings)
    assert "MEMORI KRA KOSONG" in studio.get_recent_content_memory()

    saved = studio.register_new_episode_to_memory(
        "story_kra_001",
        "kantor AI berjalan tanpa manusia",
        "Kantor AI Tanpa Manusia",
        "ai agent, otomasi",
    )
    assert saved.startswith("MEMORI TERSIMPAN")
    assert "story_kra_001" in studio.get_recent_content_memory()

    check = studio.check_topic_similarity_in_memory("Kantor AI tanpa manusia malam hari")
    assert check.startswith("DUPLICATE_WARNING")
    assert "story_kra_001" in check
    assert "ganti sudut pandang" in check

    fresh = studio.check_topic_similarity_in_memory("Cara merawat tanaman hias")
    assert fresh.startswith("TOPIC_UNIQUE_PASS")


def test_register_rejects_empty_title_and_flags_similar(settings: Settings) -> None:
    studio = _episode_tools(settings)
    assert studio.register_new_episode_to_memory("p0", "topik", "  ").startswith("ERROR MEMORI")

    studio.register_new_episode_to_memory("p1", "kantor AI tanpa manusia", "Kantor AI Tanpa Manusia")
    result = studio.register_new_episode_to_memory(
        "p2", "kantor AI berjalan tanpa manusia", "Kantor AI Tanpa Operator"
    )
    assert "MEMORI TERSIMPAN" in result
    assert "masih ada" in result  # kemiripan dilaporkan, bukan disembunyikan


def test_similarity_tool_rejects_empty(settings: Settings) -> None:
    studio = _episode_tools(settings)
    assert studio.check_topic_similarity_in_memory("   ").startswith("ERROR MEMORI")


def test_upload_records_episode_in_memory(settings: Settings) -> None:
    from studio.memory import MemoryStore

    memory = MemoryStore(settings.memory_db_path)
    studio = StudioTools(settings, memory=memory)
    (settings.paths.workspace / "script_production.json").write_text(
        json.dumps({"project_id": "story_kra_777", "title": "Kantor AI Tanpa Manusia"}),
        encoding="utf-8",
    )
    video = settings.paths.output_ready / "final_render.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 2048)

    studio.upload_to_youtube("final_render.mp4", "Judul", "00:00 intro", ["a", "b", "c"])

    episode = memory.get_episode("story_kra_777")
    assert episode is not None
    assert episode["status"] == "UPLOADED_SIMULATION"  # jujur: bukan PUBLISHED
    receipt = json.loads((settings.paths.output_ready / RECEIPT_FILENAME).read_text(encoding="utf-8"))
    assert receipt["project_id"] == "story_kra_777"


def test_memory_tools_exposed_to_bots() -> None:
    from studio.office import AGENT_SPECS

    tools = {spec.key: set(spec.tools) for spec in AGENT_SPECS}
    assert {"get_recent_content_memory", "check_topic_similarity_in_memory"} <= tools["vito"]
    assert {"check_topic_similarity_in_memory", "register_new_episode_to_memory"} <= tools["damon"]
    assert "get_recent_content_memory" not in tools["sora"]
