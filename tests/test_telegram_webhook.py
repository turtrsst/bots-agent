from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from studio.api import create_app  # noqa: E402
from studio.config import Settings  # noqa: E402
from studio.daemon import Daemon  # noqa: E402
from studio.memory import MemoryStore  # noqa: E402
from studio.state import READY_RENDER, RENDERING, WAITING_CREATOR  # noqa: E402
from studio.telegram_webhook import (  # noqa: E402
    delete_webhook,
    handle_command,
    parse_command,
    set_webhook,
)


def _daemon(settings: Settings, notifier) -> Daemon:
    return Daemon(
        settings,
        notifier=notifier,
        memory=MemoryStore(settings.memory_db_path),
        office_factory=lambda *args, **kwargs: None,
    )


@pytest.fixture
def client(settings: Settings, notifier):
    daemon = _daemon(settings, notifier)
    app = create_app(daemon, autostart=False)
    with TestClient(app) as test_client:
        yield test_client, daemon


# --------------------------------------------------------------- parse
def test_parse_command_variants() -> None:
    assert parse_command("/thumb 2") == ("thumb", "2")
    assert parse_command("/status@kra_bot") == ("status", "")
    assert parse_command("  /approve ") == ("approve", "")
    assert parse_command("halo") == ("", "halo")
    assert parse_command("") == ("", "")


# --------------------------------------------------------------- perintah
def test_help_lists_commands(settings: Settings, notifier) -> None:
    daemon = _daemon(settings, notifier)
    result = handle_command(daemon, "/help")
    assert result["command"] == "help"
    for command in ("/status", "/riwayat", "/thumb", "/approve"):
        assert command in result["reply"]


def test_status_reports_pipeline(settings: Settings, notifier) -> None:
    daemon = _daemon(settings, notifier)
    daemon.state.update(READY_RENDER, topic="Tren AI")
    reply = handle_command(daemon, "/status")["reply"]
    assert READY_RENDER in reply
    assert "Antrean" in reply
    assert "Episode memori" in reply


def test_riwayat_empty_then_filled(settings: Settings, notifier) -> None:
    daemon = _daemon(settings, notifier)
    assert "Belum ada riwayat" in handle_command(daemon, "/riwayat")["reply"]

    daemon.memory.register_episode(
        "story_kra_050", topic_summary="kantor AI", primary_title="Kantor AI Tanpa Manusia"
    )
    daemon.memory.mark_episode_published(
        "story_kra_050", video_id="vid1", url="https://youtu.be/vid1"
    )
    reply = handle_command(daemon, "/riwayat")["reply"]
    assert "story_kra_050" in reply
    assert "Kantor AI Tanpa Manusia" in reply
    assert "PUBLISHED" in reply
    assert "youtu.be/vid1" in reply


def test_thumb_requires_number(settings: Settings, notifier) -> None:
    daemon = _daemon(settings, notifier)
    result = handle_command(daemon, "/thumb")
    assert "Format salah" in result["reply"]


def test_thumb_missing_file(settings: Settings, notifier) -> None:
    daemon = _daemon(settings, notifier)
    result = handle_command(daemon, "/thumb 3")
    assert "belum dirender" in result["reply"]
    assert "photo" not in result


def test_thumb_selects_and_sends_preview(settings: Settings, notifier) -> None:
    daemon = _daemon(settings, notifier)
    folder = settings.paths.output_ready / "thumbnails"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "thumb_2.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    daemon.state.update(READY_RENDER)

    result = handle_command(daemon, "/thumb 2")
    assert "THUMBNAIL DIPILIH" in result["reply"]
    assert result["photo"].endswith("thumb_2.jpg")
    assert daemon.state.read()["details"]["selected_thumbnail"] == "thumb_2.jpg"


def test_approve_without_script_is_honest(settings: Settings, notifier) -> None:
    daemon = _daemon(settings, notifier)
    result = handle_command(daemon, "/approve")
    assert "Belum ada naskah" in result["reply"]
    assert result["job"] is None


def test_approve_queues_production_when_ready(settings: Settings, notifier) -> None:
    daemon = _daemon(settings, notifier)
    daemon.state.update(READY_RENDER, topic="Kantor AI", script_file="script_production.json")
    result = handle_command(daemon, "/approve")
    assert result["job"] is not None
    job = daemon.jobs[result["job"]["id"]]
    assert job.phase == "production" and job.source == "telegram"
    assert job.topic == "Kantor AI"
    assert daemon.state.status() == RENDERING


