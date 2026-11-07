from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio.config import Settings
from studio.media import Capabilities
from studio.story import (
    StoryBuilder,
    StoryError,
    extract_whisper_words,
    safe_staged_name,
    validate_story,
)
from studio.tools import SCENE_FILENAME, StudioTools

STORY = {
    "project_id": "story_kra_001",
    "title": "Rahasia di Balik Kantor AI Otonom",
    "fps": 30,
    "scenes": [
        {
            "scene_id": "scene_1_hook",
            "type": "REMOTION_DYNAMIC",
            "composition_id": "StoryScene",
            "narration_text": "Pernahkah kamu membayangkan kantor yang berjalan sendiri saat kamu tidur?",
            "visual_cue": "Dark futuristic server room with glowing neon nodes",
            "broll_asset": "broll_server.mp4",
            "highlight_words": ["berjalan sendiri", "tidur"],
        },
        {
            "scene_id": "scene_2_aroll",
            "type": "CREATOR_AROLL",
            "composition_id": "CreatorOverlay",
            "source_video": "a_roll_raw.mp4",
            "lower_third_text": "Rama - Founder KRA",
            "narration_text": "Ini yang terjadi tiap pagi di kantor kami.",
        },
    ],
}


# ------------------------------------------------------------------ skema
def test_schema_accepts_example_script() -> None:
    report = validate_story(STORY)
    assert report.ok, report.problems
    assert report.script is not None
    assert [scene.scene_id for scene in report.script.scenes] == ["scene_1_hook", "scene_2_aroll"]
    assert report.script.scenes[1].media == "a_roll_raw.mp4"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda data: data.pop("project_id"), "project_id"),
        (lambda data: data.update(scenes=[]), "scenes"),
        (lambda data: data["scenes"][0].update(type="MAGIC"), "tidak dikenal"),
        (lambda data: data["scenes"][1].update(source_video="/etc/passwd"), "relatif"),
        (lambda data: data["scenes"][0].update(broll_asset="../../secret.mp4"), "relatif"),
        (lambda data: data["scenes"][0].update(scene_id="scene_2_aroll"), "duplikat"),
        (lambda data: data["scenes"][1].pop("source_video"), "CREATOR_AROLL"),
        (lambda data: data.update(fps="cepat"), "fps"),
    ],
)
def test_schema_rejects_broken_scripts(mutation, expected: str) -> None:
    data = json.loads(json.dumps(STORY))
    mutation(data)
    report = validate_story(data)
    assert not report.ok
    assert any(expected in problem for problem in report.problems), report.problems


def test_text_only_scene_is_a_warning_not_an_error() -> None:
    data = json.loads(json.dumps(STORY))
    data["scenes"] = [data["scenes"][0]]
    data["scenes"][0].pop("broll_asset")
    report = validate_story(data)
    assert report.ok
    assert any("tanpa aset visual" in warning for warning in report.warnings)


# ----------------------------------------------------------------- whisper
def test_extract_whisper_words(tmp_path: Path) -> None:
    payload = {
        "segments": [
            {
                "start": 0.0,
                "end": 2.5,
                "words": [
                    {"word": " Kantor ", "start": 0.0, "end": 0.4},
                    {"word": "AI", "start": 0.41, "end": 0.9},
                    {"word": "   ", "start": 1.0, "end": 1.1},
                ],
            },
            {"start": 2.5, "end": 4.125, "words": [{"word": "otonom", "start": 2.5, "end": 2.9}]},
        ]
    }
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    words, duration = extract_whisper_words(path)

    assert [word["word"] for word in words] == ["Kantor", "AI", "otonom"]
    assert words[1]["start"] == 0.41
    assert duration == 4.125


def test_safe_staged_name_sanitises() -> None:
    assert safe_staged_name("scene 1/hook", "broll server.mp4") == "scene_1_hook__broll_server.mp4"
    assert safe_staged_name("scene", "../../etc/passwd").endswith(".mp4")


