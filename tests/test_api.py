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
