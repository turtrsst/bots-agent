from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio.config import Settings
from studio.notify import (
    ConsoleNotifier,
    TelegramNotifier,
    format_script_ready,
    format_upload_done,
    notify_script_ready,
    notify_youtube_uploaded,
    send_html,
)


@pytest.fixture(autouse=True)
def _studio_paths(settings: Settings) -> None:
    """Folder workspace/input/output harus ada sebelum menulis artefak."""
    settings.paths.ensure()


class Recorder:
    """Transport palsu: simpan payload, balas {"ok": true}."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, payload: dict, timeout: float) -> dict:
        self.calls.append((url, payload))
        return {"ok": self.ok, "result": {"message_id": len(self.calls)}}


class DocRecorder:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, dict, str, bytes]] = []

    def __call__(self, url: str, fields: dict, filename: str, data: bytes, timeout: float) -> dict:
        self.calls.append((url, fields, filename, data))
        return {"ok": self.ok}


def _telegram(settings: Settings, *, ok: bool = True) -> tuple[TelegramNotifier, Recorder, DocRecorder]:
    transport = Recorder(ok=ok)
    documents = DocRecorder(ok=ok)
    notifier = TelegramNotifier(
        "token-uji", "12345", transport=transport, document_transport=documents
    )
    return notifier, transport, documents


# ------------------------------------------------------------------ transport
def test_send_html_uses_html_parse_mode() -> None:
    notifier, transport, _ = _telegram(Settings.from_env(env={}))
    assert notifier.send_html("<b>Halo</b> dunia")

    url, payload = transport.calls[0]
    assert url.endswith("/sendMessage")
    assert payload["parse_mode"] == "HTML"
    assert payload["chat_id"] == "12345"
    assert payload["disable_web_page_preview"] == "true"


def test_send_html_reports_failure() -> None:
    notifier, _, _ = _telegram(Settings.from_env(env={}), ok=False)
    assert notifier.send_html("halo") is False


def test_send_document_multipart(tmp_path: Path) -> None:
    notifier, _, documents = _telegram(Settings.from_env(env={}))
    file = tmp_path / "script_production.json"
    file.write_text('{"title": "Uji"}', encoding="utf-8")

    assert notifier.send_document(file, caption="<b>Naskah</b>")
    _, fields, filename, data = documents.calls[0]
    assert fields["chat_id"] == "12345"
    assert fields["parse_mode"] == "HTML"
    assert filename == "script_production.json"
    assert b'"Uji"' in data


def test_send_document_missing_file_is_reported(tmp_path: Path) -> None:
    notifier, _, documents = _telegram(Settings.from_env(env={}))
    assert notifier.send_document(tmp_path / "tidak-ada.json") is False
    assert documents.calls == []


def test_send_html_falls_back_for_plain_notifiers() -> None:
    class Plain:
        supports_inbound = False

        def __init__(self) -> None:
            self.messages: list[str] = []

        def send(self, text: str, *, silent: bool = False) -> bool:
            self.messages.append(text)
            return True

    plain = Plain()
    assert send_html(plain, "<b>Halo</b>")  # type: ignore[arg-type]
    assert plain.messages == ["Halo"]  # tag dibuang untuk kanal tanpa HTML


def test_console_notifier_strips_tags(capsys) -> None:
    ConsoleNotifier().send_html("<b>Judul</b>\n&lt;aman&gt;")
    out = capsys.readouterr().out
    assert "Judul" in out and "<b>" not in out and "<aman>" in out


# ------------------------------------------------------------------- format
def test_format_script_ready_modes() -> None:
    waiting = format_script_ready("Judul", 3, require_aroll=True)
    assert "MENUNGGU A-ROLL" in waiting and "3 Scene" in waiting
    faceless = format_script_ready("Judul", 3, require_aroll=False)
    assert "FULL AUTO" in faceless


def test_format_upload_done_marks_simulation() -> None:
    live = format_upload_done(
        "Judul", privacy="unlisted", video_url="https://youtu.be/x",
        studio_url="https://studio.youtube.com/video/x/edit", engine="Whisper + Remotion + Concat",
    )
    assert "BERHASIL DI-UPLOAD" in live and "https://youtu.be/x" in live
    assert "SIMULASI" not in live

    fake = format_upload_done(
        "Judul", privacy="unlisted", video_url="", studio_url="https://studio.youtube.com/",
        engine="Whisper", simulated=True,
    )
    assert "SIMULASI" in fake


# ---------------------------------------------------------------- pipeline
def _write_script(settings: Settings, title: str = "Kantor Tanpa Manusia", scenes: int = 2) -> Path:
    path = settings.paths.workspace / "script_production.json"
    path.write_text(
        json.dumps({"title": title, "scenes": [{"scene_id": f"scene_{i}"} for i in range(scenes)]}),
        encoding="utf-8",
    )
    return path


def test_notify_script_ready_sends_message_and_attachment(settings: Settings) -> None:
    notifier, transport, documents = _telegram(settings)
    _write_script(settings, scenes=4)
    (settings.paths.workspace / "creator_todo_list.md").write_text("- rekam", encoding="utf-8")

    result = notify_script_ready(notifier, settings.paths, require_aroll=True)

    assert result.startswith("NOTIFIKASI TELEGRAM NASKAH TERKIRIM")
    message = transport.calls[0][1]["text"]
    assert "NASKAH CERITA SELESAI" in message
    assert "Kantor Tanpa Manusia" in message
    assert "4 Scene" in message
    assert [call[2] for call in documents.calls] == ["script_production.json", "creator_todo_list.md"]


def test_notify_script_ready_without_script_is_honest(settings: Settings) -> None:
    notifier, transport, documents = _telegram(settings)
    result = notify_script_ready(notifier, settings.paths, require_aroll=False)
    assert "TERKIRIM" in result
    assert "(judul belum tersedia)" in transport.calls[0][1]["text"]
    assert documents.calls == []


def test_notify_youtube_uploaded_reads_receipt(settings: Settings) -> None:
    notifier, transport, _ = _telegram(settings)
    receipt = {
        "status": "UPLOADED_LIVE",
        "video_id": "abc123",
        "url": "https://youtu.be/abc123",
        "studio_url": "https://studio.youtube.com/video/abc123/edit",
        "title": "Video Uji",
        "privacy": "private",
    }
    (settings.paths.output_ready / "youtube_publish_receipt.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    (settings.paths.output_ready / "final_render.render.json").write_text(
        json.dumps({"engine": "remotion"}), encoding="utf-8"
    )

    result = notify_youtube_uploaded(notifier, settings.paths)

    assert result.startswith("NOTIFIKASI TELEGRAM YOUTUBE TERKIRIM")
    message = transport.calls[0][1]["text"]
    assert "https://youtu.be/abc123" in message
    assert "studio.youtube.com/video/abc123/edit" in message
    assert "PRIVATE" in message
    assert "Whisper + Remotion + Concat" in message
    assert "SIMULASI" not in message


def test_notify_youtube_uploaded_marks_simulation(settings: Settings) -> None:
    notifier, transport, _ = _telegram(settings)
    receipt = {"status": "UPLOADED_SIMULATION", "title": "Sim", "privacy": "unlisted"}
    (settings.paths.output_ready / "youtube_publish_receipt.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )

    notify_youtube_uploaded(notifier, settings.paths)
    message = transport.calls[0][1]["text"]
    assert "SIMULASI" in message
    assert "link belum tersedia" in message


def test_notify_reports_failed_delivery(settings: Settings) -> None:
    notifier, _, _ = _telegram(settings, ok=False)
    _write_script(settings)
    assert "GAGAL" in notify_script_ready(notifier, settings.paths)
    assert "GAGAL" in notify_youtube_uploaded(notifier, settings.paths)


def test_send_photo_uses_multipart_photo_field(tmp_path: Path) -> None:
    transport = Recorder()
    photos = DocRecorder()
    notifier = TelegramNotifier(
        "token-uji", "12345", transport=transport, photo_transport=photos
    )
    image = tmp_path / "thumb_1.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"x" * 100)

    assert notifier.send_photo(image, caption="<b>Preview</b>")
    url, fields, filename, data = photos.calls[0]
    assert url.endswith("/sendPhoto")
    assert filename == "thumb_1.jpg"
    assert fields["parse_mode"] == "HTML"
    assert data.startswith(b"\xff\xd8\xff")


def test_send_photo_missing_file_is_reported(tmp_path: Path) -> None:
    photos = DocRecorder()
    notifier = TelegramNotifier("t", "1", document_transport=photos, photo_transport=photos)
    assert notifier.send_photo(tmp_path / "tidak-ada.jpg") is False
    assert photos.calls == []


def test_send_photo_helper_ignores_plain_notifiers(tmp_path: Path) -> None:
    from studio.notify import send_photo

    class Plain:
        supports_inbound = False

        def send(self, text: str, *, silent: bool = False) -> bool:
            return True

    image = tmp_path / "x.jpg"
    image.write_bytes(b"x")
    assert send_photo(Plain(), image) is False  # type: ignore[arg-type]
