"""Mesin produksi media nyata: FFmpeg, Auto-Editor, Whisper, dan Remotion.

Ini pengganti "tangan" Raka yang sebelumnya hanya menulis file dummy. Semua langkah
bersifat *best effort* dan **jujur**: setiap langkah dicatat `ok` / `skipped` /
`failed` beserta alasannya, dan hasil akhir tetap divalidasi dengan `ffprobe`.

Tidak ada dependensi Python wajib di sini — `auto-editor` dan `whisper` dideteksi
saat runtime; kalau tidak ada, langkahnya dilewati (bukan diklaim berhasil).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings

DEFAULT_TIMEOUT = 1800
WHISPER_TIMEOUT = 3600
_WHISPER_MODELS: dict[str, Any] = {}


class MediaError(RuntimeError):
    """Kegagalan fatal: output tidak bisa diproduksi."""


# =====================================================================
# Kapabilitas environment
# =====================================================================
@dataclass(frozen=True)
class Capabilities:
    ffmpeg: str | None = None
    ffprobe: str | None = None
    auto_editor: str | None = None
    npx: str | None = None
    whisper: bool = False
    remotion_dir: str = ""

    @property
    def can_render(self) -> bool:
        return bool(self.ffmpeg)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ffmpeg": bool(self.ffmpeg),
            "ffprobe": bool(self.ffprobe),
            "auto_editor": bool(self.auto_editor),
            "whisper": self.whisper,
            "remotion": bool(self.npx and self.remotion_dir),
            "remotion_dir": self.remotion_dir,
        }

    def summary(self) -> str:
        flags = self.as_dict()
        return ", ".join(f"{key}={'✓' if value else '✗'}" for key, value in flags.items() if key != "remotion_dir")


def detect_capabilities(
    settings: Settings,
    *,
    which: Any = None,
    whisper_available: bool | None = None,
) -> Capabilities:
    """Deteksi binary & library media yang benar-benar tersedia di mesin ini."""
    which = which or shutil.which
    if whisper_available is None:
        try:
            whisper_available = importlib.util.find_spec("whisper") is not None
        except (ImportError, ValueError):  # pragma: no cover - environment rusak
            whisper_available = False
    return Capabilities(
        ffmpeg=which("ffmpeg"),
        ffprobe=which("ffprobe"),
        auto_editor=which("auto-editor"),
        npx=which("npx"),
        whisper=bool(whisper_available),
        remotion_dir=settings.remotion_dir,
    )


# =====================================================================
# Struktur laporan
# =====================================================================
@dataclass
class StepResult:
    name: str
    status: str  # ok | skipped | failed
    detail: str = ""
    seconds: float = 0.0
    command: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "seconds": round(self.seconds, 2),
            "command": self.command,
        }


@dataclass
class RenderResult:
    target: Path
    steps: list[StepResult] = field(default_factory=list)
    specs: dict[str, Any] = field(default_factory=dict)
    subtitles: str = ""
    transcript: str = ""
    notes: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    workdir: str = ""

    @property
    def ok(self) -> bool:
        return self.target.exists() and self.target.stat().st_size > 0

    def status_of(self, name: str) -> str:
        for step in self.steps:
            if step.name == name:
                return step.status
        return "absent"

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": str(self.target),
            "engine": "ffmpeg",
            "output_bytes": self.target.stat().st_size if self.target.exists() else 0,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "workdir": self.workdir,
            "subtitles": self.subtitles,
            "transcript": self.transcript,
            "steps": [step.as_dict() for step in self.steps],
            "failed_steps": [step.name for step in self.steps if step.status == "failed"],
            "skipped_steps": [step.name for step in self.steps if step.status == "skipped"],
            "notes": self.notes,
            "specs": self.specs,
        }


# =====================================================================
# Helper ffmpeg/ffprobe
# =====================================================================
def _tail(text: str | None, limit: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _concat_escape(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def srt_timestamp(seconds: float) -> str:
    total_ms = int(round(max(0.0, float(seconds)) * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def segments_to_srt(segments: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        blocks.append(
            f"{index}\n{srt_timestamp(segment.get('start', 0))} --> "
            f"{srt_timestamp(segment.get('end', 0))}\n{text}\n"
        )
    return "\n".join(blocks)


def subtitle_filter(srt_path: Path) -> str:
    path = str(srt_path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    style = (
        "FontName=Montserrat,FontSize=20,PrimaryColour=&H0000FFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,MarginV=35"
    )
    return f"subtitles='{path}':force_style='{style}'"


# =====================================================================
# Pipeline
# =====================================================================
class MediaPipeline:
    """Enam langkah produksi yang sama dengan spesifikasi KRA, tapi tahan gagal."""

    def __init__(
        self,
        settings: Settings,
        caps: Capabilities | None = None,
        *,
        workdir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.caps = caps or detect_capabilities(settings)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.workdir = workdir or (settings.paths.media / stamp)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.width = 1920
        self.height = 1080
        self.fps = settings.render_fps
        self.crf = settings.render_crf

    # ------------------------------------------------------------- pipeline
    def render(
        self,
        source: Path,
        target: Path,
        *,
        hook_title: str = "",
        remotion_composition: str = "",
        bgm: Path | None = None,
    ) -> RenderResult:
        if not self.caps.ffmpeg:
            raise MediaError("ffmpeg tidak ditemukan di PATH.")
        source = Path(source)
        target = Path(target)
        if not source.exists() or source.stat().st_size == 0:
            raise MediaError(f"input '{source.name}' tidak ada atau 0 byte.")
        target.parent.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        result = RenderResult(target=target, workdir=str(self.workdir))
        steps = result.steps
        notes = result.notes

        normalized = self._step_normalize(source, steps, notes)
        cut = self._step_cut_dead_air(normalized, steps, notes)
        srt, transcript = self._step_transcribe(cut, steps, notes)
        result.subtitles = str(srt) if srt else ""
        result.transcript = str(transcript) if transcript else ""

        intro = self._step_remotion(
            composition=remotion_composition,
            hook_title=hook_title,
            transcript=transcript,
            steps=steps,
            notes=notes,
        )
        segments = [segment for segment in (intro, cut) if segment is not None]

        merged = self._step_concat(segments, steps, notes)
        self._step_finalize(merged, target, srt, bgm or self._bgm_from_settings(), steps, notes)

        if not target.exists() or target.stat().st_size == 0:
            raise MediaError("ffmpeg selesai tanpa menghasilkan file output.")

        result.specs = self._probe(target)
        if self.caps.ffprobe and not result.specs.get("has_video", False):
            raise MediaError("output tidak memiliki stream video yang valid.")

        result.elapsed_seconds = time.monotonic() - started
        return result

    # ---------------------------------------------------------------- steps
    def _step_normalize(self, source: Path, steps: list[StepResult], notes: list[str]) -> Path:
        out = self.workdir / "1_normalized.mp4"
        if not self._has_stream(source, "audio"):
            steps.append(StepResult("normalize_audio", "skipped", "input tanpa audio"))
            notes.append("audio tidak dinormalisasi karena input tidak punya stream audio")
            return source
        cmd = [
            self.caps.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-map", "0:v:0?", "-map", "0:a:0",
            "-c:v", "copy",
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            str(out),
        ]
        return self._exec("normalize_audio", cmd, out, steps, notes, fallback=source)

    def _step_cut_dead_air(self, source: Path, steps: list[StepResult], notes: list[str]) -> Path:
        if not self.caps.auto_editor:
            steps.append(StepResult("cut_dead_air", "skipped", "auto-editor tidak terpasang"))
            return source
        out = self.workdir / "2_cut.mp4"
        cmd = [
            self.caps.auto_editor, str(source),
            "--edit", "audio:threshold=0.04",
            "--margin", "0.15sec,0.25sec",
            "-o", str(out),
        ]
        return self._exec("cut_dead_air", cmd, out, steps, notes, fallback=source)

    def _step_transcribe(
        self, source: Path, steps: list[StepResult], notes: list[str]
    ) -> tuple[Path | None, Path | None]:
        if not self.caps.whisper:
            steps.append(StepResult("whisper", "skipped", "paket openai-whisper tidak terpasang"))
            return None, None
        started = time.monotonic()
        try:
            model = self._whisper_model()
            transcription = model.transcribe(str(source), language="id", word_timestamps=True, verbose=False)
            srt_path = self.workdir / "subtitles.srt"
            srt_path.write_text(segments_to_srt(transcription.get("segments", [])), encoding="utf-8")
            json_path = self.workdir / "transcript.json"
            json_path.write_text(
                json.dumps(transcription, ensure_ascii=False, default=str), encoding="utf-8"
            )
            steps.append(
                StepResult("whisper", "ok", f"{len(transcription.get('segments', []))} segmen", time.monotonic() - started)
            )
            return srt_path, json_path
        except Exception as exc:  # noqa: BLE001 - kegagalan whisper tidak boleh mematikan render
            steps.append(
                StepResult("whisper", "failed", f"{type(exc).__name__}: {_tail(str(exc), 200)}", time.monotonic() - started)
            )
            notes.append("subtitle otomatis gagal; render dilanjutkan tanpa burn-in")
            return None, None

    def _step_remotion(
        self,
        *,
        composition: str,
        hook_title: str,
        transcript: Path | None,
        steps: list[StepResult],
        notes: list[str],
    ) -> Path | None:
        composition = composition or self.settings.remotion_composition
        remotion_dir = Path(self.settings.remotion_dir).expanduser() if self.settings.remotion_dir else None
        if not composition:
            steps.append(StepResult("remotion", "skipped", "tidak ada composition diminta"))
            return None
        if not (self.caps.npx and remotion_dir and remotion_dir.is_dir()):
            steps.append(StepResult("remotion", "skipped", "npx/remotion_dir tidak tersedia"))
            notes.append("intro Remotion dilewati: npx atau STUDIO_REMOTION_DIR tidak siap")
            return None

        props_path = self.workdir / "remotion_props.json"
        props_path.write_text(
            json.dumps(
                {"title": hook_title, "transcriptFile": str(transcript) if transcript else ""},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        out = self.workdir / "3_remotion.mp4"
        cmd = [
            self.caps.npx, "--yes", "remotion", "render", composition, str(out),
            f"--props={props_path}", "--codec=h264",
        ]
        return self._exec("remotion", cmd, out, steps, notes, fallback=None, cwd=remotion_dir)

    def _step_concat(self, segments: list[Path], steps: list[StepResult], notes: list[str]) -> Path:
        conformed: list[Path] = []
        for index, segment in enumerate(segments):
            conformed.append(self._conform(segment, self.workdir / f"seg_{index}.mp4", steps, notes))

        if len(conformed) == 1:
            steps.append(StepResult("concat", "skipped", "hanya satu segmen"))
            return conformed[0]

        list_path = self.workdir / "concat.txt"
        list_path.write_text(
            "\n".join(f"file '{_concat_escape(path)}'" for path in conformed), encoding="utf-8"
        )
        out = self.workdir / "merged.mp4"
        cmd = [
            self.caps.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", "-movflags", "+faststart", str(out),
        ]
        return self._exec("concat", cmd, out, steps, notes, fallback=conformed[0])

    def _step_finalize(
        self,
        merged: Path,
        target: Path,
        srt: Path | None,
        bgm: Path | None,
        steps: list[StepResult],
        notes: list[str],
    ) -> None:
        if not srt and not bgm:
            shutil.copyfile(merged, target)
            steps.append(StepResult("finalize", "ok", "salin tanpa re-encode (tanpa subtitle/BGM)"))
            return

        base = [self.caps.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(merged)]
        if bgm:
            base += ["-stream_loop", "-1", "-i", str(bgm)]

        def build(with_subtitles: bool) -> list[str]:
            cmd = list(base)
            video_filter = subtitle_filter(srt) if (with_subtitles and srt) else None
            if bgm:
                chains = []
                vmap = "0:v"
                if video_filter:
                    chains.append(f"[0:v]{video_filter}[v]")
                    vmap = "[v]"
                chains.append("[1:a]volume=-24dB[bgm]")
                chains.append("[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[a]")
                cmd += ["-filter_complex", ";".join(chains), "-map", vmap, "-map", "[a]"]
            elif video_filter:
                cmd += ["-vf", video_filter]
            cmd += [
                "-c:v", "libx264", "-preset", "medium", "-crf", str(self.crf), "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
            ]
            if bgm:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
            else:
                cmd += ["-c:a", "copy"]
            cmd.append(str(target))
            return cmd

        started = time.monotonic()
        try:
            self._run(build(with_subtitles=True), timeout=DEFAULT_TIMEOUT)
        except MediaError as exc:
            if not srt:
                raise
            notes.append(f"burn-in subtitle gagal ({_tail(str(exc), 160)}); file .srt tetap disimpan")
            self._run(build(with_subtitles=False), timeout=DEFAULT_TIMEOUT)
            steps.append(
                StepResult("finalize", "ok", "tanpa burn-in subtitle (file SRT tersedia)", time.monotonic() - started)
            )
            return
        detail = "subtitle burn-in" + (" + BGM" if bgm else "") if srt or bgm else "re-encode"
        steps.append(StepResult("finalize", "ok", detail, time.monotonic() - started))

    # -------------------------------------------------------------- konform
    def _conform(
        self, source: Path, target: Path, steps: list[StepResult], notes: list[str]
    ) -> Path:
        vf = (
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
            f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.fps}"
        )
        has_audio = self._has_stream(source, "audio")
        cmd = [self.caps.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source)]
        if has_audio:
            cmd += ["-map", "0:v:0", "-map", "0:a:0"]
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        cmd += [
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(self.crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
            "-movflags", "+faststart", str(target),
        ]
        return self._exec("conform", cmd, target, steps, notes, fallback=source)

    # -------------------------------------------------------------- helpers
    def _bgm_from_settings(self) -> Path | None:
        if not self.settings.bgm_path:
            return None
        path = Path(self.settings.bgm_path).expanduser()
        return path if path.exists() else None

    def _whisper_model(self):
        try:
            import whisper
        except ImportError as exc:  # pragma: no cover - sudah dicek lewat capabilities
            raise MediaError("openai-whisper tidak terpasang") from exc
        name = self.settings.whisper_model
        if name not in _WHISPER_MODELS:
            _WHISPER_MODELS[name] = whisper.load_model(name)
        return _WHISPER_MODELS[name]

    def _run(self, cmd: list[str], *, cwd: Path | None = None, timeout: int = DEFAULT_TIMEOUT):
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise MediaError(f"binary tidak ditemukan: {cmd[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"{Path(cmd[0]).name} timeout setelah {timeout}s") from exc
        if proc.returncode != 0:
            raise MediaError(f"{Path(cmd[0]).name} exit {proc.returncode}: {_tail(proc.stderr)}")
        return proc

    def _exec(
        self,
        name: str,
        cmd: list[str],
        expected: Path,
        steps: list[StepResult],
        notes: list[str],
        *,
        fallback: Path | None,
        cwd: Path | None = None,
    ) -> Path:
        started = time.monotonic()
        printable = " ".join(Path(part).name if index == 0 else str(part) for index, part in enumerate(cmd))
        try:
            self._run(cmd, cwd=cwd)
            if not expected.exists() or expected.stat().st_size == 0:
                raise MediaError("output tidak terbentuk")
            steps.append(
                StepResult(name, "ok", "", time.monotonic() - started, _tail(printable, 300))
            )
            return expected
        except MediaError as exc:
            steps.append(
                StepResult(name, "failed", _tail(str(exc), 250), time.monotonic() - started, _tail(printable, 300))
            )
            if fallback is None:
                return expected
            notes.append(f"langkah '{name}' gagal; memakai output sebelumnya")
            return fallback

    def _has_stream(self, path: Path, kind: str) -> bool:
        if not self.caps.ffprobe:
            return True  # tidak bisa memastikan; biarkan ffmpeg yang memutuskan
        try:
            proc = self._run(
                [
                    self.caps.ffprobe, "-v", "error", "-print_format", "json",
                    "-show_streams", str(path),
                ],
                timeout=120,
            )
            data = json.loads(proc.stdout or "{}")
        except (MediaError, json.JSONDecodeError):
            return True
        return any(stream.get("codec_type") == kind for stream in data.get("streams", []))

    def _probe(self, path: Path) -> dict[str, Any]:
        if not self.caps.ffprobe:
            return {}
        try:
            proc = self._run(
                [
                    self.caps.ffprobe, "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", str(path),
                ],
                timeout=120,
            )
            data = json.loads(proc.stdout or "{}")
        except (MediaError, json.JSONDecodeError):
            return {}
        video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
        audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
        fmt = data.get("format", {})
        return {
            "has_video": bool(video),
            "duration_s": round(float(fmt.get("duration", 0.0) or 0.0), 2),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": video.get("r_frame_rate"),
            "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name"),
            "size_bytes": int(fmt.get("size", 0) or 0),
        }
