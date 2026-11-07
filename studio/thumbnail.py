"""Generator thumbnail YouTube 1280x720 (< 2 MB) lewat `npx remotion still`.

Alur:

1. baca `content_brief.json` (Vito) → badge, 3 hook, prompt visual;
2. hasilkan gambar latar lewat `studio/broll.py` (DALL·E → Flux → kanvas FFmpeg);
3. render 3 variasi warna teks dengan Remotion `Still` (`YouTubeThumbnail`);
4. kirim pratinjau ke Telegram Creator (bila kanal mendukung).

Kegagalan Remotion/aksi apa pun **tidak** menghentikan render video: laporan
mengembalikan `ok=False` beserta alasannya, dan `Sora` tidak memasang thumbnail.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .broll import ensure_visual
from .config import Settings
from .media import Capabilities, detect_capabilities
from .notify import send_photo

STILL_COMPOSITION = "YouTubeThumbnail"
THUMB_WIDTH = 1280
THUMB_HEIGHT = 720
MAX_THUMB_BYTES = 2 * 1024 * 1024  # batas YouTube
THUMB_TIMEOUT = 900
ACCENTS = ("#FFE600", "#00F5D4", "#FF2A54")
DEFAULT_PROMPT = (
    "Dramatic close up of futuristic glowing artificial intelligence core in a dark room, "
    "high contrast, cinematic lighting"
)
DEFAULT_BADGE = "TERBONGKAR"
DEFAULT_HOOKS = ("BEKERJA TANPA MANUSIA", "RAHASIA KANTOR AI", "100% OTOMATIS")
PRIMARY_NAME = "thumb_1.jpg"
CHOICE_FILENAME = "thumbnail_choice.json"  # di workspace/: pilihan A/B dari Telegram/dashboard

Runner = Callable[..., Any]


@dataclass
class ThumbnailReport:
    ok: bool = False
    primary: Path | None = None
    files: list[Path] = field(default_factory=list)
    variants: list[dict[str, Any]] = field(default_factory=list)
    background: str = ""
    notes: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def oversized(self) -> list[str]:
        return [path.name for path in self.files if path.stat().st_size > MAX_THUMB_BYTES]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "THUMBNAILS_READY" if self.ok else "THUMBNAILS_GAGAL",
            "primary_thumbnail": _rel(self.primary),
            "all_thumbnails": [_rel(path) for path in self.files],
            "background": self.background,
            "variants": self.variants,
            "oversized": self.oversized,
            "notes": self.notes,
            "elapsed_seconds": round(self.elapsed, 2),
        }


def _rel(path: Path | None) -> str:
    return path.name if path else ""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _shorten(text: str, words: int = 4, limit: int = 28) -> str:
    cleaned = " ".join(str(text or "").replace("\n", " ").split())
    return " ".join(cleaned.upper().split()[:words])[:limit]


def plan_variants(
    brief: dict[str, Any], script: dict[str, Any] | None = None, *, count: int = 3
) -> tuple[str, str, list[dict[str, str]]]:
    """Ambil (prompt visual, badge, daftar varian hook+warna) dari brief/naskah."""
    prompt = str(brief.get("thumbnail_visual_prompt") or DEFAULT_PROMPT).strip() or DEFAULT_PROMPT
    badge = str(brief.get("thumbnail_badge") or "").strip() or DEFAULT_BADGE

    raw_hooks = brief.get("thumbnail_hooks") or []
    hooks = [_shorten(str(hook)) for hook in raw_hooks if str(hook).strip()] if isinstance(raw_hooks, list) else []
    hooks = [hook for hook in hooks if hook]

    if not hooks:
        titles = brief.get("title_options") or brief.get("titles") or []
        if isinstance(titles, list):
            for entry in titles:
                title = entry.get("title") if isinstance(entry, dict) else entry
                if str(title or "").strip():
                    hooks.append(_shorten(str(title)))
    script = script or {}
    if not hooks and str(script.get("title") or "").strip():
        hooks.append(_shorten(str(script["title"])))
    if not hooks:
        hooks = list(DEFAULT_HOOKS)
    while len(hooks) < count:
        hooks.append(DEFAULT_HOOKS[len(hooks) % len(DEFAULT_HOOKS)])

    variants = [
        {"hookText": hook, "accentColor": ACCENTS[index % len(ACCENTS)]}
        for index, hook in enumerate(hooks[:count])
    ]
    return prompt, badge, variants


def _stage(temp: Path, asset: Path) -> Path:
    temp.mkdir(parents=True, exist_ok=True)
    staged = temp / asset.name
    shutil.copy2(asset, staged)
    return staged


def generate_thumbnails(
    settings: Settings,
    caps: Capabilities | None = None,
    *,
    brief_name: str = "content_brief.json",
    script: dict[str, Any] | None = None,
    count: int = 3,
    npx: str | None = None,
    broll: Callable[..., Any] | None = None,
    run: Runner | None = None,
    notifier: Any | None = None,
    notify_preview: bool = True,
    force: bool = False,
) -> ThumbnailReport:
    """Render thumbnail YouTube; tidak pernah melempar kecuali `Settings` rusak."""
    started = time.monotonic()
    caps = caps or detect_capabilities(settings)
    paths = settings.paths.ensure()
    report = ThumbnailReport()

    brief = _load_json(paths.workspace / brief_name)
    prompt, badge, variants = plan_variants(brief, script, count=max(1, min(count, 3)))
    report.variants = variants

    remotion_dir = Path(settings.remotion_dir).expanduser() if settings.remotion_dir else None
    if not remotion_dir or not remotion_dir.is_dir():
        report.notes.append("Remotion tidak siap: STUDIO_REMOTION_DIR kosong / bukan folder")
        report.elapsed = time.monotonic() - started
        return report
    temp = remotion_dir / "public" / "kra_temp"
    npx = npx or caps.npx
    if not npx:
        report.notes.append("npx tidak ditemukan; thumbnail tidak bisa dirender")
        report.elapsed = time.monotonic() - started
        return report

    # 1) Latar AI (tier yang sama dengan B-Roll; hasil di-cache di workspace/)
    background_name = ""
    asset = None
    try:
        asset = (broll or ensure_visual)(
            settings,
            "thumb_base",
            prompt,
            mode="AI_IMAGE",
            index=777,
            outdir=paths.workspace,
            force=force,
            ffmpeg=caps.ffmpeg,
        )
    except Exception as exc:  # noqa: BLE001 - latar gagal bukan alasan membatalkan thumbnail
        report.notes.append(f"visual latar gagal: {type(exc).__name__}: {str(exc)[:140]}")
    if asset is not None:
        report.notes.extend(asset.notes)
        if getattr(asset, "ok", False) and getattr(asset, "path", None):
            background_name = Path(asset.path).name
            report.background = background_name
        else:
            report.notes.append("latar AI tidak terbentuk; thumbnail memakai gradient gelap")

    staged: Path | None = None
    if background_name:
        staged = _stage(temp, paths.workspace / background_name)

    outdir = paths.output_ready / "thumbnails"
    outdir.mkdir(parents=True, exist_ok=True)
    runner = run or _default_run
    files: list[Path] = []

    # 2) Render tiap varian dengan `npx remotion still`
    for index, variant in enumerate(variants, start=1):
        props = {
            "hookText": variant["hookText"],
            "subBadgeText": badge,
            "bgImageSrc": background_name,
            "accentColor": variant["accentColor"],
        }
        props_path = paths.workspace / f"thumb_props_{index}.json"
        props_path.write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")
        target = outdir / f"thumb_{index}.jpg"
        target.unlink(missing_ok=True)
        cmd = [
            npx,
            "--yes",
            "remotion",
            "still",
            STILL_COMPOSITION,
            str(target.resolve()),
            f"--props={props_path.resolve()}",
            "--image-format=jpeg",
            "--quality=90",
            "--log=error",
        ]
        try:
            runner(cmd, cwd=str(remotion_dir), timeout=THUMB_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            report.notes.append(f"remotion still varian {index} gagal: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        if not target.exists() or target.stat().st_size == 0:
            report.notes.append(f"remotion still varian {index} selesai tanpa file")
            continue
        size = target.stat().st_size
        if size > MAX_THUMB_BYTES:
            report.notes.append(
                f"{target.name} {size / (1024 * 1024):.2f} MB melebihi batas YouTube 2 MB"
            )
        files.append(target)

    # 3) Bersihkan staging
    if staged is not None:
        staged.unlink(missing_ok=True)

    report.files = files
    report.primary = files[0] if files else None
    report.ok = bool(files)

    # 4) Pratinjau ke Telegram
    if report.ok and notify_preview and notifier is not None:
        caption = format_preview_caption(report)
        if not send_photo(notifier, report.primary, caption):
            report.notes.append("pratinjau thumbnail tidak terkirim ke Telegram")

    report.elapsed = time.monotonic() - started
    return report


def format_preview_caption(report: ThumbnailReport) -> str:
    primary = report.primary
    size_kb = round(primary.stat().st_size / 1024, 1) if primary else 0
    hook = report.variants[0]["hookText"] if report.variants else "-"
    return (
        "🖼️ <b>[KRA THUMBNAIL PREVIEW]</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Hook Text:</b> <code>{hook}</code>\n"
        f"📐 <b>Resolusi:</b> {THUMB_WIDTH}x{THUMB_HEIGHT} ({size_kb} KB)\n"
        f"📦 <b>Variasi A/B Test:</b> {len(report.files)} file di "
        "<code>output_ready/thumbnails/</code>"
    )


def _thumb_path(settings: Settings, name: str) -> Path | None:
    """Path thumbnail yang valid (ada, JPEG, < 2 MB) atau ``None``."""
    candidate = settings.paths.output_ready / "thumbnails" / Path(str(name)).name
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate
    return None


def thumbnail_variants(settings: Settings) -> list[dict[str, Any]]:
    """Semua ``thumb_<n>.jpg`` di output_ready/thumbnails, urut nomor."""
    folder = settings.paths.output_ready / "thumbnails"
    found: list[dict[str, Any]] = []
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("thumb_*.jpg")):
        digits = path.stem.split("_")[-1]
        if not digits.isdigit():
            continue
        found.append({"index": int(digits), "file": path.name, "path": path})
    return found


def choose_thumbnail(settings: Settings, index: int, *, via: str = "cli") -> Path | None:
    """Simpan pilihan variasi A/B ke ``workspace/thumbnail_choice.json``."""
    target = _thumb_path(settings, f"thumb_{int(index)}.jpg")
    if target is None:
        return None
    choice = {
        "file": target.name,
        "chosen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "via": via,
    }
    path = settings.paths.workspace / CHOICE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(choice, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def selected_thumbnail(settings: Settings) -> Path | None:
    """Thumbnail pilihan Creator; jatuh ke ``thumb_1.jpg`` bila belum memilih."""
    choice_path = settings.paths.workspace / CHOICE_FILENAME
    if choice_path.is_file():
        try:
            choice = json.loads(choice_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            choice = {}
        name = str(choice.get("file") or "")
        if name:
            chosen = _thumb_path(settings, name)
            if chosen is not None:
                return chosen
    return _thumb_path(settings, PRIMARY_NAME)


def primary_thumbnail(settings: Settings) -> Path | None:
    """File thumbnail utama yang dipasang Sora saat upload (bila ada)."""
    return selected_thumbnail(settings)


def _default_run(cmd: list[str], *, cwd: str | None = None, timeout: float = THUMB_TIMEOUT) -> Any:
    import subprocess

    return subprocess.run(  # noqa: S603 - argumen dibangun sendiri, tanpa shell
        cmd, cwd=cwd, check=True, capture_output=True, timeout=timeout
    )
