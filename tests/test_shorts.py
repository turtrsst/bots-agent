from __future__ import annotations

from pathlib import Path

import pytest

from studio.config import Settings
from studio.shorts import (
    DEFAULT_DURATION,
    build_filter_complex,
    format_shorts_caption,
    generate_youtube_short,
)


def _master(settings: Settings, name: str = "final_render.mp4") -> Path:
    settings.paths.ensure()
    path = settings.paths.output_ready / name
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 2048)
    return path


def test_missing_master_reports_error(settings: Settings) -> None:
    report = generate_youtube_short(settings)
    assert report.status == "SHORTS_GAGAL"
    assert "tidak ada" in report.error
    assert report.as_dict()["status"] == "SHORTS_GAGAL"


def test_disabled_skips(settings: Settings) -> None:
    _master(settings)
    report = generate_youtube_short(settings.with_changes(shorts_enabled=False))
    assert report.status == "DIMATIKAN"
    assert "DIMATIKAN" in format_shorts_caption(report) or "dilewati" in format_shorts_caption(report)


def test_without_ffmpeg_skips_honestly(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _master(settings)
    monkeypatch.setattr("studio.shorts.shutil.which", lambda _name: None)
    report = generate_youtube_short(settings)
    assert report.status == "SKIP_TANPA_FFMPEG"
    assert report.output is None


def test_fake_runner_writes_vertical_short(settings: Settings) -> None:
    master = _master(settings)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"s" * 4096)
        return None

    report = generate_youtube_short(settings, duration=45, run=fake_run)
    assert report.ok and report.status == "SHORTS_READY"
    assert report.output is not None and report.output.parent.name == "shorts"
    assert report.output.name.startswith("short_")
    assert report.output.stat().st_size > 0
    assert report.duration == 45

    command = calls[0]
    assert command[0] == "ffmpeg"
    assert str(master) in command
    filter_complex = command[command.index("-filter_complex") + 1]
    assert "boxblur=30:30" in filter_complex
    assert "1080:1920" in filter_complex
    assert command[command.index("-t") + 1] == "45"
    assert command[-1] == str(report.output)


def test_duration_clamped_to_shorts_window(settings: Settings) -> None:
    _master(settings)

    def fake_run(command: list[str], **_kwargs):
        Path(command[-1]).write_bytes(b"x")
        return None

    short = generate_youtube_short(settings, duration=5, run=fake_run)
    long = generate_youtube_short(settings, duration=600, output_name="kedua.mp4", run=fake_run)
    assert short.duration == 30
    assert long.duration == 58


def test_output_name_is_sanitised(settings: Settings) -> None:
    _master(settings)

    def fake_run(command: list[str], **_kwargs):
        Path(command[-1]).write_bytes(b"x")
        return None

    report = generate_youtube_short(
        settings, output_name="../../evil", duration=DEFAULT_DURATION, run=fake_run
    )
    assert report.output is not None
    assert report.output.parent == settings.paths.output_ready / "shorts"
    assert report.output.name.startswith("short_")


def test_ffmpeg_failure_is_reported(settings: Settings) -> None:
    _master(settings)

    class Boom(RuntimeError):
        stderr = b"Error opening filters!"

    def fake_run(command: list[str], **_kwargs):
        raise Boom("exit 1")

    report = generate_youtube_short(settings, run=fake_run)
    assert report.status == "SHORTS_GAGAL"
    assert "Error opening filters" in report.error
    assert "SHORTS_GAGAL" in format_shorts_caption(report)


def test_filter_complex_layout() -> None:
    chain = build_filter_complex(24)
    assert chain.count(";") == 2
    assert chain.endswith("overlay=(W-w)/2:(H-h)/2[v]")
    assert "boxblur=24:24" in chain


# --------------------------------------------------- publikasi Shorts (Sora)
def _short_file(settings: Settings, name: str = "short_100.mp4", payload: bytes = b"v" * 1024) -> Path:
    folder = settings.paths.output_ready / "shorts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + payload)
    return path


