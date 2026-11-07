"""Generator visual otomatis untuk scene Remotion (B-Roll / gambar latar).

Tiga tier, berurutan, tanpa pernah menggagalkan render:

1. **Gambar AI** — DALL·E 3 (bila `STUDIO_IMAGE_API_KEY`/`OPENAI_API_KEY` ada).
2. **Gambar AI gratis** — Flux serverless Pollinations (tanpa API key).
3. **Stock video** — Pexels (`visual_mode: "STOCK_VIDEO"` + `PEXELS_API_KEY`).
4. **Fallback nol-jaringan** — kanvas gelap dari FFmpeg.

Semua kegagalan dicatat di `VisualAsset.notes`; tidak ada yang dipalsukan sebagai
"berhasil". Nama file hasil deterministik (``auto_bg_<scene>.jpg`` /
``auto_broll_<scene>.mp4`` di ``input_creator/``) sehingga render berikutnya memakai
cache alih-alih memanggil API lagi.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings

Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]
Runner = Callable[..., Any]

DALLE_URL = "https://api.openai.com/v1/images/generations"
FLUX_URL = "https://image.pollinations.ai/prompt/{prompt}"
PEXELS_URL = "https://api.pexels.com/videos/search"

MODES = ("AI_IMAGE", "STOCK_VIDEO")
PROVIDERS = ("auto", "dalle", "flux", "pexels", "gradient", "off")
MIN_IMAGE_BYTES = 1024
DEFAULT_CUE = "Abstract dark technology background with glowing nodes"
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass
class VisualAsset:
    """Hasil satu percobaan pembuatan visual."""

    kind: str = "none"  # video | image | none
    path: Path | None = None
    tier: str = "none"  # dalle | flux | pexels | gradient | cache | none
    prompt: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kind != "none" and self.path is not None and self.path.exists()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tier": self.tier,
            "file": self.path.name if self.path else "",
            "bytes": self.path.stat().st_size if self.ok else 0,
            "prompt": self.prompt,
            "notes": self.notes,
        }


def _safe_name(scene_id: str) -> str:
    cleaned = _SAFE.sub("_", (scene_id or "scene").strip()).strip("_")
    return (cleaned or "scene")[:60]


def _default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL API
        return int(response.status), response.read()


def _default_run(cmd: Sequence[str], **kwargs: Any) -> Any:
    return subprocess.run(  # noqa: S603 - argumen dibangun sendiri, tanpa shell
        list(cmd), check=True, capture_output=True, **kwargs
    )


def resolve_provider(settings: Settings) -> str:
    """Tentukan engine visual efektif dari konfigurasi."""
    provider = (settings.broll_provider or "auto").strip().lower()
    if provider in {"off", "none", "0", "false"}:
        return "off"
    if provider not in PROVIDERS:
        return "auto"
    return provider


def _image_prompt(cue: str) -> str:
    return (
        f"cinematic 16:9 widescreen film still, {cue}, dramatic moody lighting, "
        "ultra detailed, photorealistic, 8k, no text or watermark"
    )


def _write(path: Path, data: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def _try_dalle(
    settings: Settings,
    cue: str,
    target: Path,
    transport: Transport,
    notes: list[str],
) -> bool:
    if not settings.image_api_key:
        notes.append("dalle: dilewati (tanpa STUDIO_IMAGE_API_KEY/OPENAI_API_KEY)")
        return False
    payload = json.dumps(
        {
            "model": "dall-e-3",
            "prompt": _image_prompt(cue),
            "n": 1,
            "size": "1792x1024",
            "response_format": "url",
        }
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {settings.image_api_key}",
        "Content-Type": "application/json",
    }
    try:
        status, body = transport("POST", DALLE_URL, headers, payload, 60.0)
        if status != 200:
            notes.append(f"dalle: GAGAL (HTTP {status})")
            return False
        data = json.loads(body.decode("utf-8"))
        image_url = str((data.get("data") or [{}])[0].get("url") or "")
        if not image_url:
            notes.append("dalle: respons tanpa url")
            return False
        status, image = transport("GET", image_url, {}, None, 60.0)
        if status != 200 or len(image) < MIN_IMAGE_BYTES:
            notes.append(f"dalle: unduhan gagal (HTTP {status}, {len(image)} byte)")
            return False
        _write(target, image)
        notes.append(f"dalle: gambar {target.name} ({len(image) // 1024} KB)")
        return True
    except Exception as exc:  # noqa: BLE001 - fallback berikutnya harus tetap jalan
        notes.append(f"dalle: GAGAL ({type(exc).__name__}: {str(exc)[:120]})")
        return False


def _try_flux(cue: str, target: Path, seed: int, transport: Transport, notes: list[str]) -> bool:
    prompt = _image_prompt(cue)
    url = FLUX_URL.format(prompt=urllib.parse.quote(prompt)) + (
        f"?width=1920&height=1080&seed={seed}&model=flux&nologo=true"
    )
    try:
        status, image = transport("GET", url, {}, None, 60.0)
        if status != 200 or len(image) < MIN_IMAGE_BYTES:
            notes.append(f"flux: GAGAL (HTTP {status}, {len(image)} byte)")
            return False
        _write(target, image)
        notes.append(f"flux: gambar {target.name} ({len(image) // 1024} KB)")
        return True
    except Exception as exc:  # noqa: BLE001
        notes.append(f"flux: GAGAL ({type(exc).__name__}: {str(exc)[:120]})")
        return False


def _pick_pexels_file(videos: list[dict[str, Any]]) -> str:
    """Pilih MP4 dengan lebar ~1920 (HD), kalau tidak ada pakai file pertama."""
    for video in videos:
        files = video.get("video_files") or []
        for item in files:
            if int(item.get("width") or 0) == 1920 and str(item.get("file_type")) == "video/mp4":
                return str(item.get("link") or "")
    for video in videos:
        for item in video.get("video_files") or []:
            if str(item.get("file_type")) == "video/mp4" and item.get("link"):
                return str(item["link"])
    return ""


def _try_pexels(
    settings: Settings,
    cue: str,
    target: Path,
    transport: Transport,
    notes: list[str],
) -> bool:
    if not settings.pexels_api_key:
        notes.append("pexels: dilewati (tanpa PEXELS_API_KEY)")
        return False
    query = urllib.parse.urlencode(
        {"query": cue, "orientation": "landscape", "size": "medium", "per_page": 3}
    )
    try:
        status, body = transport(
            "GET", f"{PEXELS_URL}?{query}", {"Authorization": settings.pexels_api_key}, None, 30.0
        )
        if status != 200:
            notes.append(f"pexels: GAGAL (HTTP {status})")
            return False
        videos = (json.loads(body.decode("utf-8")) or {}).get("videos") or []
        link = _pick_pexels_file(videos)
        if not link:
            notes.append("pexels: tidak ada video yang cocok")
            return False
        status, video = transport("GET", link, {}, None, 120.0)
        if status != 200 or len(video) < MIN_IMAGE_BYTES:
            notes.append(f"pexels: unduhan gagal (HTTP {status}, {len(video)} byte)")
            return False
        _write(target, video)
        notes.append(f"pexels: video {target.name} ({len(video) // (1024 * 1024)} MB)")
        return True
    except Exception as exc:  # noqa: BLE001
        notes.append(f"pexels: GAGAL ({type(exc).__name__}: {str(exc)[:120]})")
        return False


def _try_gradient(ffmpeg: str | None, target: Path, run: Runner, notes: list[str]) -> bool:
    if not ffmpeg:
        notes.append("gradient: dilewati (ffmpeg tidak tersedia)")
        return False
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=0x10141d:s=1920x1080:d=1",
        "-frames:v",
        "1",
        str(target),
    ]
    try:
        run(cmd)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"gradient: GAGAL ({type(exc).__name__}: {str(exc)[:120]})")
        return False
    if not target.exists() or target.stat().st_size == 0:
        notes.append("gradient: ffmpeg selesai tanpa file")
        return False
    notes.append(f"gradient: kanvas gelap {target.name}")
    return True


def ensure_visual(
    settings: Settings,
    scene_id: str,
    visual_cue: str = "",
    *,
    mode: str = "AI_IMAGE",
    index: int = 1,
    outdir: Path | None = None,
    force: bool = False,
    transport: Transport | None = None,
    run: Runner | None = None,
    ffmpeg: str | None = None,
) -> VisualAsset:
    """Pastikan scene punya aset visual; kembalikan :class:`VisualAsset`."""
    provider = resolve_provider(settings)
    safe = _safe_name(scene_id)
    cue = (visual_cue or "").strip() or DEFAULT_CUE
    source = (mode or "AI_IMAGE").strip().upper()
    if source not in MODES:
        source = "AI_IMAGE"
    folder = Path(outdir) if outdir else settings.paths.input_creator
    folder.mkdir(parents=True, exist_ok=True)

    asset = VisualAsset(prompt=cue)
    if provider == "off":
        asset.notes.append("visual otomatis dimatikan (STUDIO_BROLL_PROVIDER=off)")
        return asset

    video_target = folder / f"auto_broll_{safe}.mp4"
    image_target = folder / f"auto_bg_{safe}.jpg"
    transport = transport or _default_transport
    run = run or _default_run

    # Cache: file dari run sebelumnya dipakai ulang (render idempoten).
    if not force:
        if source == "STOCK_VIDEO" and video_target.exists() and video_target.stat().st_size > 0:
            asset.kind, asset.path, asset.tier = "video", video_target, "cache"
            asset.notes.append(f"{video_target.name} dipakai ulang dari cache")
            return asset
        if image_target.exists() and image_target.stat().st_size > 0:
            asset.kind, asset.path, asset.tier = "image", image_target, "cache"
            asset.notes.append(f"{image_target.name} dipakai ulang dari cache")
            return asset

    if source == "STOCK_VIDEO" and provider in {"auto", "pexels"}:
        if _try_pexels(settings, cue, video_target, transport, asset.notes):
            asset.kind, asset.path, asset.tier = "video", video_target, "pexels"
            return asset
        asset.notes.append("pexels tidak menghasilkan video; beralih ke gambar AI")

    if provider in {"auto", "dalle"} and _try_dalle(settings, cue, image_target, transport, asset.notes):
        asset.kind, asset.path, asset.tier = "image", image_target, "dalle"
        return asset
    if provider in {"auto", "flux"} and _try_flux(
        cue, image_target, 100 + max(0, int(index)), transport, asset.notes
    ):
        asset.kind, asset.path, asset.tier = "image", image_target, "flux"
        return asset
    if provider in {"auto", "gradient"} and _try_gradient(ffmpeg, image_target, run, asset.notes):
        asset.kind, asset.path, asset.tier = "image", image_target, "gradient"
        return asset

    asset.notes.append("tidak ada visual yang terbentuk; scene memakai latar gelap Remotion")
    return asset
