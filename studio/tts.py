"""Suara narasi untuk mode faceless: ElevenLabs → Edge-TTS, lalu normalisasi −14 LUFS.

Dipakai `studio/story.py` saat sebuah scene tidak punya rekaman Creator: teks
`narration_text` dari Clara diubah menjadi audio, dinormalisasi dengan FFmpeg, lalu
ditranskripsi Whisper untuk mendapatkan timestamp per kata.

Aturan kejujuran: bila tidak ada engine yang bisa dipakai, modul ini **melempar
`TTSError`**. Tidak ada audio dummy yang dipalsukan sebagai narasi jadi.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .config import Settings

DEFAULT_ELEVEN_MODEL = "eleven_multilingual_v2"
DEFAULT_TIMEOUT = 90.0
# -14 LUFS (standar YouTube) + buang keheningan di awal kalimat
NORMALIZE_FILTER = (
    "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-50dB,"
    "loudnorm=I=-14:TP=-1.5:LRA=11"
)

ElevenTransport = Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, bytes]]
EdgeSynth = Callable[[str, Path, str, str], None]


class TTSError(RuntimeError):
    """Tidak ada engine TTS yang berhasil menghasilkan audio."""


@dataclass
class Narration:
    provider: str  # elevenlabs | edge
    path: Path
    notes: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "file": str(self.path),
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
            "notes": self.notes,
            "attempts": self.attempts,
        }


# =====================================================================
# Engine
# =====================================================================
def _eleven_transport(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - host ElevenLabs
        return response.status, response.read()


def elevenlabs_speak(
    api_key: str,
    text: str,
    target: Path,
    *,
    voice_id: str,
    model_id: str = DEFAULT_ELEVEN_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
    transport: ElevenTransport | None = None,
) -> None:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.45,
            "similarity_boost": 0.80,
            "style": 0.25,
            "use_speaker_boost": True,
        },
    }
    headers = {"Accept": "audio/mpeg", "xi-api-key": api_key}
    status, audio = (transport or _eleven_transport)(url, payload, headers, timeout)
    if status != 200:
        raise TTSError(f"ElevenLabs HTTP {status}")
    if not audio:
        raise TTSError("ElevenLabs mengembalikan audio kosong")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(audio)


def edge_speak(
    text: str,
    target: Path,
    *,
    voice: str,
    rate: str = "+5%",
    synth: EdgeSynth | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)

    async def _run() -> None:
        import edge_tts

        await edge_tts.Communicate(text=text, voice=voice, rate=rate).save(str(target))

    if synth is not None:
        synth(text, target, voice, rate)
    else:
        try:
            import edge_tts  # noqa: F401 - dicek lebih dulu supaya pesannya jelas
        except ImportError as exc:
            raise TTSError("edge-tts belum terpasang (pip install edge-tts)") from exc
        asyncio.run(_run())

    if not target.exists() or target.stat().st_size == 0:
        raise TTSError("Edge-TTS menghasilkan file kosong")


# =====================================================================
# Normalisasi
# =====================================================================
def _default_run(cmd: list[str], timeout: float = 600.0) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise TTSError(f"binary tidak ditemukan: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TTSError(f"{Path(cmd[0]).name} timeout") from exc
    if proc.returncode != 0:
        raise TTSError(f"{Path(cmd[0]).name} exit {proc.returncode}: {(proc.stderr or '')[-300:]}")


def normalize_narration(
    ffmpeg: str,
    raw: Path,
    target: Path,
    *,
    run: Callable[..., None] | None = None,
) -> Path:
    """Potong dead-air + normalisasi ke −14 LUFS, AAC/MP3 192k stereo 48 kHz."""
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(raw),
        "-af",
        NORMALIZE_FILTER,
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        str(target),
    ]
    (run or _default_run)(cmd)
    if not target.exists() or target.stat().st_size == 0:
        raise TTSError("normalisasi audio gagal; file hasil kosong")
    return target


# =====================================================================
# Orkestrasi
# =====================================================================
def resolve_provider(settings: Settings) -> str:
    mode = (settings.tts_provider or "auto").lower()
    if mode in {"off", "none"}:
        return "off"
    if mode == "elevenlabs":
        return "elevenlabs" if settings.elevenlabs_api_key else "edge"
    if mode == "edge":
        return "edge"
    return "elevenlabs" if settings.elevenlabs_api_key else "edge"


def synthesize(
    settings: Settings,
    text: str,
    workdir: Path,
    *,
    scene_id: str,
    ffmpeg: str | None = None,
    voice_id: str | None = None,
    transport: ElevenTransport | None = None,
    edge: EdgeSynth | None = None,
    run: Callable[..., None] | None = None,
) -> Narration:
    """Buat narasi untuk satu scene: coba engine pilihan, fallback ke engine lain."""
    text = (text or "").strip()
    if not text:
        raise TTSError("narration_text kosong")
    provider = resolve_provider(settings)
    if provider == "off":
        raise TTSError("TTS dimatikan (STUDIO_TTS_PROVIDER=off)")

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    raw = workdir / f"tts_raw_{scene_id}.mp3"
    notes: list[str] = []
    attempts: list[str] = []
    order = [provider] + ([other for other in ("elevenlabs", "edge") if other != provider])
    if provider == "elevenlabs" and not settings.elevenlabs_api_key:
        notes.append("ELEVENLABS_API_KEY kosong; memakai Edge-TTS")

    for candidate in order:
        if candidate == "elevenlabs" and not settings.elevenlabs_api_key:
            attempts.append("elevenlabs: dilewati (tanpa API key)")
            continue
        try:
            if candidate == "elevenlabs":
                elevenlabs_speak(
                    settings.elevenlabs_api_key,
                    text,
                    raw,
                    voice_id=voice_id or settings.elevenlabs_voice_id,
                    transport=transport,
                )
            else:
                edge_speak(text, raw, voice=settings.edge_tts_voice, rate=settings.tts_rate, synth=edge)
        except Exception as exc:  # noqa: BLE001 - fallback adalah tujuan modul ini
            attempts.append(f"{candidate}: GAGAL ({type(exc).__name__}: {str(exc)[:120]})")
            continue

        normalized = workdir / f"tts_norm_{scene_id}.mp3"
        if not ffmpeg:
            notes.append("ffmpeg tidak ada; audio TTS tidak dinormalisasi")
            raw.replace(normalized)
        else:
            try:
                normalize_narration(ffmpeg, raw, normalized, run=run)
                raw.unlink(missing_ok=True)
            except TTSError as exc:
                notes.append(f"normalisasi gagal ({str(exc)[:120]}); memakai audio mentah")
                raw.replace(normalized)
        attempts.append(f"{candidate}: ok")
        return Narration(provider=candidate, path=normalized, notes=notes, attempts=attempts)

    raise TTSError("semua engine TTS gagal: " + "; ".join(attempts))
