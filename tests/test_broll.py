from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio.broll import PROVIDERS, _pick_pexels_file, ensure_visual, resolve_provider
from studio.config import Settings

IMAGE_BYTES = b"\xff\xd8\xff" + b"x" * 4096
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"v" * 8192


class Transport:
    """Transport palsu berbasis aturan URL → (status, bytes)."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, headers, body, timeout: float):
        self.calls.append((method, url))
        for needle, response in self.routes.items():
            if needle in url:
                return response
        raise AssertionError(f"URL tidak terduga: {url}")


@pytest.fixture(autouse=True)
def _paths(settings: Settings) -> None:
    settings.paths.ensure()


def test_resolve_provider(settings: Settings) -> None:
    assert resolve_provider(settings.with_changes(broll_provider="off")) == "off"
    assert resolve_provider(settings.with_changes(broll_provider="FLUX")) == "flux"
    assert resolve_provider(settings.with_changes(broll_provider="ngawur")) == "auto"
    for provider in PROVIDERS:
        assert resolve_provider(settings.with_changes(broll_provider=provider)) in PROVIDERS


def test_off_provider_does_nothing(settings: Settings) -> None:
    configured = settings.with_changes(broll_provider="off")
    asset = ensure_visual(configured, "scene_1", "server room")
    assert asset.kind == "none" and asset.path is None
    assert any("dimatikan" in note for note in asset.notes)


def test_flux_generates_image(settings: Settings) -> None:
    configured = settings.with_changes(broll_provider="flux")
    transport = Transport({"pollinations": (200, IMAGE_BYTES)})

    asset = ensure_visual(configured, "scene_01_intro", "factory in the dark", index=1, transport=transport)

    assert asset.kind == "image" and asset.tier == "flux"
    assert asset.path is not None and asset.path.name == "auto_bg_scene_01_intro.jpg"
    assert asset.path.read_bytes() == IMAGE_BYTES
    url = transport.calls[0][1]
    assert "width=1920" in url and "height=1080" in url and "seed=101" in url


def test_dalle_is_used_when_key_exists(settings: Settings) -> None:
    configured = settings.with_changes(broll_provider="auto", image_api_key="sk-gambar")
    transport = Transport(
        {
            "api.openai.com": (200, json.dumps({"data": [{"url": "https://cdn.local/x.jpg"}]}).encode()),
            "cdn.local": (200, IMAGE_BYTES),
        }
    )

    asset = ensure_visual(configured, "scene_2", "robotic arms", transport=transport)

    assert asset.tier == "dalle" and asset.ok
    assert transport.calls[0][0] == "POST"
    assert any("dalle" in note for note in asset.notes)


def test_dalle_failure_falls_back_to_flux(settings: Settings) -> None:
    configured = settings.with_changes(broll_provider="auto", image_api_key="sk-gambar")
    transport = Transport({"api.openai.com": (500, b"boom"), "pollinations": (200, IMAGE_BYTES)})

    asset = ensure_visual(configured, "scene_3", "control room", transport=transport)

    assert asset.tier == "flux" and asset.ok
    assert any("dalle: GAGAL" in note for note in asset.notes)


def test_stock_video_uses_pexels(settings: Settings) -> None:
    configured = settings.with_changes(broll_provider="auto", pexels_api_key="pk-uji")
    payload = {
        "videos": [
            {
                "video_files": [
                    {"width": 1280, "file_type": "video/mp4", "link": "https://cdn.local/sd.mp4"},
                    {"width": 1920, "file_type": "video/mp4", "link": "https://cdn.local/hd.mp4"},
                ]
            }
        ]
    }
    transport = Transport(
        {
            "api.pexels.com": (200, json.dumps(payload).encode()),
            "cdn.local/hd.mp4": (200, VIDEO_BYTES),
        }
    )

    asset = ensure_visual(
        configured, "scene_02_data", "server room", mode="STOCK_VIDEO", transport=transport
    )

    assert asset.kind == "video" and asset.tier == "pexels"
    assert asset.path is not None and asset.path.name == "auto_broll_scene_02_data.mp4"


def test_pexels_missing_key_falls_back_to_image(settings: Settings) -> None:
    configured = settings.with_changes(broll_provider="auto", pexels_api_key="")
    transport = Transport({"pollinations": (200, IMAGE_BYTES)})

    asset = ensure_visual(configured, "scene_4", "city", mode="STOCK_VIDEO", transport=transport)

    assert asset.kind == "image" and asset.tier == "flux"
    assert any("pexels: dilewati" in note for note in asset.notes)


def test_stock_mode_without_pexels_provider_stays_image(settings: Settings) -> None:
    configured = settings.with_changes(broll_provider="flux", pexels_api_key="pk-uji")
    transport = Transport({"pollinations": (200, IMAGE_BYTES)})

    asset = ensure_visual(configured, "scene_5", "city", mode="STOCK_VIDEO", transport=transport)

    assert asset.tier == "flux"
    assert all("pexels" not in call[1] for call in transport.calls)


def test_gradient_fallback_with_ffmpeg(settings: Settings) -> None:
    configured = settings.with_changes(broll_provider="auto")
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"\xff\xd8\xff" + b"g" * 2048)

    asset = ensure_visual(
        configured, "scene_6", "dark canvas", ffmpeg="/usr/bin/ffmpeg", run=run, transport=Transport({})
    )

    assert asset.tier == "gradient" and asset.ok
    assert "color=c=0x10141d:s=1920x1080:d=1" in calls[0]


def test_everything_fails_is_reported_honestly(settings: Settings) -> None:
    configured = settings.with_changes(broll_provider="auto")
    transport = Transport({"pollinations": (503, b"down")})

    asset = ensure_visual(configured, "scene_7", "x", transport=transport, ffmpeg=None)

    assert asset.kind == "none" and asset.path is None
    assert any("flux: GAGAL" in note for note in asset.notes)
    assert asset.notes[-1].startswith("tidak ada visual")


def test_cache_is_reused(settings: Settings) -> None:
    configured = settings.with_changes(broll_provider="flux")
    cached = settings.paths.input_creator / "auto_bg_scene_8.jpg"
    cached.write_bytes(IMAGE_BYTES)

    asset = ensure_visual(configured, "scene_8", "x", transport=Transport({}))

    assert asset.tier == "cache" and asset.path == cached


def test_unsafe_scene_id_is_sanitised(settings: Settings) -> None:
    configured = settings.with_changes(broll_provider="flux")
    transport = Transport({"pollinations": (200, IMAGE_BYTES)})

    asset = ensure_visual(configured, "../../etc/passwd", "x", transport=transport)

    assert asset.path is not None
    assert asset.path.parent == settings.paths.input_creator
    assert "/" not in asset.path.name and ".." not in asset.path.name


def test_pick_pexels_prefers_hd() -> None:
    videos = [
        {"video_files": [{"width": 640, "file_type": "video/mp4", "link": "sd"}]},
        {"video_files": [{"width": 1920, "file_type": "video/mp4", "link": "hd"}]},
    ]
    assert _pick_pexels_file(videos) == "hd"
    assert _pick_pexels_file([{"video_files": []}]) == ""