def test_latest_short_picks_newest(settings: Settings) -> None:
    import os

    from studio.shorts import latest_short

    assert latest_short(settings) is None
    old = _short_file(settings, "short_1.mp4")
    new = _short_file(settings, "short_2.mp4")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))
    assert latest_short(settings) == new


def test_title_has_hashtag_and_limit() -> None:
    from studio.shorts import ensure_shorts_hashtag

    assert ensure_shorts_hashtag("Kantor AI Tanpa Manusia") == "Kantor AI Tanpa Manusia #Shorts"
    assert ensure_shorts_hashtag("Kantor AI #shorts") == "Kantor AI #shorts"
    long_title = ensure_shorts_hashtag("judul " * 40)
    assert len(long_title) <= 100
    assert long_title.lower().endswith("#shorts")


def test_default_title_uses_brief(settings: Settings) -> None:
    import json

    from studio.shorts import default_short_title

    settings.paths.workspace.mkdir(parents=True, exist_ok=True)
    (settings.paths.workspace / "content_brief.json").write_text(
        json.dumps({"titles": [{"title": "Kantor AI Tanpa Manusia"}]}), encoding="utf-8"
    )
    assert default_short_title(settings) == "Kantor AI Tanpa Manusia #Shorts"


def test_upload_simulation_records_manifest(settings: Settings) -> None:
    import json

    from studio.tools import StudioTools

    short = _short_file(settings)
    studio = StudioTools(settings)
    result = studio.upload_youtube_short(privacy_status="unlisted")
    assert result.startswith("UPLOAD SHORTS (SIMULASI)")

    manifest = json.loads((settings.paths.workspace / "shorts_published.json").read_text())
    record = manifest[short.name]
    assert record["status"] == "UPLOADED_SIMULATION"
    assert record["title"].endswith("#Shorts")
    receipt = json.loads(
        (settings.paths.output_ready / "shorts_publish_receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["privacy"] == "unlisted"

    again = studio.upload_youtube_short()
    assert again.startswith("SHORTS SUDAH TAYANG")
    forced = studio.upload_youtube_short(force=True)
    assert forced.startswith("UPLOAD SHORTS (SIMULASI)")


def test_upload_without_shorts_is_honest(settings: Settings) -> None:
    from studio.tools import StudioTools

    result = StudioTools(settings).upload_youtube_short()
    assert result.startswith("TIDAK ADA SHORTS")
    assert "output_ready/shorts/" in result


def test_upload_live_uses_api_and_skips_thumbnail(settings: Settings, monkeypatch) -> None:
    from studio.tools import StudioTools

    _short_file(settings)
    (settings.paths.root / "client_secret.json").write_text("{}", encoding="utf-8")
    calls: list[dict] = []

    def fake_insert(self, video, title, description, tags, privacy, **kwargs):
        calls.append(
            {
                "video": video,
                "title": title,
                "privacy": privacy,
                "apply_thumbnail": kwargs.get("apply_thumbnail"),
            }
        )
        return {"id": "short123"}

    monkeypatch.setattr(StudioTools, "_youtube_insert", fake_insert)
    result = StudioTools(settings).upload_youtube_short(title="Kantor AI")
    assert result.startswith("SHORTS TERPUBLIKASI")
    assert calls[0]["privacy"] == "unlisted"
    assert calls[0]["apply_thumbnail"] is False
    assert calls[0]["title"] == "Kantor AI #Shorts"
    assert "https://youtu.be/short123" in result


def test_upload_live_error_is_reported(settings: Settings, monkeypatch) -> None:
    from studio.tools import StudioTools

    _short_file(settings)
    (settings.paths.root / "client_secret.json").write_text("{}", encoding="utf-8")

    def broken(self, *_args, **_kwargs):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(StudioTools, "_youtube_insert", broken)
    result = StudioTools(settings).upload_youtube_short()
    assert result.startswith("ERROR UPLOAD SHORTS")
    assert "Jangan klaim" in result
    manifest = settings.paths.workspace / "shorts_published.json"
    assert not manifest.exists()  # gagal tidak dicatat sebagai tayang
