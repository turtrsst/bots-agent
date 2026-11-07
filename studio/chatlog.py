"""Rekaman percakapan GroupChat AutoGen → `workspace/chat_logs.json`.

Padanan `kra_chat_logger.py` (spec Creator) di dalam paket studio. Berbeda dari
spec, penulisan memakai:

* ``datetime.now()`` (bukan ``os.popen("date")``) — tanpa subprocess tiap pesan;
* tulis atomik ``tmp → os.replace`` + lock, supaya dashboard tidak pernah
  membaca JSON setengah jadi;
* batas jumlah pesan ``limit`` (default 100) agar file tetap ringan.

Format satu baris log (dibaca dashboard `GET /api/logs`)::

    {"sender": "Vito_TrendStrategist", "recipient": "GROUPCHAT",
     "message": "...", "timestamp": "14:07:11"}
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_LIMIT = 100
MAX_MESSAGE_CHARS = 1500
GROUP = "GROUPCHAT"

_TOOL_PREFIX = "🛠️ memanggil"


def _now_clock() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S")


def _trim(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "…"


class ChatLog:
    """Buffer pesan terakhir antar-bot; satu file JSON per studio."""

    def __init__(
        self,
        path: Path,
        *,
        limit: int = DEFAULT_LIMIT,
        clock: Any = None,
    ) -> None:
        self.path = Path(path)
        self.limit = max(1, int(limit))
        self.clock = clock or _now_clock
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ tulis
    def append(self, sender: str, recipient: str, message: str) -> dict[str, str]:
        entry = {
            "sender": str(sender or "KRA"),
            "recipient": str(recipient or GROUP),
            "message": _trim(message),
            "timestamp": self.clock(),
        }
        with self._lock:
            logs = self._read()
            logs.append(entry)
            if len(logs) > self.limit:
                logs = logs[-self.limit :]
            self._write(logs)
        return entry

    def clear(self) -> None:
        with self._lock:
            self._write([])

    # ------------------------------------------------------------------- baca
    def all(self) -> list[dict[str, str]]:
        with self._lock:
            return self._read()

    def tail(self, count: int = 20) -> list[dict[str, str]]:
        return self.all()[-max(1, count) :]

    # --------------------------------------------------------------- AutoGen
    def log_messages(self, messages: list[dict[str, Any]], *, seen: int = 0) -> int:
        """Salin pesan GroupChat mulai indeks ``seen``; kembalikan jumlah baru."""
        fresh = messages[max(0, seen) :]
        for index, message in enumerate(fresh):
            absolute = max(0, seen) + index
            sender, text = _describe(message)
            if not text:
                continue
            recipient = _next_speaker(messages, absolute)
            self.append(sender, recipient, text)
        return len(fresh)

    # ------------------------------------------------------------- internal
    def _read(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _write(self, logs: list[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(logs, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


def _describe(message: dict[str, Any]) -> tuple[str, str]:
    """(sender, teks) dari satu entri ``GroupChat.messages``."""
    sender = str(message.get("name") or message.get("role") or "KRA")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return sender, content
    function_call = message.get("function_call") or {}
    if function_call.get("name"):
        return sender, f"{_TOOL_PREFIX} {function_call['name']}({function_call.get('arguments') or '{}'})"
    if message.get("role") == "tool":
        return sender, _trim(str(content or ""))
    return sender, ""


def _next_speaker(messages: list[dict[str, Any]], index: int) -> str:
    """Penerima pesan: bot berikutnya bila sudah ada, selain itu GROUPCHAT."""
    for follow in messages[index + 1 :]:
        name = follow.get("name")
        if name:
            return str(name)
    return GROUP


def default_chat_log(workspace: Path, *, limit: int = DEFAULT_LIMIT) -> ChatLog:
    return ChatLog(Path(workspace) / "chat_logs.json", limit=limit)
