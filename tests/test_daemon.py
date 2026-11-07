from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from studio.config import Settings
from studio.daemon import Daemon, Job, next_run_at, parse_schedule
from studio.memory import MemoryStore
from studio.office import RunSummary


class FakeOffice:
    def __init__(self, summary: RunSummary, calls: list[tuple[str, str]], on_run=None) -> None:
        self.summary = summary
        self.calls = calls
        self.on_run = on_run

    def run(self, topic: str, *, raw_file: str = "") -> RunSummary:
        self.calls.append((topic, raw_file))
        if self.on_run:
            self.on_run(topic, raw_file)
        return self.summary


def make_daemon(settings: Settings, notifier, factory, memory=None, clock=None) -> Daemon:
    return Daemon(
        settings,
        notifier=notifier,
        memory=memory or MemoryStore(settings.memory_db_path),
        office_factory=factory,
        clock=clock,
    )


def summary(**overrides) -> RunSummary:
    data = {
        "topic": "Topik uji",
        "completed": False,
        "rounds": 6,
        "elapsed_seconds": 1.2,
        "awaiting_creator": True,
        "files": ["workspace/script_production.md"],
    }
    data.update(overrides)
    return RunSummary(**data)


# ------------------------------------------------------------------ jadwal
def test_parse_schedule_ignores_garbage() -> None:
    assert parse_schedule("07:00, 16:30") == [dtime(7, 0), dtime(16, 30)]
    assert parse_schedule("7:5, 25:00, abc, 12:00;12:00") == [dtime(7, 5), dtime(12, 0)]
    assert parse_schedule("") == []


def test_next_run_at_rolls_to_tomorrow() -> None:
    now = datetime(2026, 11, 7, 9, 0, tzinfo=datetime.now().astimezone().tzinfo)
    upcoming = next_run_at([dtime(7, 0), dtime(16, 30)], now)
    assert upcoming is not None
    assert upcoming.date() == now.date() and upcoming.hour == 16
    later = next_run_at([dtime(7, 0)], now)
    assert later.date() == (now + timedelta(days=1)).date()
    assert next_run_at([], now) is None


