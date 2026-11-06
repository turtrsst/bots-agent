from __future__ import annotations

from collections import deque

import pytest

from studio.config import Settings, StudioPaths
from studio.office import (
    AGENT_SPECS,
    TERMINATION_KEYWORD,
    build_office,
    build_transitions,
    continuation_selector,
    kickoff_message,
)


@pytest.fixture
def office(settings: Settings):
    return build_office(settings)


def test_all_six_bots_plus_creator_are_present(office) -> None:
    names = set(office.group_chat.agent_names)
    assert names == {"Creator_Kamu", *(spec.name for spec in AGENT_SPECS)}
    assert len(AGENT_SPECS) == 6


def test_bots_self_execute_their_tools(office) -> None:
    """Tool call harus dirutekan ke bot pemiliknya sendiri, bukan agen lain."""
    for agent in office.agents.values():
        for tool_name in agent.function_map:
            assert agent.can_execute_function(tool_name)
    assert office.agents["vito"].name in office.group_chat.agent_names


def test_each_bot_has_only_its_own_tools(office) -> None:
    expected = {spec.name: set(spec.tools) for spec in AGENT_SPECS}
    for agent in office.agents.values():
        assert set(agent.function_map) == expected[agent.name]
        assert agent.llm_config is not False


def test_unregistered_tool_cannot_be_executed(office) -> None:
    assert not office.agents["vito"].can_execute_function("upload_to_youtube")
    assert office.agents["sora"].can_execute_function("upload_to_youtube")


def test_personas_are_loaded_from_markdown(office) -> None:
    for key, persona in office.persona_sources.items():
        assert not persona.fallback_used, f"persona fallback dipakai untuk {key}"
        assert persona.source is not None and persona.source.suffix == ".md"


def test_creator_is_the_first_speaker_edge(office) -> None:
    transitions = build_transitions(office.agents, office.creator)
    assert transitions[office.creator] == [office.agents["vito"]]


def test_every_bot_is_reachable_from_creator(office) -> None:
    transitions = build_transitions(office.agents, office.creator)
    seen = {office.creator}
    queue = deque([office.creator])
    while queue:
        current = queue.popleft()
        for nxt in transitions.get(current, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    assert set(office.agents.values()) <= seen


def test_damon_has_veto_loops(office) -> None:
    transitions = build_transitions(office.agents, office.creator)
    damon_targets = transitions[office.agents["damon"]]
    assert office.agents["clara"] in damon_targets  # tolak naskah
    assert office.agents["raka"] in damon_targets   # tolak render
    assert office.agents["sora"] in damon_targets   # lolos QC


def test_group_chat_uses_transitions_and_introductions(office) -> None:
    assert office.group_chat.speaker_transitions_type == "allowed"
    assert office.group_chat.allowed_speaker_transitions_dict
    assert office.group_chat.send_introductions is True
    assert office.group_chat.max_round == office.settings.max_rounds


def test_termination_keyword_is_wired(office) -> None:
    assert TERMINATION_KEYWORD == "PIPELINE_COMPLETE"
    assert office.manager._is_termination_msg({"content": "selesai. pipeline_complete"})
    assert not office.manager._is_termination_msg({"content": "belum selesai"})


def test_kickoff_message_mentions_topic_and_all_steps() -> None:
    message = kickoff_message("Workflow editing 2026")
    assert "Workflow editing 2026" in message
    for name in ("Vito_TrendStrategist", "Clara_Scriptwriter", "Damon_RealityChecker",
                 "Bima_StudioPM", "Raka_Producer", "Sora_YouTubePublisher"):
        assert name in message
    # kata kunci terminasi TIDAK boleh ada di pesan pembuka, kalau tidak rapat
    # langsung ditutup sebelum Vito sempat bicara
    assert TERMINATION_KEYWORD not in message


def test_build_office_requires_api_key(studio_root) -> None:
    settings = Settings(paths=StudioPaths.from_root(studio_root), api_key="", model="gpt-4o")
    with pytest.raises(RuntimeError, match="API key"):
        build_office(settings)


def test_continuation_selector_returns_tool_owner() -> None:
    from types import SimpleNamespace

    owner = object()
    tool_result = SimpleNamespace(messages=[{"role": "tool", "content": "TERSIMPAN"}])
    normal = SimpleNamespace(messages=[{"role": "user", "content": "lanjut"}])
    empty = SimpleNamespace(messages=[])

    assert continuation_selector(owner, tool_result) is owner
    assert continuation_selector(owner, normal) == "auto"
    assert continuation_selector(owner, empty) == "auto"