def test_approve_waits_for_recording(settings: Settings, notifier) -> None:
    daemon = _daemon(settings, notifier)
    daemon.state.update(WAITING_CREATOR)
    blocked = handle_command(daemon, "/approve")
    assert "menunggu rekaman" in blocked["reply"]
    assert blocked["job"] is None

    (settings.paths.input_creator / "a_roll_raw.mp4").write_bytes(b"footage")
    allowed = handle_command(daemon, "/approve")
    assert allowed["job"] is not None
    assert daemon.jobs[allowed["job"]["id"]].raw_file.endswith("a_roll_raw.mp4")


def test_unknown_command_hints_help(settings: Settings, notifier) -> None:
    daemon = _daemon(settings, notifier)
    reply = handle_command(daemon, "/layani")["reply"]
    assert "tidak dikenali" in reply
    assert "/help" in reply


# ----------------------------------------------------------------- route
def test_webhook_route_dispatches_command(client, notifier) -> None:
    test_client, daemon = client
    daemon.state.update(READY_RENDER)
    response = test_client.post(
        "/kra/telegram-webhook",
        json={"message": {"chat": {"id": 42}, "text": "/status"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["command"] == "status"
    assert READY_RENDER in body["reply"]
    assert notifier.messages  # balasan benar-benar dikirim


def test_webhook_rejects_other_chat(settings: Settings, notifier) -> None:
    daemon = _daemon(settings.with_changes(telegram_chat_id="999"), notifier)
    app = create_app(daemon, autostart=False)
    with TestClient(app) as test_client:
        body = test_client.post(
            "/kra/telegram-webhook",
            json={"message": {"chat": {"id": 111}, "text": "/status"}},
        ).json()
    assert body["status"] == "ignored_unauthorized_chat"
    assert notifier.messages == []


def test_webhook_secret_token_enforced(settings: Settings, notifier) -> None:
    daemon = _daemon(settings.with_changes(telegram_webhook_secret="rahasia"), notifier)
    app = create_app(daemon, autostart=False)
    with TestClient(app) as test_client:
        denied = test_client.post(
            "/kra/telegram-webhook",
            json={"message": {"chat": {"id": 1}, "text": "/help"}},
        )
        allowed = test_client.post(
            "/kra/telegram-webhook",
            json={"message": {"chat": {"id": 1}, "text": "/help"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "rahasia"},
        )
    assert denied.status_code == 403
    assert allowed.status_code == 200 and allowed.json()["command"] == "help"


def test_webhook_ignores_non_text(client) -> None:
    test_client, _ = client
    body = test_client.post("/kra/telegram-webhook", json={"message": {"chat": {"id": 7}}}).json()
    assert body["status"] == "no_text"


# ------------------------------------------------------------ setWebhook
def test_set_webhook_without_token() -> None:
    result = set_webhook("https://contoh.ngrok.app/kra/telegram-webhook", token="")
    assert result["ok"] is False and result["status"] == "NO_TOKEN"


def test_set_webhook_requires_https() -> None:
    result = set_webhook("http://localhost:8100/kra/telegram-webhook", token="123:abc")
    assert result["status"] == "URL_TIDAK_HTTPS"


def test_set_webhook_success_via_fake_transport() -> None:
    calls: list[tuple[str, dict]] = []

    def transport(url: str, payload: dict, timeout: float) -> dict:
        calls.append((url, payload))
        return {"ok": True, "description": "Webhook was set"}

    result = set_webhook(
        "https://contoh.ngrok.app/kra/telegram-webhook",
        token="123:abc",
        secret="rahasia",
        transport=transport,
    )
    assert result["status"] == "WEBHOOK_TERDAFTAR"
    assert calls and calls[0][0].endswith("/bot123:abc/setWebhook")
    assert calls[0][1]["secret_token"] == "rahasia"
    assert calls[0][1]["url"].startswith("https://")


def test_set_webhook_reports_error_honestly() -> None:
    def broken(url: str, payload: dict, timeout: float) -> dict:
        raise OSError("jaringan mati")

    result = set_webhook(
        "https://contoh.ngrok.app/kra/telegram-webhook", token="123:abc", transport=broken
    )
    assert result["status"] == "ERROR"
    assert "jaringan mati" in result["description"]


def test_delete_webhook() -> None:
    def transport(url: str, payload: dict, timeout: float) -> dict:
        assert url.endswith("/deleteWebhook")
        return {"ok": True}

    assert delete_webhook(token="1:a", transport=transport)["status"] == "WEBHOOK_DIHAPUS"
    assert delete_webhook(token="")["status"] == "NO_TOKEN"