# ----------------------------------------------------------------- watcher
def _video(path: Path, size: int = 4096, age: float = 60.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    old = 1_700_000_000
    os.utime(path, (old, old))
    return path


def test_check_uploads_detects_once(settings: Settings, notifier) -> None:
    daemon = make_daemon(settings, notifier, lambda *a, **k: None)
    _video(settings.paths.input_creator / "a_roll_raw.mp4")

    first = daemon.check_uploads()
    assert [path.name for path in first] == ["a_roll_raw.mp4"]
    assert daemon.check_uploads() == []  # tidak diproses dua kali


def test_check_uploads_ignores_empty_and_wrong_extension(settings: Settings, notifier) -> None:
    daemon = make_daemon(settings, notifier, lambda *a, **k: None)
    (settings.paths.input_creator / "kosong.mp4").write_bytes(b"")
    _video(settings.paths.input_creator / "catatan.txt")
    assert daemon.check_uploads() == []


def test_settle_time_holds_files_still_being_written(settings: Settings, notifier) -> None:
    settings = settings.with_changes(upload_settle=3600)
    daemon = make_daemon(settings, notifier, lambda *a, **k: None)
    path = settings.paths.input_creator / "baru.mp4"
    path.write_bytes(b"partial")
    assert daemon.check_uploads() == []
    old = 1_700_000_000
    os.utime(path, (old, old))
    assert [item.name for item in daemon.check_uploads()] == ["baru.mp4"]


# -------------------------------------------------------------------- job
def test_ideation_job_records_memory_and_notifies(settings: Settings, notifier) -> None:
    calls: list[tuple[str, str]] = []

    def on_run(topic: str, raw_file: str) -> None:
        (settings.paths.workspace / "content_brief.json").write_text(
            json.dumps({"titles": [{"title": "Judul Terbaik"}]}), encoding="utf-8"
        )

    factory = lambda *a, **k: FakeOffice(summary(), calls, on_run)  # noqa: E731
    daemon = make_daemon(settings, notifier, factory)

    job = daemon.submit_ideation("Ide pagi")
    asyncio.run(daemon.drain())

    assert job.status == "awaiting_creator"
    assert job.status_detail == "AWAITING_CREATOR_UPLOAD"
    assert calls and calls[0][0] == "Ide pagi"
    run = daemon.memory.get_run(job.run_id)
    assert run["status"] == "AWAITING_CREATOR_UPLOAD"
    assert "input_creator" in notifier.messages[-1]
    assert "Naskah siap" in notifier.messages[-1]


def test_production_job_reuses_pending_run_and_records_video(settings: Settings, notifier) -> None:
    memory = MemoryStore(settings.memory_db_path)
    memory.record_run("run-pending", "Topik lama", status="AWAITING_CREATOR_UPLOAD")

    completed = summary(
        completed=True, awaiting_creator=False, youtube_url="https://youtu.be/final", topic="Topik lama"
    )
    calls: list[tuple[str, str]] = []

    def on_run(topic: str, raw_file: str) -> None:
        (settings.paths.workspace / "content_brief.json").write_text(
            json.dumps({"titles": [{"title": "Judul Video"}]}), encoding="utf-8"
        )

    daemon = make_daemon(settings, notifier, lambda *a, **k: FakeOffice(completed, calls, on_run), memory)
    raw = _video(settings.paths.input_creator / "a_roll_raw.mp4")

    job = daemon.submit_production(str(raw))
    asyncio.run(daemon.drain())

    assert job.status == "done"
    assert calls == [("Topik lama", "a_roll_raw.mp4")]
    assert job.run_id == "run-pending"
    assert daemon.memory.get_run("run-pending")["status"] == "PIPELINE_COMPLETE"
    videos = daemon.memory.recent_videos()
    assert videos and videos[0]["url"] == "https://youtu.be/final"
    assert "Video selesai" in notifier.messages[-1]


def test_failing_job_is_marked_and_reported(settings: Settings, notifier) -> None:
    class Boom:
        def run(self, topic: str, *, raw_file: str = "") -> RunSummary:
            raise RuntimeError("API key tidak valid")

    daemon = make_daemon(settings, notifier, lambda *a, **k: Boom())
    job = daemon.submit_ideation("Ide")
    asyncio.run(daemon.drain())

    assert job.status == "failed"
    assert "API key" in job.error
    assert "gagal" in notifier.messages[-1].lower()


def test_status_text_reports_queue_and_capabilities(settings: Settings, notifier) -> None:
    daemon = make_daemon(settings, notifier, lambda *a, **k: None)
    daemon.submit_ideation("Ide tertunda")
    text = daemon.status_text()
    assert "STATUS STUDIO" in text
    assert "antrean: 1" in text
    assert daemon.status()["queue"] == 1


def test_resume_pending_requeues_production(settings: Settings, notifier) -> None:
    memory = MemoryStore(settings.memory_db_path)
    memory.record_run("run-wait", "Topik tertunda", status="AWAITING_CREATOR_UPLOAD")
    daemon = make_daemon(settings, notifier, lambda *a, **k: None, memory)
    _video(settings.paths.input_creator / "a_roll_raw.mp4")

    daemon._resume_pending()

    assert daemon.queue.qsize() == 1
    job = daemon.queue.get_nowait()
    assert job.phase == "production"
    assert job.source == "resume"


# --------------------------------------------------------------- telegram
class InboundNotifier:
    supports_inbound = True

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str, *, silent: bool = False) -> bool:
        self.messages.append(text)
        return True

    def poll(self, offset: int = 0):  # pragma: no cover - tidak dipakai di test ini
        return [], offset


def test_telegram_commands(settings: Settings) -> None:
    inbound = InboundNotifier()
    daemon = make_daemon(settings, inbound, lambda *a, **k: None)

    asyncio.run(daemon._handle_command({"message": {"text": "/status"}}))
    assert "STATUS STUDIO" in inbound.messages[-1]

    asyncio.run(daemon._handle_command({"message": {"text": "/topic Video soal AI agents"}}))
    assert daemon.queue.qsize() == 1
    assert daemon.queue.get_nowait().source == "telegram"

    _video(settings.paths.input_creator / "a_roll_raw.mp4")
    asyncio.run(daemon._handle_command({"message": {"text": "/produce@studio_bot"}}))
    assert daemon.queue.get_nowait().phase == "production"

    asyncio.run(daemon._handle_command({"message": {"text": "halo biasa"}}))
    assert daemon.queue.qsize() == 0


