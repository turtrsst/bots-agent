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