# ------------------------------------------------------------------ build
@pytest.fixture
def story_settings(settings: Settings, remotion_root: Path) -> Settings:
    configured = settings.with_changes(remotion_dir=str(remotion_root), real_render=True)
    configured.paths.ensure()
    return configured


def _write_story(settings: Settings, data: dict | None = None) -> Path:
    path = settings.paths.workspace / SCENE_FILENAME
    path.write_text(json.dumps(data or STORY), encoding="utf-8")
    return path


def _assets(settings: Settings) -> None:
    (settings.paths.input_creator / "a_roll_raw.mp4").write_bytes(b"a-roll")
    (settings.paths.input_creator / "broll_server.mp4").write_bytes(b"b-roll")


def test_build_renders_each_scene_with_remotion(
    story_settings: Settings, fake_ffmpeg: Path, fake_npx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("studio.story.shutil.which", lambda _name: None)  # tanpa xvfb-run
    caps = Capabilities(ffmpeg=str(fake_ffmpeg), npx=str(fake_npx), remotion_dir=str(story_settings.remotion_dir))
    _assets(story_settings)
    script_path = _write_story(story_settings)
    target = story_settings.paths.output_ready / "final_render.mp4"

    result = StoryBuilder(story_settings, caps).build(script_path, target)

    assert result.ok and target.exists()
    assert result.engine == "remotion"
    assert len(result.scenes) == 2
    assert all(scene.engine == "remotion" for scene in result.scenes)
    # aset disalin ke public/kra_temp lalu dibersihkan lagi
    assert list((Path(story_settings.remotion_dir) / "public" / "kra_temp").iterdir()) == []
    # props memuat skema yang dibaca StoryScene.tsx
    props_files = sorted(story_settings.paths.workspace.glob("props_*.json"))
    assert len(props_files) == 2
    props = json.loads(props_files[0].read_text(encoding="utf-8"))
    assert set(props) >= {"title", "videoSrc", "words", "highlightWords", "durationInSeconds"}
    assert props["videoSrc"].startswith("scene_1_hook__")
    assert props["highlightWords"] == ["berjalan sendiri", "tidur"]
    assert props["durationInSeconds"] == 5.0  # tanpa whisper → durasi default


def test_build_falls_back_to_ffmpeg_without_remotion(
    story_settings: Settings, fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("studio.story.shutil.which", lambda _name: None)
    caps = Capabilities(ffmpeg=str(fake_ffmpeg))  # tanpa npx → tidak ada Remotion
    _assets(story_settings)
    script_path = _write_story(story_settings)
    target = story_settings.paths.output_ready / "final_render.mp4"

    result = StoryBuilder(story_settings, caps).build(script_path, target)

    assert result.ok and target.exists()
    assert result.engine == "ffmpeg-fallback"
    assert all(scene.engine == "ffmpeg" for scene in result.scenes)
    assert any("Remotion tidak siap" in note for note in result.notes)


def test_missing_scene_asset_uses_fallback_source(
    story_settings: Settings, fake_ffmpeg: Path, fake_npx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("studio.story.shutil.which", lambda _name: None)
    caps = Capabilities(ffmpeg=str(fake_ffmpeg), npx=str(fake_npx), remotion_dir=str(story_settings.remotion_dir))
    (story_settings.paths.input_creator / "a_roll_raw.mp4").write_bytes(b"a-roll")
    script_path = _write_story(story_settings)

    result = StoryBuilder(story_settings, caps).build(script_path, story_settings.paths.output_ready / "final.mp4")

    hook = result.scenes[0]
    assert any("broll_server.mp4' tidak ditemukan" in note for note in hook.notes)
    assert hook.source.endswith("a_roll_raw.mp4")


def test_build_rejects_missing_or_invalid_script(story_settings: Settings, fake_ffmpeg: Path, tmp_path: Path) -> None:
    caps = Capabilities(ffmpeg=str(fake_ffmpeg))
    builder = StoryBuilder(story_settings, caps)

    with pytest.raises(StoryError, match="tidak ditemukan"):
        builder.build(tmp_path / "nope.json", tmp_path / "out.mp4")

    broken = tmp_path / "broken.json"
    broken.write_text("{bukan json", encoding="utf-8")
    with pytest.raises(StoryError, match="bukan JSON"):
        builder.build(broken, tmp_path / "out.mp4")

    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"title": "x"}), encoding="utf-8")
    with pytest.raises(StoryError, match="skema"):
        builder.build(invalid, tmp_path / "out.mp4")


