from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from studio.api import create_app  # noqa: E402
from studio.config import Settings  # noqa: E402
from studio.daemon import Daemon  # noqa: E402
from studio.memory import MemoryStore  # noqa: E402
from studio.state import READY_RENDER, SCRIPTING  # noqa: E402
from studio.thumbnail import primary_thumbnail, selected_thumbnail  # noqa: E402


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


def _thumb(settings: Settings, index: int, payload: bytes = b"\xff\xd8\xff\xe0jpeg") -> None:
    folder = settings.paths.output_ready / "thumbnails"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"thumb_{index}.jpg").write_bytes(payload)


# ------------------------------------------------------------------ halaman
def test_dashboard_page_is_self_contained(client) -> None:
    test_client, _ = client
    page = test_client.get("/")
    assert page.status_code == 200
    html = page.text
    assert "KRA CONTROL CENTER" in html
    assert "Live Agent Group Chat" in html
    assert "SQLite Content Memory" in html
    assert "Remotion Queue" in html
    # tanpa CDN eksternal: dashboard tetap tampil saat mesin n100n offline
    assert "cdn.jsdelivr.net" not in html
    assert "/api/state" in html and "/api/logs" in html
    assert test_client.get("/dashboard").status_code == 200


# -------------------------------------------------------------------- state
def test_state_endpoint_reports_pipeline_and_runtime(client) -> None:
    test_client, daemon = client
    daemon.state.update(SCRIPTING, topic="Tren AI", job_id="job-1")
    body = test_client.get("/api/state").json()
    assert body["status"] == SCRIPTING
    assert body["details"]["topic"] == "Tren AI"
    assert body["runtime"]["queue"] == 0
    assert "capabilities" in body["runtime"]
    assert body["memory"]["episodes"] == 0


def test_logs_endpoint_serves_chat_log(client) -> None:
    test_client, daemon = client
    daemon.chat_log.append("Vito_TrendStrategist", "GROUPCHAT", "Riset tren selesai.")
    daemon.chat_log.append("Clara_Scriptwriter", "GROUPCHAT", "Naskah babak 1 siap.")
    logs = test_client.get("/api/logs?limit=1").json()
    assert len(logs) == 1
    assert logs[0]["sender"] == "Clara_Scriptwriter"
    assert (daemon.settings.paths.workspace / "chat_logs.json").is_file()


def test_history_endpoint_lists_episodes(client) -> None:
    test_client, daemon = client
    daemon.memory.register_episode(
        "story_kra_101",
        topic_summary="kantor AI tanpa manusia",
        primary_title="Kantor AI Tanpa Manusia",
    )
    daemon.memory.mark_episode_published(
        "story_kra_101", video_id="abc123", url="https://youtu.be/abc123"
    )
    body = test_client.get("/api/history").json()
    assert len(body) == 1
    episode = body[0]
    assert episode["project_id"] == "story_kra_101"
    assert episode["status"] == "PUBLISHED"
    assert episode["youtube_url"].endswith("abc123")


# --------------------------------------------------------------- thumbnail
def test_thumbnails_endpoint_and_selection(client, settings: Settings) -> None:
    test_client, daemon = client
    _thumb(settings, 1)
    _thumb(settings, 2)
    daemon.state.update(READY_RENDER)

    listing = test_client.get("/api/thumbnails").json()
    assert [item["file"] for item in listing["items"]] == ["thumb_1.jpg", "thumb_2.jpg"]
    assert listing["selected"] == "thumb_1.jpg"
    assert listing["items"][0]["selected"] is True

    image = test_client.get("/api/thumbnail/2")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"

    chosen = test_client.post("/api/thumbnail/select?index=2").json()
    assert chosen["status"] == "THUMBNAIL_DIPILIH"
    assert chosen["file"] == "thumb_2.jpg"
    assert selected_thumbnail(settings).name == "thumb_2.jpg"
    assert primary_thumbnail(settings).name == "thumb_2.jpg"
    assert daemon.state.read()["details"]["selected_thumbnail"] == "thumb_2.jpg"
    assert json.loads((settings.paths.workspace / "thumbnail_choice.json").read_text())["via"] == (
        "dashboard"
    )


def test_selecting_missing_thumbnail_is_404(client) -> None:
    test_client, _ = client
    response = test_client.post("/api/thumbnail/select?index=3")
    assert response.status_code == 404
    assert response.json()["status"] == "TIDAK_ADA"


# ------------------------------------------------------------ cron mingguan
def test_weekly_sync_endpoint_is_honest_without_key(client) -> None:
    test_client, daemon = client
    body = test_client.post("/kra/schedule/sync-views").json()
    assert body["status"] in {"EMPTY", "NO_KEY"}
    assert "message" in body
    assert daemon.state.read()["details"]["views_sync"]["status"] == body["status"]
