from __future__ import annotations

import json
from pathlib import Path

from studio.chatlog import GROUP, ChatLog, default_chat_log


def _wallclock() -> str:
    return "12:34:56"


def test_append_and_read(tmp_path: Path) -> None:
    log = ChatLog(tmp_path / "workspace" / "chat_logs.json", clock=_wallclock)
    entry = log.append("Vito_TrendStrategist", GROUP, "Tren AI minggu ini naik.")
    assert entry == {
        "sender": "Vito_TrendStrategist",
        "recipient": GROUP,
        "message": "Tren AI minggu ini naik.",
        "timestamp": "12:34:56",
    }
    data = json.loads((tmp_path / "workspace" / "chat_logs.json").read_text(encoding="utf-8"))
    assert data == [entry]
    assert log.all() == [entry]


def test_limit_keeps_only_last_entries(tmp_path: Path) -> None:
    log = ChatLog(tmp_path / "chat_logs.json", limit=5, clock=_wallclock)
    for index in range(12):
        log.append("Bot", GROUP, f"pesan {index}")
    messages = [item["message"] for item in log.all()]
    assert len(messages) == 5
    assert messages[0] == "pesan 7" and messages[-1] == "pesan 11"


def test_long_message_truncated(tmp_path: Path) -> None:
    log = ChatLog(tmp_path / "chat_logs.json", clock=_wallclock)
    entry = log.append("Sora_Publisher", GROUP, "x" * 5000)
    assert len(entry["message"]) <= 1500
    assert entry["message"].endswith("…")


def test_corrupt_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "chat_logs.json"
    path.write_text("{bukan json", encoding="utf-8")
    log = ChatLog(path, clock=_wallclock)
    assert log.all() == []
    log.append("Vito_TrendStrategist", GROUP, "pulih")
    assert len(log.all()) == 1


def test_log_messages_infers_recipient_and_tool_calls(tmp_path: Path) -> None:
    log = ChatLog(tmp_path / "chat_logs.json", clock=_wallclock)
    messages = [
        {"role": "user", "name": "Creator_Kamu", "content": "Mulai rapat"},
        {"role": "assistant", "name": "Vito_TrendStrategist", "content": "Saya riset dulu."},
        {
            "role": "assistant",
            "name": "Vito_TrendStrategist",
            "content": None,
            "function_call": {"name": "save_content_brief", "arguments": '{"title": "A"}'},
        },
        {"role": "assistant", "name": "Clara_Scriptwriter", "content": "Naskah siap."},
    ]
    assert log.log_messages(messages) == 4
    logs = log.all()
    assert logs[0]["recipient"] == "Vito_TrendStrategist"
    assert logs[1]["recipient"] == "Vito_TrendStrategist"
    assert logs[2]["message"].startswith("🛠️ memanggil save_content_brief")
    assert logs[3]["recipient"] == GROUP

    # tidak menggandakan pesan yang sudah tercatat
    assert log.log_messages(messages, seen=4) == 0
    assert len(log.all()) == 4


def test_default_chat_log_points_to_workspace(tmp_path: Path) -> None:
    log = default_chat_log(tmp_path / "workspace")
    assert isinstance(log, ChatLog)
    assert log.path == tmp_path / "workspace" / "chat_logs.json"


def test_clear(tmp_path: Path) -> None:
    log = ChatLog(tmp_path / "chat_logs.json", clock=_wallclock)
    log.append("Bima", GROUP, "visual siap")
    log.clear()
    assert log.all() == []
