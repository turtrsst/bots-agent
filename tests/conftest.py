from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from studio.config import Settings, StudioPaths  # noqa: E402


@pytest.fixture
def studio_root(tmp_path: Path) -> Path:
    """Folder studio sementara lengkap dengan salinan persona asli."""
    shutil.copytree(REPO / "agents", tmp_path / "agents")
    return tmp_path


@pytest.fixture
def settings(studio_root: Path) -> Settings:
    return Settings(
        paths=StudioPaths.from_root(studio_root),
        model="gpt-4o",
        api_key="sk-test-dummy",
        temperature=0.0,
        max_rounds=30,
        mock_creator=True,
        real_render=False,
    )