# ---------------------------------------------------------------- lifecycle
def test_start_stop_launches_background_tasks(settings: Settings, notifier) -> None:
    calls: list[tuple[str, str]] = []
    settings = settings.with_changes(schedule="", watch_interval=0.05)
    daemon = make_daemon(settings, notifier, lambda *a, **k: FakeOffice(summary(), calls))

    async def scenario() -> None:
        await daemon.start()
        assert daemon._running
        assert len(daemon._tasks) >= 2
        await daemon.stop()
        assert not daemon._running

    asyncio.run(scenario())


def test_watcher_queues_production_when_file_lands(settings: Settings, notifier) -> None:
    calls: list[tuple[str, str]] = []
    settings = settings.with_changes(schedule="", watch_interval=0.05)
    daemon = make_daemon(settings, notifier, lambda *a, **k: FakeOffice(summary(), calls))

    async def scenario() -> None:
        await daemon.start()
        _video(settings.paths.input_creator / "a_roll_raw.mp4")
        for _ in range(60):
            await asyncio.sleep(0.05)
            if daemon.queue.qsize():
                break
        await daemon.stop()

    asyncio.run(scenario())
    jobs = list(daemon.jobs.values())
    assert [job.phase for job in jobs] == ["production"]
    assert jobs[0].raw_file.endswith("a_roll_raw.mp4")
    assert any("terdeteksi" in message for message in notifier.messages)


def test_job_as_dict_is_json_serialisable() -> None:
    job = Job(id="job-1", phase="ideation", topic="x")
    assert json.loads(json.dumps(job.as_dict()))["id"] == "job-1"


# --------------------------------------------------- dual-loop pipeline state
def test_ideation_sets_waiting_for_creator(settings: Settings, notifier) -> None:
    daemon = make_daemon(settings, notifier, lambda *a, **k: FakeOffice(summary(), []))
    daemon.submit_ideation("Ide pagi")
    asyncio.run(daemon.drain())

    state = daemon.state.read()
    assert state["status"] == "WAITING_FOR_CREATOR"
    assert state["details"]["expected_file"] == "a_roll_raw.mp4"
    assert state["details"]["script_file"] == "script_production.json"


def test_faceless_ideation_never_waits_for_creator(settings: Settings, notifier) -> None:
    seen: dict = {}

    def factory(*_args, **kwargs):
        seen.update(kwargs)
        return FakeOffice(summary(), [])

    daemon = make_daemon(settings, notifier, factory)
    daemon.submit_ideation("Ide faceless", require_aroll=False)
    asyncio.run(daemon.drain())

    assert seen["require_creator_aroll"] is False
    state = daemon.state.read()
    assert state["status"] == "READY_FOR_RENDER"
    assert state["details"]["faceless"] is True


def test_production_publishes_then_archives_raw(settings: Settings, notifier) -> None:
    completed = summary(completed=True, awaiting_creator=False, youtube_url="https://youtu.be/abc")

    def on_run(topic: str, raw_file: str) -> None:
        (settings.paths.workspace / "content_brief.json").write_text(
            json.dumps({"titles": [{"title": "Judul"}]}), encoding="utf-8"
        )

    daemon = make_daemon(settings, notifier, lambda *a, **k: FakeOffice(completed, [], on_run))
    raw = _video(settings.paths.input_creator / "a_roll_raw.mp4")

    job = daemon.submit_production(str(raw))
    assert daemon.state.status() == "RENDERING"
    asyncio.run(daemon.drain())

    state = daemon.state.read()
    assert job.status == "done"
    assert state["status"] == "PUBLISHED_COMPLETE"
    assert state["details"]["youtube_url"] == "https://youtu.be/abc"
    assert not raw.exists()
    assert (settings.paths.processed / "a_roll_raw.mp4").exists()


