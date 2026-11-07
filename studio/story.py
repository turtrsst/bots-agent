"""Jembatan JSON: naskah Clara → Whisper → Remotion CLI → concat.

Ini pengganti `kra_remotion_concat_bridge.py`: satu skema tetap
(`workspace/script_production.json`) yang dibaca oleh Remotion **dan** modul concat,
tanpa parsing Markdown.

Dua masalah klasik penyambungan Remotion dengan file lokal ditangani di sini:

* **Blokir `file://`** — Chromium milik Remotion tidak boleh membuka path lokal, jadi
  setiap aset disalin dulu ke `remotion_studio/public/kra_temp/`, lalu komponen
  memanggil ``staticFile("kra_temp/...")``.
* **Durasi statis** — panjang tiap scene dihitung dari transkrip Whisper (``max(end)``),
  dikirim lewat props ``durationInSeconds``, dan ``calculateMetadata`` di ``Root.tsx``
  mengubahnya menjadi ``durationInFrames``.

Kalau Remotion/Node tidak tersedia (atau render satu scene gagal), scene itu dirakit
dengan FFmpeg (conform + burn-in SRT) dan hasil akhirnya ditandai
``engine="ffmpeg-fallback"`` — bukan diklaim sebagai render Remotion.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .broll import VisualAsset, ensure_visual
from .config import Settings
from .media import (
    Capabilities,
    MediaError,
    MediaPipeline,
    detect_capabilities,
    segments_to_srt,
    subtitle_filter,
)
from .tts import Narration, TTSError, synthesize

SCENE_TYPES = ("REMOTION_DYNAMIC", "CREATOR_AROLL")
TTS_TAIL_SECONDS = 0.35  # ekor kecil supaya caption terakhir tidak terpotong
DEFAULT_COMPOSITION = "StoryScene"
REMOTION_TIMEOUT = 2400
CONCAT_TIMEOUT = 2400
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


TTSFn = Any  # Callable[[Settings, str, Path, ...], Narration]
BrollFn = Any  # Callable[[Settings, str, str, ...], VisualAsset]
VISUAL_MODES = ("AI_IMAGE", "STOCK_VIDEO")


class StoryError(RuntimeError):
    """Naskah tidak valid atau tidak ada scene yang bisa dirender."""


# =====================================================================
# Skema naskah
# =====================================================================
@dataclass
class Scene:
    scene_id: str
    type: str
    composition_id: str = DEFAULT_COMPOSITION
    narration_text: str = ""
    visual_cue: str = ""
    broll_asset: str = ""
    source_video: str = ""
    lower_third_text: str = ""
    highlight_words: list[str] = field(default_factory=list)
    bg_image: str = ""
    voice_id: str = ""
    visual_mode: str = "AI_IMAGE"

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int) -> Scene:
        return cls(
            scene_id=str(data.get("scene_id") or f"scene_{index + 1}"),
            type=str(data.get("type") or "REMOTION_DYNAMIC").upper(),
            composition_id=str(data.get("composition_id") or DEFAULT_COMPOSITION),
            narration_text=str(data.get("narration_text") or ""),
            visual_cue=str(data.get("visual_cue") or ""),
            broll_asset=str(data.get("broll_asset") or ""),
            source_video=str(data.get("source_video") or ""),
            lower_third_text=str(data.get("lower_third_text") or ""),
            highlight_words=[str(word) for word in (data.get("highlight_words") or [])],
            bg_image=str(data.get("bg_image") or ""),
            voice_id=str(data.get("voice_id") or ""),
            visual_mode=str(data.get("visual_mode") or "AI_IMAGE").upper(),
        )

    @property
    def media(self) -> str:
        """Aset visual utama scene (A-Roll Creator didahulukan)."""
        return self.source_video or self.broll_asset


@dataclass
class StoryScript:
    project_id: str
    title: str
    fps: int
    scenes: list[Scene]


@dataclass
class Validation:
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    script: StoryScript | None = None


def _is_safe_relative(name: str) -> bool:
    if not name:
        return False
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts


def validate_story(data: Any) -> Validation:
    """Periksa skema `script_production.json` sebelum dipakai Raka."""
    problems: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict):
        return Validation(False, ["akar JSON harus sebuah objek"])

    project_id = str(data.get("project_id") or "").strip()
    title = str(data.get("title") or "").strip()
    raw_scenes = data.get("scenes")

    if not project_id:
        problems.append("'project_id' wajib diisi (mis. story_kra_001)")
    if not title:
        problems.append("'title' wajib diisi")
    try:
        fps = int(data.get("fps", 30))
        if not 1 <= fps <= 120:
            problems.append(f"'fps' di luar rentang wajar: {fps}")
    except (TypeError, ValueError):
        fps = 30
        problems.append("'fps' harus angka (mis. 30)")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        problems.append("'scenes' harus array dan tidak boleh kosong")
        raw_scenes = []

    scenes: list[Scene] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_scenes):
        if not isinstance(raw, dict):
            problems.append(f"scenes[{index}] bukan objek")
            continue
        scene = Scene.from_dict(raw, index)
        if scene.scene_id in seen_ids:
            problems.append(f"scene_id duplikat: {scene.scene_id}")
        seen_ids.add(scene.scene_id)
        if scene.type not in SCENE_TYPES:
            problems.append(
                f"{scene.scene_id}: type '{scene.type}' tidak dikenal (pilih {', '.join(SCENE_TYPES)})"
            )
        if not scene.narration_text.strip():
            problems.append(f"{scene.scene_id}: 'narration_text' kosong")
        if scene.source_video and not _is_safe_relative(scene.source_video):
            problems.append(f"{scene.scene_id}: 'source_video' harus nama file relatif, bukan path luar")
        if scene.broll_asset and not _is_safe_relative(scene.broll_asset):
            problems.append(f"{scene.scene_id}: 'broll_asset' harus nama file relatif, bukan path luar")
        if scene.visual_mode not in VISUAL_MODES:
            problems.append(
                f"{scene.scene_id}: 'visual_mode' '{scene.visual_mode}' tidak dikenal "
                f"(pilih {', '.join(VISUAL_MODES)})"
            )
        if scene.bg_image and not _is_safe_relative(scene.bg_image):
            problems.append(f"{scene.scene_id}: 'bg_image' harus nama file relatif, bukan path luar")
        if scene.type == "CREATOR_AROLL" and not scene.media:
            problems.append(f"{scene.scene_id}: CREATOR_AROLL wajib punya 'source_video'")
        if not scene.media and not scene.bg_image:
            warnings.append(
                f"{scene.scene_id}: tanpa aset visual, scene memakai latar gelap + narasi TTS/teks"
            )
        scenes.append(scene)

    if not problems and not any(scene.media for scene in scenes):
        warnings.append("tidak ada scene yang menyebut aset video; semua akan berbasis teks")

    script = None if problems else StoryScript(project_id=project_id, title=title, fps=fps, scenes=scenes)
    return Validation(ok=not problems, problems=problems, warnings=warnings, script=script)


# =====================================================================
# Whisper → words
# =====================================================================
def extract_whisper_words(whisper_json_path: Path) -> tuple[list[dict[str, Any]], float]:
    """Ambil `[{word, start, end}]` + durasi dari JSON mentah Whisper."""
    data = json.loads(Path(whisper_json_path).read_text(encoding="utf-8"))
    words: list[dict[str, Any]] = []
    duration = 0.0
    for segment in data.get("segments", []) or []:
        duration = max(duration, float(segment.get("end") or 0.0))
        for item in segment.get("words", []) or []:
            text = str(item.get("word") or "").strip()
            if not text:
                continue
            words.append(
                {
                    "word": text,
                    "start": round(float(item.get("start") or 0.0), 3),
                    "end": round(float(item.get("end") or 0.0), 3),
                }
            )
    if not duration:
        duration = max((word["end"] for word in words), default=0.0)
    return words, round(duration, 3)


def safe_staged_name(scene_id: str, filename: str) -> str:
    stem = _SAFE_NAME.sub("_", Path(filename).stem)[:60] or "asset"
    suffix = _SAFE_NAME.sub("", Path(filename).suffix)[:8] or ".mp4"
    return f"{_SAFE_NAME.sub('_', scene_id)[:40]}__{stem}{suffix}"


def _concat_line(path: Path) -> str:
    return "file '" + str(path.resolve()).replace("'", "'\\''") + "'"


# =====================================================================
# Hasil
# =====================================================================
@dataclass
class SceneResult:
    scene_id: str
    type: str
    composition_id: str
    output: str = ""
    engine: str = "skipped"  # remotion | ffmpeg | skipped
    source: str = ""
    audio: str = ""
    audio_provider: str = ""
    visual_tier: str = ""
    duration_seconds: float = 0.0
    words: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "type": self.type,
            "composition_id": self.composition_id,
            "output": self.output,
            "engine": self.engine,
            "source": self.source,
            "audio": self.audio,
            "audio_provider": self.audio_provider,
            "visual_tier": self.visual_tier,
            "duration_seconds": round(self.duration_seconds, 2),
            "words": self.words,
            "notes": self.notes,
        }


@dataclass
class StoryResult:
    ok: bool
    output: Path
    engine: str
    project_id: str = ""
    title: str = ""
    fps: int = 30
    scenes: list[SceneResult] = field(default_factory=list)
    specs: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    workdir: str = ""
    tts_providers: list[str] = field(default_factory=list)
    visual_tiers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": str(self.output),
            "engine": self.engine,
            "tts_providers": self.tts_providers,
            "visual_tiers": self.visual_tiers,
            "project_id": self.project_id,
            "title": self.title,
            "fps": self.fps,
            "output_bytes": self.output.stat().st_size if self.output.exists() else 0,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "workdir": self.workdir,
            "scenes": [scene.as_dict() for scene in self.scenes],
            "failed_scenes": [scene.scene_id for scene in self.scenes if not scene.output],
            "notes": self.notes,
            "specs": self.specs,
        }


# =====================================================================
# Builder
# =====================================================================
class StoryBuilder:
    """Rakit semua scene dari naskah JSON menjadi satu video final."""

    def __init__(
        self,
        settings: Settings,
        caps: Capabilities | None = None,
        *,
        pipeline: MediaPipeline | None = None,
        tts: TTSFn | None = None,
        broll: BrollFn | None = None,
    ) -> None:
        self.settings = settings
        self.caps = caps or detect_capabilities(settings)
        self.pipeline = pipeline or MediaPipeline(settings, self.caps)
        self.workdir = self.pipeline.workdir / "story"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.tts = tts or synthesize
        self.broll = broll or ensure_visual
        self._words_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}

    # -------------------------------------------------------------- lokasi
    @property
    def remotion_dir(self) -> Path | None:
        if not self.settings.remotion_dir:
            return None
        path = Path(self.settings.remotion_dir).expanduser()
        return path if path.is_dir() else None

    @property
    def public_temp(self) -> Path | None:
        remotion = self.remotion_dir
        return (remotion / "public" / "kra_temp") if remotion else None

    def resolve_asset(self, name: str) -> Path | None:
        """Cari aset di workspace/, input_creator/, output_ready/, lalu media/."""
        if not name:
            return None
        candidate = Path(name)
        if candidate.is_absolute():
            return candidate if candidate.exists() else None
        for folder in (
            self.settings.paths.workspace,
            self.settings.paths.input_creator,
            self.settings.paths.output_ready,
            self.settings.paths.media,
        ):
            path = folder / name
            if path.exists():
                return path
        for path in self.settings.paths.workspace.rglob(Path(name).name):
            if path.is_file():
                return path
        return None

    # ------------------------------------------------------------ transkrip
    def words_for(self, source: Path) -> tuple[list[dict[str, Any]], float]:
        """Word-level timestamps untuk satu aset (di-cache per file+mtime)."""
        stat = source.stat()
        key = f"{source.resolve()}:{int(stat.st_mtime)}:{stat.st_size}"
        if key in self._words_cache:
            return self._words_cache[key]
        if not self.caps.whisper:
            self._words_cache[key] = ([], 0.0)
            return self._words_cache[key]
        transcript = self.workdir / f"whisper_{_SAFE_NAME.sub('_', source.stem)[:50]}.json"
        try:
            self.pipeline.transcribe(source, transcript)
            words, duration = extract_whisper_words(transcript)
        except Exception as exc:  # noqa: BLE001 - whisper gagal tidak boleh mematikan render
            print(f"⚠️  Whisper gagal untuk {source.name}: {type(exc).__name__}: {exc}", flush=True)
            self._words_cache[key] = ([], 0.0)
            return self._words_cache[key]
        self._words_cache[key] = (words, duration)
        return words, duration

    # ---------------------------------------------------------------- scene
    def _stage(self, scene: Scene, asset: Path) -> Path | None:
        """Salin aset ke public/kra_temp/ supaya staticFile() aman di Chromium."""
        temp = self.public_temp
        if temp is None:
            return None
        temp.mkdir(parents=True, exist_ok=True)
        staged = temp / safe_staged_name(scene.scene_id, asset.name)
        shutil.copy2(asset, staged)
        return staged

    def _narrate(self, scene: Scene) -> Narration | None:
        """Voiceover TTS untuk mode faceless (tanpa rekaman Creator)."""
        if not scene.narration_text.strip():
            return None
        try:
            return self.tts(
                self.settings,
                scene.narration_text,
                self.workdir,
                scene_id=scene.scene_id,
                ffmpeg=self.caps.ffmpeg,
                voice_id=scene.voice_id or None,
            )
        except TTSError as exc:
            return Narration(provider="", path=Path(), notes=[f"TTS gagal: {str(exc)[:200]}"])

    def _visualize(self, scene: Scene, index: int) -> VisualAsset | None:
        """Buat B-Roll/gambar latar otomatis untuk scene tanpa aset manual."""
        try:
            return self.broll(
                self.settings,
                scene.scene_id,
                scene.visual_cue or scene.narration_text,
                mode=scene.visual_mode,
                index=index,
                outdir=self.settings.paths.input_creator,
                ffmpeg=self.caps.ffmpeg,
            )
        except Exception as exc:  # noqa: BLE001 - visual gagal tidak boleh menghentikan render
            return VisualAsset(notes=[f"visual otomatis gagal: {type(exc).__name__}: {str(exc)[:160]}"])

    @staticmethod
    def _props(
        scene: Scene,
        *,
        video_name: str,
        audio_name: str,
        image_name: str,
        words: list[dict[str, Any]],
        duration: float,
    ) -> dict:
        return {
            "title": scene.narration_text,
            "videoSrc": video_name,
            "audioSrc": audio_name,
            "bgImageSrc": image_name,
            "words": words,
            "highlightWords": scene.highlight_words,
            "durationInSeconds": round((duration + TTS_TAIL_SECONDS) if duration > 0 else 5.0, 3),
            "lowerThirdText": scene.lower_third_text,
            "visualCue": scene.visual_cue,
        }

    def _render_with_remotion(
        self, scene: Scene, props_path: Path, output: Path, notes: list[str]
    ) -> str:
        remotion = self.remotion_dir
        if not (self.caps.npx and remotion):
            notes.append("npx/remotion_dir tidak tersedia")
            return "unavailable"
        cmd = [
            self.caps.npx,
            "--yes",
            "remotion",
            "render",
            scene.composition_id or DEFAULT_COMPOSITION,
            str(output.resolve()),
            f"--props={props_path.resolve()}",
            "--codec=h264",
            "--pixel-format=yuv420p",
            "--log=error",
        ]
        try:
            self.pipeline.run(cmd, cwd=remotion, timeout=REMOTION_TIMEOUT)
        except MediaError as exc:
            notes.append(f"remotion gagal: {str(exc)[:200]}")
            return "failed"
        if not output.exists() or output.stat().st_size == 0:
            notes.append("remotion selesai tanpa menghasilkan file")
            return "failed"
        return "remotion"

    def _render_with_ffmpeg(
        self,
        source: Path | None,
        audio: Path | None,
        words: list[dict[str, Any]],
        output: Path,
        notes: list[str],
    ) -> str:
        """Fallback: 1080p + audio (video/TTS) + burn-in SRT, tanpa animasi Remotion."""
        if source is None and audio is None:
            notes.append("tidak ada video maupun audio untuk fallback FFmpeg")
            return "skipped"

        cmd = [self.caps.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        if source is None:
            cmd += [
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x0f0f14:s={self.pipeline.width}x{self.pipeline.height}:"
                f"r={self.pipeline.fps}",
            ]
        else:
            cmd += ["-i", str(source)]
        if audio is not None:
            cmd += ["-i", str(audio)]

        vf = (
            f"scale={self.pipeline.width}:{self.pipeline.height}:force_original_aspect_ratio=decrease,"
            f"pad={self.pipeline.width}:{self.pipeline.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.pipeline.fps}"
        )
        if self.caps.whisper and words:
            srt = self._write_srt(words, output)
            if srt is not None:
                vf += "," + subtitle_filter(srt)
                notes.append(f"subtitle burn-in dari Whisper ({len(words)} kata)")

        cmd += ["-map", "0:v:0"]
        if audio is not None:
            cmd += ["-map", "1:a:0"]
        elif source is not None:
            cmd += ["-map", "0:a:0?"]
        cmd += [
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(self.settings.render_crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-shortest",
            str(output),
        ]
        try:
            self.pipeline.run(cmd, timeout=1800)
        except MediaError as exc:
            notes.append(f"ffmpeg fallback gagal: {str(exc)[:200]}")
            return "failed"
        if not output.exists() or output.stat().st_size == 0:
            return "failed"
        if not self.caps.whisper or not words:
            notes.append("tanpa subtitle (Whisper/kata tidak tersedia)")
        return "ffmpeg"

    def _write_srt(self, words: list[dict[str, Any]], output: Path) -> Path | None:
        segments = [{"start": word["start"], "end": word["end"], "text": word["word"]} for word in words]
        if not segments:
            return None
        srt = self.workdir / f"{output.stem}.srt"
        srt.write_text(segments_to_srt(segments), encoding="utf-8")
        return srt

    # --------------------------------------------------------------- publik
    def build(
        self,
        script_path: Path,
        output_path: Path,
        *,
        transition: str = "directionalWarp",
        transition_ms: int = 400,
        fallback_source: str = "a_roll_raw.mp4",
    ) -> StoryResult:
        started = time.monotonic()
        script_path = Path(script_path)
        if not script_path.exists():
            raise StoryError(f"naskah '{script_path.name}' tidak ditemukan di workspace")
        try:
            data = json.loads(script_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoryError(f"naskah '{script_path.name}' bukan JSON valid: {exc}") from exc

        validation = validate_story(data)
        if not validation.ok or validation.script is None:
            raise StoryError("skema naskah tidak valid: " + "; ".join(validation.problems))
        script = validation.script

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result = StoryResult(
            ok=False,
            output=output_path,
            engine="remotion" if self.remotion_dir else "ffmpeg-fallback",
            project_id=script.project_id,
            title=script.title,
            fps=script.fps,
            workdir=str(self.workdir),
            notes=list(validation.warnings),
        )
        if not self.caps.ffmpeg:
            raise StoryError("ffmpeg tidak ditemukan; tidak bisa menggabungkan scene")

        fallback = self.resolve_asset(fallback_source)
        can_remotion = bool(self.public_temp and self.caps.npx)
        if not can_remotion:
            result.notes.append(
                "Remotion tidak siap (npx/STUDIO_REMOTION_DIR); semua scene dirakit dengan FFmpeg"
            )

        rendered: list[Path] = []
        for index, scene in enumerate(script.scenes, start=1):
            scene_result = SceneResult(
                scene_id=scene.scene_id, type=scene.type, composition_id=scene.composition_id
            )

            # 1) Tentukan aset visual: A-Roll Creator > aset manual > visual otomatis > fallback.
            source = self.resolve_asset(scene.media) if scene.media else None
            image = self.resolve_asset(scene.bg_image) if scene.bg_image else None
            missing_media = bool(scene.media) and source is None
            missing_image = bool(scene.bg_image) and image is None

            # 1b) Scene dinamis tanpa aset manual → generate B-Roll/gambar dari visual_cue.
            if scene.type != "CREATOR_AROLL" and source is None and image is None:
                visual = self._visualize(scene, index)
                if visual is not None:
                    scene_result.notes.extend(visual.notes)
                    if visual.ok and visual.path is not None:
                        scene_result.visual_tier = visual.tier
                        result.visual_tiers.append(visual.tier)
                        if visual.kind == "video":
                            source = visual.path
                        else:
                            image = visual.path

            if missing_media and source is None:
                scene_result.notes.append(
                    f"aset '{scene.media}' tidak ditemukan; memakai '{fallback_source}' bila ada"
                )
                source = fallback
            if missing_image and image is None:
                scene_result.notes.append(f"gambar latar '{scene.bg_image}' tidak ditemukan")

            uses_creator_video = scene.type == "CREATOR_AROLL" and source is not None
            narration: Narration | None = None
            audio: Path | None = None

            # 2) Sumber audio: A-Roll Creator (audio asli) atau TTS (mode faceless).
            if source is not None:
                scene_result.source = str(source)
            if not uses_creator_video and scene.narration_text.strip():
                narration = self._narrate(scene)
                if narration is not None:
                    scene_result.notes.extend(narration.notes)
                    if narration.path.is_file() and narration.path.stat().st_size > 0:
                        audio = narration.path
                        scene_result.audio = str(audio)
                        scene_result.audio_provider = narration.provider
                        if narration.provider:
                            result.tts_providers.append(narration.provider)
                    else:
                        scene_result.notes.append("narasi TTS tidak terbentuk; scene tanpa suara")

            # 3) Word-level timestamps: dari video Creator atau dari audio TTS.
            words: list[dict[str, Any]] = []
            duration = 0.0
            timing_source = audio or (source if uses_creator_video else None)
            if timing_source is not None:
                words, duration = self.words_for(timing_source)
                scene_result.words = len(words)
            if not duration and source is not None:
                probe = self.pipeline.probe(source)
                duration = float(probe.get("duration_s") or 0.0)
            if not duration and audio is not None:
                probe = self.pipeline.probe(audio)
                duration = float(probe.get("duration_s") or 0.0)
            scene_result.duration_seconds = duration

            # 4) Render scene.
            scene_output = self.workdir / f"scene_{index:02d}_{_SAFE_NAME.sub('_', scene.scene_id)[:40]}.mp4"
            engine = "skipped"
            if can_remotion:
                video_name = image_name = audio_name = ""
                if source is not None:
                    staged = self._stage(scene, source)
                    video_name = staged.name if staged else ""
                if image is not None:
                    staged_image = self._stage(scene, image)
                    image_name = staged_image.name if staged_image else ""
                if audio is not None:
                    staged_audio = self._stage(scene, audio)
                    audio_name = staged_audio.name if staged_audio else ""

                props_path = (
                    self.settings.paths.workspace
                    / f"props_{index:02d}_{_SAFE_NAME.sub('_', scene.scene_id)[:40]}.json"
                )
                props_path.write_text(
                    json.dumps(
                        self._props(
                            scene,
                            video_name=video_name,
                            audio_name=audio_name,
                            image_name=image_name,
                            words=words,
                            duration=duration,
                        ),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                scene_notes: list[str] = []
                engine = self._render_with_remotion(scene, props_path, scene_output, scene_notes)
                scene_result.notes.extend(scene_notes)

            if engine != "remotion":
                engine = self._render_with_ffmpeg(
                    source,
                    audio,
                    words,
                    scene_output,
                    scene_result.notes,
                )

            scene_result.engine = engine
            if scene_output.exists() and scene_output.stat().st_size > 0:
                scene_result.output = str(scene_output)
                rendered.append(scene_output)
            result.scenes.append(scene_result)

        if not rendered:
            raise StoryError(
                "tidak ada scene yang berhasil dirender: "
                + "; ".join(f"{scene.scene_id}({scene.engine})" for scene in result.scenes)
            )

        self._concat(
            rendered,
            output_path,
            transition=transition,
            transition_ms=transition_ms,
            notes=result.notes,
        )
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise StoryError("concat selesai tanpa menghasilkan file")

        result.specs = self.pipeline.probe(output_path)
        engines = {scene.engine for scene in result.scenes if scene.output}
        if engines == {"remotion"}:
            result.engine = "remotion"
        elif engines == {"ffmpeg"}:
            result.engine = "ffmpeg-fallback"
        else:
            result.engine = "mixed"
        result.ok = True
        result.elapsed_seconds = time.monotonic() - started
        self._cleanup_public_temp()
        return result

    # ---------------------------------------------------------------- concat
    def _concat(
        self,
        scenes: list[Path],
        output: Path,
        *,
        transition: str,
        transition_ms: int,
        notes: list[str],
    ) -> None:
        if len(scenes) == 1:
            shutil.copyfile(scenes[0], output)
            notes.append("hanya satu scene; concat dilewati")
            return

        remotion = self.remotion_dir
        if self.caps.npx and remotion and transition:
            base: list[str] = []
            if shutil.which("xvfb-run"):
                base = ["xvfb-run", "-a", "-s", "-ac -screen 0 1920x1080x24"]
            cmd = base + [
                self.caps.npx,
                "--yes",
                "ffmpeg-concat",
                "-t",
                transition,
                "-d",
                str(transition_ms),
                "-o",
                str(output.resolve()),
                *[str(path.resolve()) for path in scenes],
            ]
            try:
                self.pipeline.run(cmd, cwd=remotion, timeout=CONCAT_TIMEOUT)
                if output.exists() and output.stat().st_size > 0:
                    notes.append(f"concat transisi '{transition}' via ffmpeg-concat")
                    return
                notes.append("ffmpeg-concat selesai tanpa output; fallback ffmpeg concat")
            except MediaError as exc:
                notes.append(f"ffmpeg-concat gagal ({str(exc)[:160]}); fallback ffmpeg concat")

        list_path = self.workdir / "concat.txt"
        list_path.write_text("\n".join(_concat_line(path) for path in scenes), encoding="utf-8")
        self.pipeline.run(
            [
                self.caps.ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        notes.append("concat via ffmpeg demuxer (tanpa transisi)")

    def _cleanup_public_temp(self) -> None:
        temp = self.public_temp
        if temp is None:
            return
        for path in temp.glob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)
