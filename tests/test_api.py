from __future__ import annotations

import os

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from studio.api import create_app  # noqa: E402
from studio.config import Settings  # noqa: E402
from studio.daemon import Daemon  # noqa: E402
from studio.memory import MemoryStore  # noqa: E402


@pytest.fixture
def client(settings: Settings, notifier):
    daemon = Daemon(
        settings,
        notifier=notifier,
        memory=MemoryStore(settings.memory_db_path),
        office_factory=lambda *args, **kwargs: None,
    )
    app = create_app(daemon, autostart=False)
    with TestClient(app) as test_client:
        yield test_client, daemon


def _video(settings: Settings) -> str:
    path = settings.paths.input_creator / "a_roll_raw.mp4"
    path.write_bytes(b"footage")
    old = 1_700_000_000
    os.utime(path, (old, old))
    return path.name


def test_health_reports_capabilities(client) -> None:
    test_client, _ = client
    response = test_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "ffmpeg" in body["capabilities"]


def test_ideation_trigger_queues_job(client) -> None:
    test_client, daemon = client
    response = test_client.post(
        "/kra/n100n-trigger",
        json={"phase": "IDEATION_SCRIPT", "topic_or_prompt": "Tren AI November"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued" and body["phase"] == "IDEATION_SCRIPT"
    job = daemon.jobs[body["job"]["id"]]
    assert job.phase == "ideation"
    assert job.topic == "Tren AI November"
    assert job.source == "n100n"


def test_production_trigger_requires_file(client, settings: Settings) -> None:
    test_client, daemon = client
    missing = test_client.post(
        "/kra/n100n-trigger",
        json={"phase": "PRODUCE_AND_UPLOAD", "raw_video_filename": "belum_ada.mp4"},
    )
    assert missing.status_code == 409
    assert daemon.queue.qsize() == 0

    name = _video(settings)
    response = test_client.post(
        "/kra/n100n-trigger",
        json={
            "phase": "PRODUCE_AND_UPLOAD",
            "raw_video_filename": name,
            "remotion_composition": "Intro",
        },
    )
    assert response.status_code == 202
    job = daemon.jobs[response.json()["job"]["id"]]
    assert job.phase == "production"
    assert job.raw_file.endswith(name)


def test_production_trigger_rejects_path_traversal(client, settings: Settings) -> None:
    test_client, _ = client
    response = test_client.post(
        "/kra/n100n-trigger",
        json={"phase": "PRODUCE_AND_UPLOAD", "raw_video_filename": "../../etc/passwd"},
    )
    assert response.status_code == 409  # disanitasi jadi nama file saja, tidak ada di input_creator


def test_unknown_phase_is_rejected(client) -> None:
    test_client, _ = client
    response = test_client.post("/kra/n100n-trigger", json={"phase": "RENDER_NOW"})
    assert response.status_code == 422
    assert "IDEATION_SCRIPT" in response.json()["detail"]


def test_job_detail_and_status(client) -> None:
    test_client, daemon = client
    job = daemon.submit_ideation("Topik status", source="test")
    assert test_client.get(f"/jobs/{job.id}").status_code == 200
    assert test_client.get("/jobs/tidak-ada").status_code == 404
    status = test_client.get("/status")
    assert status.status_code == 200
    assert status.json()["queue"] >= 1


# ------------------------------------------------- dual-loop n100n (1/2/3)
def test_start_script_marks_state_and_blocks_double_trigger(client) -> None:
    test_client, daemon = client
    body = {"topic_hint": "Tren AI untuk UMKM", "require_creator_aroll": True}
    first = test_client.post("/kra/schedule/1-start-script", json=body)
    assert first.status_code == 202
    payload = first.json()
    assert payload["status"] == "STARTED"
    assert payload["phase"] == "IDEATION_SCRIPT"
    assert payload["mode"] == "creator_aroll"
    assert daemon.state.status() == "SCRIPTING_IN_PROGRESS"

    second = test_client.post("/kra/schedule/1-start-script", json=body)
    assert second.status_code == 409
    assert "sibuk" in second.json()["detail"]


def test_faceless_cycle_needs_no_creator_file(client) -> None:
    test_client, daemon = client
    test_client.post(
        "/kra/schedule/1-start-script",
        json={"topic_hint": "Faceless", "require_creator_aroll": False},
    )
    assert daemon.state.read()["details"]["require_creator_aroll"] is False

    daemon.state.update("READY_FOR_RENDER", script_file="script_production.json")
    gate = test_client.get("/kra/schedule/2-check-gate").json()
    assert gate["ready_to_produce"] is True
    assert gate["creator_file_detected"] is False
    assert "faceless" in gate["reason"]

    started = test_client.post("/kra/schedule/3-produce-and-upload")
    assert started.status_code == 202
    payload = started.json()
    assert payload["status"] == "PRODUCTION_STARTED"
    assert payload["mode"] == "faceless_tts"
    assert daemon.state.status() == "RENDERING"

    again = test_client.post("/kra/schedule/3-produce-and-upload")
    assert again.status_code == 202
    assert again.json()["status"] == "ALREADY_RUNNING"


def test_gate_waits_until_creator_drops_file(client, settings: Settings) -> None:
    test_client, daemon = client
    daemon.state.update("WAITING_FOR_CREATOR", expected_file="a_roll_raw.mp4")

    waiting = test_client.get("/kra/schedule/2-check-gate?expected_file=a_roll_raw.mp4").json()
    assert waiting["ready_to_produce"] is False
    assert waiting["expected_file"] == "a_roll_raw.mp4"

    blocked = test_client.post("/kra/schedule/3-produce-and-upload?raw_video_filename=a_roll_raw.mp4")
    assert blocked.status_code == 409

    _video(settings)
    ready = test_client.get("/kra/schedule/2-check-gate").json()
    assert ready["ready_to_produce"] is True
    assert ready["creator_file_detected"] is True

    started = test_client.post("/kra/schedule/3-produce-and-upload")
    assert started.status_code == 202
    assert started.json()["mode"] == "creator_aroll"


def test_produce_rejected_while_rendering(client, settings: Settings) -> None:
    test_client, daemon = client
    _video(settings)
    daemon.state.update("RENDERING", job_id="job-lain")
    response = test_client.post("/kra/schedule/3-produce-and-upload")
    assert response.status_code == 409
    assert "sibuk" in response.json()["detail"]
