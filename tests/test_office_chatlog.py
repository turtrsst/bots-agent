from __future__ import annotations

import time
from pathlib import Path

import pytest

from studio.chatlog import ChatLog
from studio.config import Settings
from studio.office import Office

pytest.importorskip("autogen")


class FakeGroupChat:
    def __init__(self) -> None:
        self.messages: list[dict] = []


class FakeCreator:
    """Meniru `initiate_chat`: mengisi `GroupChat.messages` bertahap."""

    def __init__(self, group: FakeGroupChat, *, fail: bool = False) -> None:
        self.group = group
        self.fail = fail

    def initiate_chat(self, manager, *, message, clear_history):
        assert clear_history is True
        self.group.messages.clear()  # AutoGen: clear_history=True mengosongkan transcript
        self.group.messages.append({"role": "user", "name": "Creator_Kamu", "content": message})
        for index in range(3):
            time.sleep(0.05)
            self.group.messages.append(
                {
                    "role": "assistant",
                    "name": ["Vito_TrendStrategist", "Clara_Scriptwriter", "Damon_RealityChecker"][
                        index
                    ],
                    "content": f"pesan {index + 1}",
                }
            )
        self.group.messages.append(
            {"role": "user", "name": "Damon_RealityChecker", "content": "PIPELINE_COMPLETE"}
        )
        if self.fail:
            raise RuntimeError("llm mati di tengah rapat")


class FakeManager:
    pass


def _office(settings: Settings, group: FakeGroupChat, creator: FakeCreator, log: ChatLog) -> Office:
    return Office(
        settings=settings,
        tools=None,
        agents={},
        creator=creator,
        group_chat=group,
        manager=FakeManager(),
        persona_sources={},
        chat_log=log,
    )


def test_run_writes_chat_log(settings: Settings, studio_root: Path) -> None:
    group = FakeGroupChat()
    log = ChatLog(studio_root / "workspace" / "chat_logs.json", limit=50)
    office = _office(settings, group, FakeCreator(group), log)

    office.run("Kantor AI tanpa manusia")

    logs = log.all()
    assert logs[0]["message"].startswith("[RAPAT DIMULAI] topik: Kantor AI tanpa manusia")
    senders = [item["sender"] for item in logs]
    assert "Vito_TrendStrategist" in senders
    assert "Clara_Scriptwriter" in senders
    # kickoff AutoGen juga terekam, dan penerima pesan pertama = bot berikutnya
    assert senders[1] == "Creator_Kamu"
    assert logs[1]["recipient"] == "Vito_TrendStrategist"


def test_messages_are_not_duplicated(settings: Settings, studio_root: Path) -> None:
    group = FakeGroupChat()
    log = ChatLog(studio_root / "workspace" / "chat_logs.json", limit=100)
    office = _office(settings, group, FakeCreator(group), log)

    office.run("Topik pertama")
    first = len(log.all())
    office.run("Topik kedua")

    logs = log.all()
    assert len(logs) == first + 6  # header + kickoff + 3 bot + 1 penutup
    messages = [item["message"] for item in logs]
    assert messages.count("pesan 1") == 2  # sekali per rapat, bukan dobel
    assert messages[0].startswith("[RAPAT DIMULAI] topik: Topik pertama")
    assert messages[first].startswith("[RAPAT DIMULAI] topik: Topik kedua")


def test_chat_log_survives_crash_mid_meeting(settings: Settings, studio_root: Path) -> None:
    group = FakeGroupChat()
    log = ChatLog(studio_root / "workspace" / "chat_logs.json", limit=50)
    office = _office(settings, group, FakeCreator(group, fail=True), log)

    with pytest.raises(RuntimeError):
        office.run("Topik gagal")

    messages = [item["message"] for item in log.all()]
    assert any("pesan 1" in message for message in messages)
    assert messages[0].startswith("[RAPAT DIMULAI]")


def test_office_without_log_does_not_touch_disk(settings: Settings, studio_root: Path) -> None:
    group = FakeGroupChat()
    office = _office(settings, group, FakeCreator(group), None)
    office.run("Tanpa logger")
    assert not (studio_root / "workspace" / "chat_logs.json").exists()
