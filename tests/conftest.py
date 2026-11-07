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
        broll_provider="off",  # test tidak boleh memanggil API gambar sungguhan
        thumbnails_enabled=False,  # thumbnail diuji terpisah dengan npx palsu
    )


@pytest.fixture
def fake_ffmpeg(tmp_path: Path) -> Path:
    """Binary ffmpeg palsu: tulis byte apa pun ke argumen terakhir."""
    script = tmp_path / "bin" / "ffmpeg"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/sh\nfor a in \"$@\"; do out=\"$a\"; done\n"
        "printf '\\000\\000\\000\\030ftypmp42\\000\\000\\000\\000mp42isom' > \"$out\"\n"
        "printf 'fakevideo' >> \"$out\"\n"
    )
    script.chmod(0o755)
    return script


class RecordingNotifier:
    """Notifier in-memory untuk test daemon."""

    supports_inbound = False

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str, *, silent: bool = False) -> bool:
        self.messages.append(text)
        return True


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def fake_npx(tmp_path: Path) -> Path:
    """Binary npx palsu untuk `remotion render` dan `ffmpeg-concat`."""
    script = tmp_path / "bin" / "npx"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/sh\n"
        "while [ \"$1\" = '--yes' ]; do shift; done\n"
        "out=''\n"
        "if [ \"$1\" = 'remotion' ]; then out=\"$4\"; else\n"
        "  prev=''; for a in \"$@\"; do [ \"$prev\" = '-o' ] && out=\"$a\"; prev=\"$a\"; done\n"
        "fi\n"
        "[ -z \"$out\" ] && exit 2\n"
        "printf '\\000\\000\\000\\030ftypmp42\\000\\000\\000\\000mp42isom' > \"$out\"\n"
        "printf 'rendered' >> \"$out\"\n"
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def remotion_root(tmp_path: Path) -> Path:
    """Folder Remotion tiruan dengan public/ (tempat kra_temp di-stage)."""
    root = tmp_path / "remotion_studio"
    (root / "public" / "kra_temp").mkdir(parents=True)
    return root
