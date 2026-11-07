from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio.broll import VisualAsset
from studio.config import Settings
from studio.media import Capabilities
from studio.thumbnail import (
    MAX_THUMB_BYTES,
    generate_thumbnails,
    plan_variants,
    primary_thumbnail,
)


@pytest.fixture(autouse=True)
def _paths(settings: Settings) -> None:
    settings.paths.ensure()


def _remotion(settings: Settings, tmp_path: Path) -> Path:
    root = tmp_path / "remotion_studio"
    (root / "public" / "kra_temp").mkdir(parents=True, exist_ok=True)
    return root


def _fake_broll(settings, scene_id, cue, **kwargs):
    target = Path(kwargs["outdir"]) / "auto_bg_thumb_base.jpg"
    target.write_bytes(b"\xff\xd8\xff" + b"b" * 4096)
    return VisualAsset(kind="image", path=target, tier="flux", prompt=cue, notes=["flux: latar dibuat"])


def _fake_npx(fail_index: int | None = None):
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        out = Path(cmd[5])  # npx --yes remotion still <COMPOSITION> <OUT> ...
        index = len(calls)
        if fail_index is not None and index == fail_index:
            raise RuntimeError("remotion crash")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\xff\xd8\xff" + b"x" * 8192)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_plan_variants_uses_brief_fields() -> None:
    brief = {
        "thumbnail_badge": "FAKTA BARU",
        "thumbnail_hooks": ["ini rahasianya", "robot bekerja malam", "nol manusia"],
        "thumbnail_visual_prompt": "dark factory",
    }
    prompt, badge, variants = plan_variants(brief)
    assert prompt == "dark factory"
    assert badge == "FAKTA BARU"
    assert variants[0]["hookText"] == "INI RAHASIANYA"
    assert variants[1]["accentColor"] == "#00F5D4"


def test_plan_variants_falls_back_to_titles_and_defaults() -> None:
    prompt, badge, variants = plan_variants({"title_options": ["Judul satu dua tiga empat lima"]})
    assert prompt.startswith("Dramatic close up")
    assert badge == "TERBONGKAR"
    assert variants[0]["hookText"] == "JUDUL SATU DUA TIGA"

    prompt, badge, variants = plan_variants({}, {"title": "Cerita kantor AI"})
    assert variants[0]["hookText"] == "CERITA KANTOR AI"

    _, _, variants = plan_variants({"thumbnail_hooks": []})
    assert len(variants) == 3


def test_generate_thumbnails_renders_three_variants(settings: Settings, tmp_path: Path) -> None:
    remotion = _remotion(settings, tmp_path)
    configured = settings.with_changes(remotion_dir=str(remotion), thumbnails_enabled=True)
    (settings.paths.workspace / "content_brief.json").write_text(
        json.dumps(
            {
                "thumbnail_badge": "RAHASIA",
                "thumbnail_hooks": ["satu", "dua", "tiga"],
                "thumbnail_visual_prompt": "dark server room",
            }
        ),
        encoding="utf-8",
    )
    run = _fake_npx()
    caps = Capabilities(npx="/usr/bin/npx", remotion_dir=str(remotion))

    report = generate_thumbnails(
        configured, caps, broll=_fake_broll, run=run, notify_preview=False
    )

    assert report.ok
    assert len(report.files) == 3
    assert report.primary is not None and report.primary.name == "thumb_1.jpg"
    assert report.background == "auto_bg_thumb_base.jpg"
    assert list((remotion / "public" / "kra_temp").iterdir()) == []  # staging dibersihkan
    props = json.loads((settings.paths.workspace / "thumb_props_2.json").read_text(encoding="utf-8"))
    assert props["hookText"] == "DUA"
    assert props["subBadgeText"] == "RAHASIA"
    assert props["bgImageSrc"] == "auto_bg_thumb_base.jpg"
    assert "--image-format=jpeg" in run.calls[0]
    assert "YouTubeThumbnail" in run.calls[0]


def test_generate_thumbnails_without_remotion_is_honest(settings: Settings) -> None:
    configured = settings.with_changes(remotion_dir="", thumbnails_enabled=True)
    report = generate_thumbnails(configured, Capabilities(npx="/usr/bin/npx"))
    assert report.ok is False
    assert any("Remotion tidak siap" in note for note in report.notes)
    assert report.as_dict()["status"] == "THUMBNAILS_GAGAL"


def test_generate_thumbnails_reports_partial_failure(settings: Settings, tmp_path: Path) -> None:
    remotion = _remotion(settings, tmp_path)
    configured = settings.with_changes(remotion_dir=str(remotion), thumbnails_enabled=True)

    report = generate_thumbnails(
        configured,
        Capabilities(npx="/usr/bin/npx"),
        broll=_fake_broll,
        run=_fake_npx(fail_index=2),
        notify_preview=False,
    )

    assert report.ok and len(report.files) == 2
    assert any("varian 2 gagal" in note for note in report.notes)


def test_oversized_thumbnail_is_reported(settings: Settings, tmp_path: Path) -> None:
    remotion = _remotion(settings, tmp_path)
    configured = settings.with_changes(remotion_dir=str(remotion), thumbnails_enabled=True)

    def run(cmd, **kwargs):
        out = Path(cmd[5])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * (MAX_THUMB_BYTES + 10))

    report = generate_thumbnails(
        configured, Capabilities(npx="/usr/bin/npx"), broll=_fake_broll, run=run, notify_preview=False
    )

    assert report.ok
    assert report.oversized  # dilaporkan, bukan disembunyikan
    assert any("melebihi batas YouTube" in note for note in report.notes)


def test_preview_photo_is_sent(settings: Settings, tmp_path: Path) -> None:
    remotion = _remotion(settings, tmp_path)
    configured = settings.with_changes(remotion_dir=str(remotion), thumbnails_enabled=True)

    class PhotoNotifier:
        supports_inbound = False

        def __init__(self) -> None:
            self.photos: list[tuple[str, str]] = []

        def send(self, text: str, *, silent: bool = False) -> bool:
            return True

        def send_photo(self, path, caption: str = "") -> bool:
            self.photos.append((Path(path).name, caption))
            return True

    notifier = PhotoNotifier()
    report = generate_thumbnails(
        configured,
        Capabilities(npx="/usr/bin/npx"),
        broll=_fake_broll,
        run=_fake_npx(),
        notifier=notifier,
    )

    assert notifier.photos[0][0] == "thumb_1.jpg"
    assert "THUMBNAIL PREVIEW" in notifier.photos[0][1]
    assert "1280x720" in notifier.photos[0][1]
    assert report.ok


def test_primary_thumbnail_resolution(settings: Settings) -> None:
    assert primary_thumbnail(settings) is None
    target = settings.paths.output_ready / "thumbnails" / "thumb_1.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff" + b"x" * 100)
    assert primary_thumbnail(settings) == target
