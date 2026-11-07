from __future__ import annotations

from pathlib import Path

import pytest

from studio.config import Settings
from studio.media import (
    Capabilities,
    MediaError,
    MediaPipeline,
    detect_capabilities,
    segments_to_srt,
    srt_timestamp,
    subtitle_filter,
)


def test_detect_capabilities_without_binaries(settings: Settings) -> None:
    caps = detect_capabilities(settings, which=lambda _: None, whisper_available=False)
    assert not caps.can_render
    assert not caps.auto_editor
    assert not caps.whisper
    assert "ffmpeg=✗" in caps.summary()


def test_detect_capabilities_with_binaries(settings: Settings, fake_ffmpeg: Path) -> None:
    caps = detect_capabilities(
        settings, which=lambda name: str(fake_ffmpeg) if name == "ffmpeg" else "/usr/bin/" + name,
        whisper_available=True,
    )
    assert caps.can_render
    assert caps.auto_editor
    assert caps.whisper


def test_render_without_ffmpeg_fails_loudly(settings: Settings, tmp_path: Path) -> None:
    source = tmp_path / "raw.mp4"
    source.write_bytes(b"data")
    pipeline = MediaPipeline(settings, Capabilities(), workdir=tmp_path / "work")
    with pytest.raises(MediaError, match="ffmpeg"):
        pipeline.render(source, tmp_path / "out.mp4")


def test_render_missing_input_fails(settings: Settings, tmp_path: Path, fake_ffmpeg: Path) -> None:
    pipeline = MediaPipeline(
        settings, Capabilities(ffmpeg=str(fake_ffmpeg)), workdir=tmp_path / "work"
    )
    with pytest.raises(MediaError, match="tidak ada"):
        pipeline.render(tmp_path / "hilang.mp4", tmp_path / "out.mp4")


def test_pipeline_reports_every_step(
    settings: Settings, tmp_path: Path, fake_ffmpeg: Path
) -> None:
    source = tmp_path / "raw.mp4"
    source.write_bytes(b"real footage")
    target = tmp_path / "out" / "final.mp4"
    pipeline = MediaPipeline(
        settings, Capabilities(ffmpeg=str(fake_ffmpeg)), workdir=tmp_path / "work"
    )

    result = pipeline.render(source, target, hook_title="Hook")

    assert result.ok and target.exists()
    assert result.status_of("normalize_audio") == "ok"
    assert result.status_of("conform") == "ok"
    assert result.status_of("cut_dead_air") == "skipped"
    assert result.status_of("whisper") == "skipped"
    assert result.status_of("remotion") == "skipped"
    assert result.status_of("concat") == "skipped"


def test_failed_step_is_reported_not_hidden(settings: Settings, tmp_path: Path) -> None:
    broken = tmp_path / "broken_ffmpeg"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    source = tmp_path / "raw.mp4"
    source.write_bytes(b"data")
    pipeline = MediaPipeline(settings, Capabilities(ffmpeg=str(broken)), workdir=tmp_path / "work")

    result = pipeline.render(source, tmp_path / "out.mp4")

    assert result.status_of("normalize_audio") == "failed"
    assert any("gagal" in note for note in result.notes)
    assert result.as_dict()["failed_steps"]


def test_srt_timestamp_and_segments() -> None:
    assert srt_timestamp(0) == "00:00:00,000"
    assert srt_timestamp(3661.5) == "01:01:01,500"
    srt = segments_to_srt(
        [
            {"start": 0.5, "end": 2.0, "text": " Halo semua "},
            {"start": 2.0, "end": 3.0, "text": "   "},
            {"start": 3.0, "end": 4.25, "text": "Kita mulai"},
        ]
    )
    assert srt.startswith("1\n00:00:00,500 --> 00:00:02,000\nHalo semua")
    assert "Kita mulai" in srt
    assert srt.count("-->") == 2  # segmen kosong dibuang


def test_subtitle_filter_escapes_path() -> None:
    path = subtitle_filter(Path("/tmp/kantor rama/asset's.srt"))
    assert path.startswith("subtitles='")
    assert "\\'" in path
    assert "force_style=" in path