# ------------------------------------------------------------------- tools
def test_save_scene_breakdown_validates_and_writes(settings: Settings) -> None:
    tools = StudioTools(settings)

    result = tools.save_scene_breakdown(json.dumps(STORY))

    assert result.startswith("TERSIMPAN")
    assert "2 scene" in result
    saved = json.loads((settings.paths.workspace / SCENE_FILENAME).read_text(encoding="utf-8"))
    assert saved["project_id"] == "story_kra_001"


def test_save_scene_breakdown_reports_schema_errors(settings: Settings) -> None:
    tools = StudioTools(settings)
    broken = json.loads(json.dumps(STORY))
    broken["scenes"][0]["type"] = "MAGIC"

    result = tools.save_scene_breakdown(json.dumps(broken))

    assert result.startswith("ERROR SCHEMA")
    assert not (settings.paths.workspace / SCENE_FILENAME).exists()
    assert tools.save_scene_breakdown("{rusak").startswith("ERROR JSON")


def test_tool_assembles_story_and_writes_sidecar(
    story_settings: Settings,
    fake_ffmpeg: Path,
    fake_npx: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("studio.story.shutil.which", lambda _name: None)
    tools = StudioTools(story_settings)
    monkeypatch.setattr(
        "studio.tools.detect_capabilities",
        lambda _settings: Capabilities(
            ffmpeg=str(fake_ffmpeg), npx=str(fake_npx), remotion_dir=str(story_settings.remotion_dir)
        ),
    )
    _assets(story_settings)
    _write_story(story_settings)

    result = tools.assemble_story_from_script()

    assert result.startswith("RENDER STORY SELESAI")
    report = json.loads(
        (story_settings.paths.output_ready / "final_render.render.json").read_text(encoding="utf-8")
    )
    assert report["engine"] == "remotion"
    assert report["scenes"][0]["scene_id"] == "scene_1_hook"
    assert report["source_script"] == f"workspace/{SCENE_FILENAME}"
    assert tools.inspect_rendered_output("final_render.mp4").startswith("AUDIT LULUS")


def test_tool_without_script_explains_what_is_missing(
    story_settings: Settings, fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = StudioTools(story_settings)
    monkeypatch.setattr(
        "studio.tools.detect_capabilities", lambda _settings: Capabilities(ffmpeg=str(fake_ffmpeg))
    )
    result = tools.assemble_story_from_script()
    assert result.startswith("ERROR RENDER")
    assert SCENE_FILENAME in result


# ------------------------------------------------------- mode faceless (TTS)
FACELESS = {
    "project_id": "story_faceless_001",
    "title": "Kantor yang Tidak Pernah Tidur",
    "fps": 30,
    "scenes": [
        {
            "scene_id": "scene_1_hook",
            "type": "REMOTION_DYNAMIC",
            "composition_id": "StoryScene",
            "narration_text": "Kantor ini tutup pukul lima, tapi videonya tidak pernah tidur.",
            "visual_cue": "Dark server room with neon lights",
            "bg_image": "bg_office.jpg",
            "highlight_words": ["tidak pernah tidur"],
        }
    ],
}


def _fake_tts(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> list[dict]:
    from studio.tts import Narration

    calls: list[dict] = []

    def tts(settings, text, workdir, **kwargs):
        calls.append({"text": text, **kwargs})
        if fail:
            from studio.tts import TTSError

            raise TTSError("semua engine TTS gagal: edge-tts belum terpasang")
        target = Path(workdir) / f"tts_norm_{kwargs['scene_id']}.mp3"
        target.write_bytes(b"ID3tts")
        return Narration(provider="edge", path=target, notes=["narasi: edge"])

    return calls, tts


def test_faceless_scene_narrates_with_tts(
    story_settings: Settings, fake_ffmpeg: Path, fake_npx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("studio.story.shutil.which", lambda _name: None)
    caps = Capabilities(ffmpeg=str(fake_ffmpeg), npx=str(fake_npx), remotion_dir=str(story_settings.remotion_dir))
    (story_settings.paths.input_creator / "bg_office.jpg").write_bytes(b"jpg")
    script_path = _write_story(story_settings, FACELESS)
    calls, tts = _fake_tts(monkeypatch)
    builder = StoryBuilder(story_settings, caps, tts=tts)
    monkeypatch.setattr(
        builder,
        "words_for",
        lambda _source: ([{"word": "tidak", "start": 0.2, "end": 0.6}, {"word": "tidur", "start": 0.7, "end": 1.9}], 1.9),
    )

    result = builder.build(script_path, story_settings.paths.output_ready / "final.mp4")

    assert result.ok
    assert calls and calls[0]["scene_id"] == "scene_1_hook"
    assert calls[0]["text"].startswith("Kantor ini tutup")
    scene = result.scenes[0]
    assert scene.audio_provider == "edge"
    assert scene.audio.endswith("tts_norm_scene_1_hook.mp3")
    assert result.tts_providers == ["edge"]
    props_path = next(story_settings.paths.workspace.glob("props_*_scene_1_hook.json"))
    props = json.loads(props_path.read_text(encoding="utf-8"))
    assert props["audioSrc"] == "scene_1_hook__tts_norm_scene_1_hook.mp3"
    assert props["bgImageSrc"].startswith("scene_1_hook__")
    assert props["videoSrc"] == ""
    assert [word["word"] for word in props["words"]] == ["tidak", "tidur"]
    assert props["durationInSeconds"] == pytest.approx(1.9 + 0.35)
    assert list((Path(story_settings.remotion_dir) / "public" / "kra_temp").iterdir()) == []


def test_tts_failure_is_reported_not_faked(
    story_settings: Settings, fake_ffmpeg: Path, fake_npx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("studio.story.shutil.which", lambda _name: None)
    caps = Capabilities(ffmpeg=str(fake_ffmpeg), npx=str(fake_npx), remotion_dir=str(story_settings.remotion_dir))
    script_path = _write_story(story_settings, FACELESS)
    _calls, tts = _fake_tts(monkeypatch, fail=True)

    result = StoryBuilder(story_settings, caps, tts=tts).build(
        script_path, story_settings.paths.output_ready / "final.mp4"
    )

    scene = result.scenes[0]
    assert scene.audio_provider == ""
    assert any("TTS gagal" in note for note in scene.notes)
    assert result.tts_providers == []
    props_path = next(story_settings.paths.workspace.glob("props_*_scene_1_hook.json"))
    props = json.loads(props_path.read_text(encoding="utf-8"))
    assert props["audioSrc"] == ""


def test_creator_aroll_scene_skips_tts(
    story_settings: Settings, fake_ffmpeg: Path, fake_npx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("studio.story.shutil.which", lambda _name: None)
    caps = Capabilities(ffmpeg=str(fake_ffmpeg), npx=str(fake_npx), remotion_dir=str(story_settings.remotion_dir))
    _assets(story_settings)
    script_path = _write_story(story_settings)
    calls, tts = _fake_tts(monkeypatch)

    result = StoryBuilder(story_settings, caps, tts=tts).build(
        script_path, story_settings.paths.output_ready / "final.mp4"
    )

    assert [call["scene_id"] for call in calls] == ["scene_1_hook"]  # scene A-Roll tidak di-TTS
    aroll = result.scenes[1]
    assert aroll.audio_provider == ""
    assert aroll.audio == ""
