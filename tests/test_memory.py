from __future__ import annotations

from pathlib import Path

import pytest

from studio.config import Settings
from studio.memory import MemoryStore, new_run_id


@pytest.fixture
def memory(settings: Settings) -> MemoryStore:
    return MemoryStore(settings.memory_db_path)


def test_schema_created_under_workspace(memory: MemoryStore, settings: Settings) -> None:
    assert memory.path == settings.paths.workspace / "studio.db"
    assert memory.path.exists()
    assert memory.stats()["runs"] == 0


def test_record_run_then_recall(memory: MemoryStore) -> None:
    run_id = new_run_id()
    memory.record_run(
        run_id,
        "Cara bikin video AI",
        status="AWAITING_CREATOR_UPLOAD",
        stage="ideation",
        brief={"titles": [{"title": "Judul pemenang"}]},
        script_path="/tmp/script.md",
    )
    row = memory.get_run(run_id)
    assert row is not None
    assert row["status"] == "AWAITING_CREATOR_UPLOAD"
    assert "Judul pemenang" in row["brief_json"]
    assert row["script_path"] == "/tmp/script.md"

    # upsert: status produksi tidak menghapus brief/naskah lama
    memory.record_run(run_id, "Cara bikin video AI", status="PIPELINE_COMPLETE", stage="production",
                      video_path="/tmp/final.mp4", youtube_url="https://youtu.be/abc")
    row = memory.get_run(run_id)
    assert row["status"] == "PIPELINE_COMPLETE"
    assert row["brief_json"]
    assert row["script_path"] == "/tmp/script.md"
    assert row["video_path"] == "/tmp/final.mp4"


def test_runs_by_status_and_topics(memory: MemoryStore) -> None:
    memory.record_run("run-1", "Topik A", status="AWAITING_CREATOR_UPLOAD")
    memory.record_run("run-2", "Topik B", status="PIPELINE_COMPLETE")
    assert [run["id"] for run in memory.runs_by_status("AWAITING_CREATOR_UPLOAD")] == ["run-1"]
    assert set(memory.used_topics()) == {"Topik A", "Topik B"}


def test_update_run_ignores_unknown_fields(memory: MemoryStore) -> None:
    memory.record_run("run-x", "Topik", status="RUNNING")
    memory.update_run("run-x", status="PIPELINE_COMPLETE", tidak_dikenal="abaikan")
    assert memory.get_run("run-x")["status"] == "PIPELINE_COMPLETE"


def test_video_performance_and_summary(memory: MemoryStore) -> None:
    memory.record_run("run-1", "Topik video", status="PIPELINE_COMPLETE",
                      brief={"titles": [{"title": "5 Tools AI"}]})
    memory.record_video("run-1", "5 Tools AI", "https://youtu.be/xyz", views=100)
    assert memory.record_performance("https://youtu.be/xyz", views=2500, ctr=7.5, notes="CTR bagus") == 1

    context = memory.prompt_context()
    assert "MEMORI STUDIO" in context
    assert "5 Tools AI" in context
    assert "2500 views" in context
    assert "CTR 7.5%" in context
    assert "jangan mengulang topik" in context.lower()
    assert memory.stats()["total_views"] == 2500


def test_prompt_context_empty_without_history(memory: MemoryStore) -> None:
    assert memory.prompt_context() == ""


def test_persistent_across_instances(settings: Settings) -> None:
    first = MemoryStore(settings.memory_db_path)
    first.record_run("run-p", "Topik persist", status="PIPELINE_COMPLETE")
    second = MemoryStore(Path(settings.memory_db_path))
    assert second.get_run("run-p")["topic"] == "Topik persist"
