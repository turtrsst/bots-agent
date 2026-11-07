from __future__ import annotations

from pathlib import Path

import pytest

from studio.config import Settings
from studio.tts import TTSError, normalize_narration, resolve_provider, synthesize


def eleven_ok(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, bytes]:
    assert "text-to-speech" in url
    assert payload["text"]
    return 200, b"ID3-eleven"


def eleven_down(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, bytes]:
    return 402, b"quota exceeded"


def edge_ok(text: str, path: Path, voice: str, rate: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3-edge")


def test_resolve_provider(settings: Settings) -> None:
    assert resolve_provider(settings) == "edge"  # tanpa key
    assert resolve_provider(settings.with_changes(elevenlabs_api_key="xi")) == "elevenlabs"
    assert resolve_provider(settings.with_changes(tts_provider="edge", elevenlabs_api_key="xi")) == "edge"
    assert resolve_provider(settings.with_changes(tts_provider="off")) == "off"


def test_elevenlabs_primary(settings: Settings, tmp_path: Path, fake_ffmpeg: Path) -> None:
    configured = settings.with_changes(elevenlabs_api_key="xi-test")
    narration = synthesize(
        configured,
        "Kantor yang berjalan sendiri.",
        tmp_path,
        scene_id="scene_1",
        ffmpeg=str(fake_ffmpeg),
        transport=eleven_ok,
    )
    assert narration.provider == "elevenlabs"
    assert narration.path.name == "tts_norm_scene_1.mp3"
    assert narration.path.exists()
    assert not (tmp_path / "tts_raw_scene_1.mp3").exists()  # mentah dibersihkan


def test_falls_back_to_edge_when_elevenlabs_fails(settings: Settings, tmp_path: Path, fake_ffmpeg: Path) -> None:
    configured = settings.with_changes(elevenlabs_api_key="xi-test")
    narration = synthesize(
        configured,
        "Suara cadangan.",
        tmp_path,
        scene_id="scene_2",
        ffmpeg=str(fake_ffmpeg),
        transport=eleven_down,
        edge=edge_ok,
    )
    assert narration.provider == "edge"
    assert any("elevenlabs: GAGAL" in attempt for attempt in narration.attempts)
    assert narration.path.exists()


def test_no_engine_available_is_an_honest_error(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(TTSError, match="edge-tts|gagal"):
        synthesize(settings, "Teks", tmp_path, scene_id="x", ffmpeg=None)


def test_off_and_empty_text(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(TTSError, match="dimatikan"):
        synthesize(settings.with_changes(tts_provider="off"), "Teks", tmp_path, scene_id="x")
    with pytest.raises(TTSError, match="kosong"):
        synthesize(settings, "   ", tmp_path, scene_id="x")


def test_normalization_failure_keeps_raw_audio(
    settings: Settings, tmp_path: Path
) -> None:
    def broken_run(cmd, timeout=600.0):
        raise TTSError("ffmpeg exit 1")

    narration = synthesize(
        settings,
        "Teks narasi.",
        tmp_path,
        scene_id="scene_3",
        ffmpeg="/usr/bin/ffmpeg",
        edge=edge_ok,
        run=broken_run,
    )
    assert narration.provider == "edge"
    assert any("normalisasi gagal" in note for note in narration.notes)
    assert narration.path.exists()


def test_normalize_command(tmp_path: Path, fake_ffmpeg: Path) -> None:
    raw = tmp_path / "raw.mp3"
    raw.write_bytes(b"ID3")
    target = tmp_path / "norm.mp3"
    calls: list[list[str]] = []

    def run(cmd, timeout=600.0):
        calls.append(cmd)
        cmd[-1] and Path(cmd[-1]).write_bytes(b"ID3norm")

    normalize_narration(str(fake_ffmpeg), raw, target, run=run)
    assert target.exists()
    command = calls[0]
    assert "loudnorm=I=-14:TP=-1.5:LRA=11" in " ".join(command)
    assert "silenceremove" in " ".join(command)
