"""Konfigurasi runtime studio: folder kerja, LLM, media engine, dan daemon."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_MODEL = "gpt-4o"
DEFAULT_MAX_ROUNDS = 40


def env_flag(name: str, default: bool = False, env: Mapping[str, str] | None = None) -> bool:
    """Baca boolean dari environment: 1/true/yes/on/y dianggap True."""
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_float(source: Mapping[str, str], name: str, default: float) -> float:
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class StudioPaths:
    """Lokasi folder yang dipakai seluruh bot."""

    root: Path
    agents: Path
    workspace: Path
    input_creator: Path
    output_ready: Path

    @classmethod
    def from_root(cls, root: str | Path | None = None) -> StudioPaths:
        base = Path(root or os.environ.get("STUDIO_ROOT") or Path.cwd()).expanduser().resolve()
        return cls(
            root=base,
            agents=base / "agents",
            workspace=base / "workspace",
            input_creator=base / "input_creator",
            output_ready=base / "output_ready",
        )

    @property
    def media(self) -> Path:
        """Folder kerja sementara untuk pipeline media."""
        return self.workspace / "media"

    @property
    def memory_db(self) -> Path:
        return self.workspace / "studio.db"

    @property
    def pipeline_state(self) -> Path:
        """Status pipeline yang dibaca scheduler n100n (`/kra/schedule/2-check-gate`)."""
        return self.workspace / "pipeline_state.json"

    @property
    def processed(self) -> Path:
        """Arsip rekaman yang sudah diproduksi (agar tidak diproses dua kali)."""
        return self.input_creator / "processed"

    def ensure(self) -> StudioPaths:
        for folder in (self.agents, self.workspace, self.input_creator, self.output_ready):
            folder.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True)
class Settings:
    """Hasil resolusi environment + argumen CLI."""

    paths: StudioPaths
    # --- LLM
    model: str = DEFAULT_MODEL
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.4
    max_rounds: int = DEFAULT_MAX_ROUNDS
    # --- perilaku tool
    mock_creator: bool = False
    real_render: bool = False
    creator_timeout: float = 0.0
    async_gate: bool = False
    # --- media engine
    render_fps: int = 60
    render_crf: int = 20
    whisper_model: str = "base"
    remotion_dir: str = ""
    remotion_composition: str = ""
    bgm_path: str = ""
    # --- daemon
    db_path: str = ""
    schedule: str = ""
    schedule_topic: str = ""
    watch_interval: float = 5.0
    upload_settle: float = 4.0
    telegram_token: str = ""
    telegram_chat_id: str = ""
    notify: str = "auto"
    # --- riset tren nyata (Vito)
    youtube_api_key: str = ""
    tavily_api_key: str = ""
    serpapi_api_key: str = ""
    # --- TTS (mode faceless)
    tts_provider: str = "auto"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "pNInz6obpgDQGcFmaJgB"
    edge_tts_voice: str = "id-ID-ArdiNeural"
    tts_rate: str = "+5%"
    # --- pipeline state untuk n100n
    require_creator_aroll: bool = True
    # --- visual otomatis (B-Roll generator)
    broll_provider: str = "auto"
    thumbnails_enabled: bool = True
    image_api_key: str = ""
    # --- YouTube Shorts otomatis
    shorts_enabled: bool = True
    shorts_duration: int = 45
    # --- dashboard & webhook Telegram
    telegram_webhook_secret: str = ""
    chat_log_limit: int = 100
    pexels_api_key: str = ""

    # ------------------------------------------------------------------ setup
    @classmethod
    def from_env(
        cls,
        *,
        root: str | Path | None = None,
        model: str | None = None,
        max_rounds: int | None = None,
        mock_creator: bool | None = None,
        real_render: bool | None = None,
        async_gate: bool | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Settings:
        source = os.environ if env is None else env

        def pick(*names: str) -> str:
            for env_name in names:
                value = source.get(env_name, "").strip()
                if value:
                    return value
            return ""

        resolved_model = model or pick("STUDIO_MODEL", "OPENAI_MODEL") or DEFAULT_MODEL
        rounds = max_rounds if max_rounds is not None else _env_int(source, "STUDIO_MAX_ROUNDS", DEFAULT_MAX_ROUNDS)

        return cls(
            paths=StudioPaths.from_root(root),
            model=resolved_model,
            api_key=pick("STUDIO_API_KEY", "OPENAI_API_KEY"),
            base_url=pick("STUDIO_BASE_URL", "OPENAI_BASE_URL"),
            temperature=_env_float(source, "STUDIO_TEMPERATURE", 0.4),
            max_rounds=max(8, rounds),
            mock_creator=env_flag("STUDIO_MOCK_CREATOR", False, source)
            if mock_creator is None
            else mock_creator,
            real_render=env_flag("STUDIO_REAL_RENDER", False, source) if real_render is None else real_render,
            creator_timeout=max(0.0, _env_float(source, "STUDIO_CREATOR_TIMEOUT", 0.0)),
            async_gate=env_flag("STUDIO_ASYNC_GATE", False, source) if async_gate is None else async_gate,
            render_fps=max(1, _env_int(source, "STUDIO_RENDER_FPS", 60)),
            render_crf=min(51, max(1, _env_int(source, "STUDIO_RENDER_CRF", 20))),
            whisper_model=pick("STUDIO_WHISPER_MODEL") or "base",
            remotion_dir=pick("STUDIO_REMOTION_DIR"),
            remotion_composition=pick("STUDIO_REMOTION_COMPOSITION"),
            bgm_path=pick("STUDIO_BGM"),
            db_path=pick("STUDIO_DB"),
            schedule=pick("STUDIO_SCHEDULE"),
            schedule_topic=pick("STUDIO_SCHEDULE_TOPIC"),
            watch_interval=max(1.0, _env_float(source, "STUDIO_WATCH_INTERVAL", 5.0)),
            upload_settle=max(0.0, _env_float(source, "STUDIO_UPLOAD_SETTLE", 4.0)),
            telegram_token=pick("STUDIO_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=pick("STUDIO_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"),
            notify=(pick("STUDIO_NOTIFY") or "auto").lower(),
            youtube_api_key=pick("STUDIO_YOUTUBE_API_KEY", "YOUTUBE_API_KEY"),
            tavily_api_key=pick("STUDIO_TAVILY_API_KEY", "TAVILY_API_KEY"),
            serpapi_api_key=pick("STUDIO_SERPAPI_API_KEY", "SERPAPI_API_KEY"),
            tts_provider=(pick("STUDIO_TTS_PROVIDER") or "auto").lower(),
            elevenlabs_api_key=pick("STUDIO_ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=pick("STUDIO_ELEVENLABS_VOICE_ID") or "pNInz6obpgDQGcFmaJgB",
            edge_tts_voice=pick("STUDIO_EDGE_TTS_VOICE") or "id-ID-ArdiNeural",
            tts_rate=pick("STUDIO_TTS_RATE") or "+5%",
            require_creator_aroll=env_flag("STUDIO_REQUIRE_AROLL", True, source),
            broll_provider=(pick("STUDIO_BROLL_PROVIDER") or "auto").lower(),
            thumbnails_enabled=env_flag("STUDIO_THUMBNAILS", True, source),
            image_api_key=pick("STUDIO_IMAGE_API_KEY", "OPENAI_IMAGE_API_KEY", "OPENAI_API_KEY"),
            shorts_enabled=env_flag("STUDIO_SHORTS", True, source),
            shorts_duration=min(58, max(30, _env_int(source, "STUDIO_SHORTS_DURATION", 45))),
            telegram_webhook_secret=pick(
                "STUDIO_TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_WEBHOOK_SECRET"
            ),
            chat_log_limit=max(10, _env_int(source, "STUDIO_CHAT_LOG_LIMIT", 100)),
            pexels_api_key=pick("STUDIO_PEXELS_API_KEY", "PEXELS_API_KEY"),
        )

    def with_changes(self, **kwargs) -> Settings:
        """Salin settings dengan beberapa field diganti (dataclass tetap frozen)."""
        return replace(self, **kwargs)

    # ------------------------------------------------------------------ LLM
    def require_api_key(self) -> str:
        if not self.api_key or self.api_key.startswith("MASUKKAN"):
            raise RuntimeError(
                "API key belum diset. Isi STUDIO_API_KEY (atau OPENAI_API_KEY) di file .env, "
                "atau jalankan `python virtual_office_superbots.py --rehearsal` untuk uji alur tanpa LLM."
            )
        return self.api_key

    def llm_config(self) -> dict:
        """llm_config AutoGen 0.2 untuk semua bot (OpenAI atau endpoint kompatibel)."""
        entry: dict = {"model": self.model, "api_key": self.require_api_key()}
        if self.base_url:
            entry["base_url"] = self.base_url
        return {
            "config_list": [entry],
            "temperature": self.temperature,
            "cache_seed": None,  # jangan pakai cache: tiap rapat harus benar-benar jalan
            "timeout": 180,
        }

    def masked_api_key(self) -> str:
        key = self.api_key
        if not key:
            return "(belum diset)"
        if len(key) <= 8:
            return "***"
        return f"{key[:4]}…{key[-4:]}"

    # --------------------------------------------------------------- memory
    @property
    def memory_db_path(self) -> Path:
        return Path(self.db_path).expanduser().resolve() if self.db_path else self.paths.memory_db

    def describe(self) -> str:
        base = self.base_url or "api.openai.com"
        mode = "async" if self.async_gate else "blocking"
        research = [
            name
            for name, key in (
                ("youtube", self.youtube_api_key),
                ("tavily", self.tavily_api_key),
                ("serpapi", self.serpapi_api_key),
            )
            if key
        ]
        tts = "elevenlabs" if self.elevenlabs_api_key else "edge"
        visual = self.broll_provider
        if visual == "auto":
            visual = "dalle" if self.image_api_key else "flux"
        return (
            f"model={self.model} | endpoint={base} | temperature={self.temperature} | "
            f"max_rounds={self.max_rounds} | api_key={self.masked_api_key()} | gate={mode} | "
            f"riset={','.join(research) or 'off'} | tts={tts} | broll={visual} | "
            f"shorts={'on' if self.shorts_enabled else 'off'} | root={self.paths.root}"
        )
