"""Konfigurasi runtime studio (folder kerja, model LLM, saklar simulasi)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gpt-4o"
DEFAULT_MAX_ROUNDS = 40


def env_flag(name: str, default: bool = False, env: Mapping[str, str] | None = None) -> bool:
    """Baca boolean dari environment: 1/true/yes/on/y dianggap True."""
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


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

    def ensure(self) -> StudioPaths:
        for folder in (self.agents, self.workspace, self.input_creator, self.output_ready):
            folder.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True)
class Settings:
    """Hasil resolusi environment + argumen CLI."""

    paths: StudioPaths
    model: str = DEFAULT_MODEL
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.4
    max_rounds: int = DEFAULT_MAX_ROUNDS
    mock_creator: bool = False
    real_render: bool = False
    creator_timeout: float = 0.0

    @classmethod
    def from_env(
        cls,
        *,
        root: str | Path | None = None,
        model: str | None = None,
        max_rounds: int | None = None,
        mock_creator: bool | None = None,
        real_render: bool | None = None,
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
        rounds_raw = max_rounds if max_rounds is not None else source.get("STUDIO_MAX_ROUNDS", "")
        try:
            rounds = int(rounds_raw) if str(rounds_raw).strip() else DEFAULT_MAX_ROUNDS
        except (TypeError, ValueError):
            rounds = DEFAULT_MAX_ROUNDS

        temp_raw = source.get("STUDIO_TEMPERATURE", "")
        try:
            temperature = float(temp_raw) if temp_raw.strip() else 0.4
        except ValueError:
            temperature = 0.4

        timeout_raw = source.get("STUDIO_CREATOR_TIMEOUT", "")
        try:
            creator_timeout = float(timeout_raw) if timeout_raw.strip() else 0.0
        except ValueError:
            creator_timeout = 0.0

        return cls(
            paths=StudioPaths.from_root(root),
            model=resolved_model,
            api_key=pick("STUDIO_API_KEY", "OPENAI_API_KEY"),
            base_url=pick("STUDIO_BASE_URL", "OPENAI_BASE_URL"),
            temperature=temperature,
            max_rounds=max(8, rounds),
            mock_creator=env_flag("STUDIO_MOCK_CREATOR", False, source)
            if mock_creator is None
            else mock_creator,
            real_render=env_flag("STUDIO_REAL_RENDER", False, source) if real_render is None else real_render,
            creator_timeout=max(0.0, creator_timeout),
        )

    def require_api_key(self) -> str:
        if not self.api_key or self.api_key.startswith("MASUKKAN"):
            raise RuntimeError(
                "API key belum diset. Isi STUDIO_API_KEY (atau OPENAI_API_KEY) di file .env, "
                "atau jalankan `python virtual_office_superbots.py --rehearsal` untuk uji alur tanpa LLM."
            )
        return self.api_key

    def llm_config(self) -> dict[str, Any]:
        """llm_config AutoGen 0.2 untuk semua bot (OpenAI atau endpoint kompatibel)."""
        entry: dict[str, Any] = {"model": self.model, "api_key": self.require_api_key()}
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

    def describe(self) -> str:
        base = self.base_url or "api.openai.com"
        return (
            f"model={self.model} | endpoint={base} | temperature={self.temperature} | "
            f"max_rounds={self.max_rounds} | api_key={self.masked_api_key()} | root={self.paths.root}"
        )
