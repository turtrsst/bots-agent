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


# ------------------------------------------------- memori episodik (SQLite)
def _memory(tmp_path: Path):
    from studio.memory import MemoryStore

    return MemoryStore(tmp_path / "studio.db")


def test_episode_registered_and_readable(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.register_episode(
        "story_kra_001",
        topic_summary="kantor AI yang berjalan tanpa manusia",
        primary_title="Kantor AI Tanpa Manusia",
        keywords="ai agent, otomasi",
        all_titles=["Kantor AI Tanpa Manusia", "Robot Kerja Malam"],
    )

    episode = memory.get_episode("story_kra_001")
    assert episode["status"] == "SCRIPT_APPROVED"
    assert episode["primary_title"] == "Kantor AI Tanpa Manusia"
    assert "otomasi" in episode["keywords"]

    again = memory.register_episode(
        "story_kra_001", topic_summary="versi revisi", primary_title="Judul Revisi"
    )
    assert again == "story_kra_001"
    assert len(memory.recent_episodes()) == 1  # idempoten per project_id
    assert memory.get_episode("story_kra_001")["primary_title"] == "Judul Revisi"


def test_similarity_math() -> None:
    from studio.memory import token_similarity

    assert token_similarity("kantor AI tanpa manusia", "kantor AI tanpa manusia") == 1.0
    assert token_similarity("", "kantor AI") == 0.0
    assert token_similarity("dan yang di ke", "kantor AI") == 0.0  # semua stopword
    assert token_similarity("kantor AI manusia", "kantor pabrik robot") == 0.5  # 1 irisan / 2 token


def test_duplicate_detection_threshold(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.register_episode(
        "story_kra_001",
        topic_summary="kantor AI berjalan tanpa manusia",
        primary_title="Kantor AI Tanpa Manusia",
        keywords="otomasi",
    )

    duplicate = memory.check_topic_similarity("Kantor AI tanpa manusia di malam hari")
    assert duplicate and duplicate[0]["project_id"] == "story_kra_001"
    assert duplicate[0]["similarity"] >= 0.55
    assert duplicate[0]["similarity_percent"].endswith("%")

    unique = memory.check_topic_similarity("Cara merawat tanaman hias di apartemen")
    assert unique == []

    strict = memory.check_topic_similarity("Kantor AI di malam hari", threshold=0.95)
    assert strict == []


def test_similarity_falls_back_without_fts(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.register_episode("p1", topic_summary="kantor AI tanpa manusia", primary_title="Kantor AI")
    memory.fts = False  # simulasi SQLite tanpa FTS5

    matches = memory.check_topic_similarity("Kantor AI tanpa manusia")
    assert matches and matches[0]["project_id"] == "p1"


def test_episodes_text_is_honest_when_empty(tmp_path: Path) -> None:
    text = _memory(tmp_path).episodes_text()
    assert text.startswith("MEMORI KRA KOSONG")


def test_mark_published_creates_and_updates(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.register_episode("p1", topic_summary="topik", primary_title="Judul")
    memory.mark_episode_published("p1", video_id="vid1", url="https://youtu.be/vid1")

    episode = memory.get_episode("p1")
    assert episode["status"] == "PUBLISHED"
    assert episode["youtube_video_id"] == "vid1"

    memory.mark_episode_published("p2", video_id="vid2", url="https://youtu.be/vid2", title="Episode Baru")
    assert memory.get_episode("p2")["primary_title"] == "Episode Baru"
    assert len(memory.recent_episodes()) == 2


def test_sync_views_updates_counts(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.register_episode("p1", topic_summary="topik", primary_title="Judul")
    memory.mark_episode_published("p1", video_id="vid1", url="https://youtu.be/vid1")

    calls: list[str] = []

    def transport(method: str, url: str, payload=None, **_kwargs):
        calls.append(url)
        return {"items": [{"id": "vid1", "statistics": {"viewCount": "14200"}}]}

    report = memory.sync_views("key", transport=transport)

    assert report["status"] == "SYNCED"
    assert report["updated"] == 1
    assert report["views"] == 14200
    assert memory.get_episode("p1")["views_count"] == 14200
    assert "statistics" in calls[0]


def test_sync_views_without_key_or_videos(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    assert memory.sync_views("")["status"] == "EMPTY"  # belum ada video live
    memory.register_episode("p1", topic_summary="topik", primary_title="Judul")
    memory.mark_episode_published("p1", video_id="vid1", url="https://youtu.be/vid1")
    assert memory.sync_views("")["status"] == "NO_KEY"


def test_sync_views_reports_errors(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.register_episode("p1", topic_summary="topik", primary_title="Judul")
    memory.mark_episode_published("p1", video_id="vid1", url="https://youtu.be/vid1")

    def broken(*_args, **_kwargs):
        raise OSError("kuota habis")

    report = memory.sync_views("key", transport=broken)
    assert report["status"].startswith("ERROR")


def test_prompt_context_includes_episodes(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.register_episode("p1", topic_summary="topik", primary_title="Kantor AI Tanpa Manusia")
    memory.mark_episode_published("p1", video_id="v", url="https://youtu.be/v")
    memory.sync_views("key", transport=lambda *a, **k: {"items": [{"id": "v", "statistics": {"viewCount": "900"}}]})

    context = memory.prompt_context()
    assert "Episode terakhir" in context
    assert "Kantor AI Tanpa Manusia" in context
    assert "900 views" in context
    assert memory.stats()["episodes"] == 1