def test_failed_job_marks_state_error(settings: Settings, notifier) -> None:
    class Boom:
        def run(self, topic: str, *, raw_file: str = "") -> RunSummary:
            raise RuntimeError("kuota habis")

    daemon = make_daemon(settings, notifier, lambda *a, **k: Boom())
    daemon.submit_ideation("Ide")
    asyncio.run(daemon.drain())

    state = daemon.state.read()
    assert state["status"] == "ERROR"
    assert "kuota habis" in state["details"]["error"]


def test_submit_production_is_idempotent(settings: Settings, notifier) -> None:
    daemon = make_daemon(settings, notifier, lambda *a, **k: None)
    raw = _video(settings.paths.input_creator / "a_roll_raw.mp4")

    first = daemon.submit_production(str(raw))
    second = daemon.submit_production(str(raw))

    assert first.id == second.id
    assert daemon.queue.qsize() == 1


def test_status_includes_pipeline_state(settings: Settings, notifier) -> None:
    daemon = make_daemon(settings, notifier, lambda *a, **k: None)
    daemon.state.update("READY_FOR_RENDER", script_file="script_production.json")

    assert daemon.status()["pipeline_state"]["status"] == "READY_FOR_RENDER"
    assert "Pipeline: READY_FOR_RENDER" in daemon.status_text()


# ------------------------------------------------------------------- shorts
def test_production_renders_short_after_upload(
    settings: Settings, notifier, fake_ffmpeg, monkeypatch
) -> None:
    """Upload selesai → daemon langsung memotong Shorts 9:16 dari final_render.mp4."""
    completed = summary(completed=True, awaiting_creator=False, youtube_url="https://youtu.be/abc")
    settings = settings.with_changes(shorts_duration=40)
    monkeypatch.setenv("PATH", f"{fake_ffmpeg.parent}:{os.environ.get('PATH', '')}")

    def on_run(topic: str, raw_file: str) -> None:
        (settings.paths.output_ready / "final_render.mp4").write_bytes(b"video" * 512)

    daemon = make_daemon(settings, notifier, lambda *a, **k: FakeOffice(completed, [], on_run))
    daemon.submit_production(str(_video(settings.paths.input_creator / "a_roll_raw.mp4")))
    asyncio.run(daemon.drain())

    shorts = (daemon.state.read()["details"] or {}).get("shorts") or {}
    assert shorts["status"] == "SHORTS_READY"
    assert shorts["duration_seconds"] == 40
    assert shorts["resolution"] == "1080x1920"
    assert (settings.paths.output_ready / "shorts").is_dir()
    assert any("KRA SHORTS" in message for message in notifier.messages)
    assert any("[SHORTS]" in entry["message"] for entry in daemon.chat_log.all())


def test_shorts_failure_does_not_fail_pipeline(settings: Settings, notifier, monkeypatch) -> None:
    completed = summary(completed=True, awaiting_creator=False, youtube_url="https://youtu.be/abc")

    def on_run(topic: str, raw_file: str) -> None:
        (settings.paths.output_ready / "final_render.mp4").write_bytes(b"video")

    def boom(*_args, **_kwargs):
        raise RuntimeError("filter ffmpeg rusak")

    monkeypatch.setattr("studio.shorts.generate_youtube_short", boom)
    daemon = make_daemon(settings, notifier, lambda *a, **k: FakeOffice(completed, [], on_run))
    daemon.submit_production(str(_video(settings.paths.input_creator / "a_roll_raw.mp4")))
    asyncio.run(daemon.drain())

    assert daemon.state.status() == "PUBLISHED_COMPLETE"
    assert "shorts" not in (daemon.state.read()["details"] or {})


def test_chat_log_file_is_shared_with_dashboard(settings: Settings, notifier) -> None:
    daemon = make_daemon(settings, notifier, lambda *a, **k: None)
    daemon.chat_log.append("Vito_TrendStrategist", "GROUPCHAT", "Halo dari rapat")
    path = settings.paths.workspace / "chat_logs.json"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))[-1]["sender"] == "Vito_TrendStrategist"
