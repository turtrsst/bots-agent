from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio.state import (
    BUSY,
    ERROR,
    IDLE,
    PUBLISHED,
    QC_UPLOADING,
    READY_RENDER,
    RENDERING,
    SCRIPTING,
    WAITING_CREATOR,
    PipelineState,
    gate,
)


def test_missing_file_reads_as_idle(tmp_path: Path) -> None:
    state = PipelineState(tmp_path / "workspace" / "pipeline_state.json")
    assert state.status() == IDLE
    assert state.read()["details"] == {}


def test_update_is_atomic_and_keeps_details(tmp_path: Path) -> None:
    path = tmp_path / "pipeline_state.json"
    state = PipelineState(path)
    state.update(SCRIPTING, topic="AI untuk UMKM", job_id="job-1")
    state.update(WAITING_CREATOR, expected_file="a_roll_raw.mp4")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == WAITING_CREATOR
    assert data["previous_status"] == SCRIPTING
    assert data["details"]["topic"] == "AI untuk UMKM"  # detail lama dipertahankan
    assert data["details"]["expected_file"] == "a_roll_raw.mp4"
    assert not path.with_suffix(".json.tmp").exists()


def test_unknown_status_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        PipelineState(tmp_path / "s.json").update("NGAWUR")


def test_corrupt_state_degrades_to_idle(tmp_path: Path) -> None:
    path = tmp_path / "pipeline_state.json"
    path.write_text("{bukan json", encoding="utf-8")
    assert PipelineState(path).status() == IDLE


def _gate(tmp_path: Path, status: str, *, file_bytes: int | None):
    state = PipelineState(tmp_path / "state.json")
    state.update(status)
    creator = tmp_path / "a_roll_raw.mp4"
    if file_bytes is not None:
        creator.write_bytes(b"x" * file_bytes)
    return gate(state, creator)


def test_gate_waits_for_creator_file(tmp_path: Path) -> None:
    report = _gate(tmp_path, WAITING_CREATOR, file_bytes=None)
    assert report["ready_to_produce"] is False
    assert report["creator_file_detected"] is False
    assert report["expected_file"] == "a_roll_raw.mp4"


def test_gate_zero_byte_file_is_not_ready(tmp_path: Path) -> None:
    report = _gate(tmp_path, WAITING_CREATOR, file_bytes=0)
    assert report["creator_file_detected"] is False
    assert report["ready_to_produce"] is False


def test_gate_ready_when_file_arrives(tmp_path: Path) -> None:
    report = _gate(tmp_path, WAITING_CREATOR, file_bytes=2048)
    assert report["ready_to_produce"] is True
    assert "rekaman Creator" in report["reason"]


def test_gate_faceless_ready_without_file(tmp_path: Path) -> None:
    report = _gate(tmp_path, READY_RENDER, file_bytes=None)
    assert report["ready_to_produce"] is True
    assert "faceless" in report["reason"]


@pytest.mark.parametrize("status", BUSY)
def test_gate_blocks_while_busy(tmp_path: Path, status: str) -> None:
    report = _gate(tmp_path, status, file_bytes=4096)
    assert report["ready_to_produce"] is False
    assert "sibuk" in report["reason"]


@pytest.mark.parametrize("status", [PUBLISHED, ERROR])
def test_gate_blocks_after_cycle_finishes(tmp_path: Path, status: str) -> None:
    report = _gate(tmp_path, status, file_bytes=4096)
    assert report["ready_to_produce"] is False
    assert "tunggu jadwal" in report["reason"]


def test_gate_idle_needs_script_first(tmp_path: Path) -> None:
    report = _gate(tmp_path, IDLE, file_bytes=4096)
    assert report["ready_to_produce"] is False
    assert "rapat naskah" in report["reason"]


def test_as_text_summarises() -> None:
    text = PipelineState(Path("/tmp/tidak-dipakai-state.json")).as_text()
    assert text.startswith(IDLE)
    assert QC_UPLOADING not in text
    assert RENDERING not in text
