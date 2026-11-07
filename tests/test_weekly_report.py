from __future__ import annotations

from pathlib import Path

from studio.memory import MemoryStore
from studio.weekly_report import (
    build_weekly_report,
    format_weekly_report,
    send_weekly_report,
)


def _memory(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "studio.db")


def _episode(memory: MemoryStore, project_id: str, title: str, views: int, *, live: bool = True) -> None:
    memory.register_episode(project_id, topic_summary=title.lower(), primary_title=title)
    memory.mark_episode_published(
        project_id,
        video_id=f"vid-{project_id}",
        url=f"https://youtu.be/{project_id}" if live else "",
        status="PUBLISHED",
    )
    if views:
        with memory._connect() as conn:
            conn.execute(
                "UPDATE content_history SET views_count=? WHERE project_id=?", (views, project_id)
            )


def test_empty_catalog_reports_honestly(tmp_path: Path) -> None:
    report = build_weekly_report(_memory(tmp_path))
    assert report.empty
    assert report.total_videos == 0
    assert report.as_dict()["status"] == "EMPTY"
    text = format_weekly_report(report)
    assert "Total Video Tayang:</b> 0 video" in text
    assert "Belum ada video yang berstatus PUBLISHED" in text


def test_totals_top_performers_and_links(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _episode(memory, "p_low", "Video Sepi", 1200)
    _episode(memory, "p_top", "Kantor AI Tanpa Manusia", 48210)
    _episode(memory, "p_mid", "Thumbnail yang Bikin Klik", 9400)
    _episode(memory, "p_nol", "Belum Disinkron", 0)
    memory.register_episode("draft", topic_summary="draft", primary_title="Masih Draft")

    report = build_weekly_report(memory, top=3)
    assert report.total_videos == 4  # hanya PUBLISHED
    assert report.total_views == 58810
    assert report.avg_views == 14702
    assert [item["title"] for item in report.top] == [
        "Kantor AI Tanpa Manusia",
        "Thumbnail yang Bikin Klik",
        "Video Sepi",
    ]
    assert report.top[0]["url"].endswith("p_top")

    text = format_weekly_report(report)
    assert "58.810 views" in text
    assert "Top 3" not in text and "TOP 3 KONTEN TERPOPULER" in text
    assert "<a href=\"https://youtu.be/p_top\">" in text
    assert "hasil sinkronisasi terakhir" in text


def test_zero_views_warns_about_sync(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _episode(memory, "p1", "Baru Tayang", 0)
    report = build_weekly_report(memory)
    assert report.total_views == 0
    assert any("0 views" in note for note in report.notes)
    assert "sync-views" in format_weekly_report(report)


def test_send_weekly_report_uses_notifier(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _episode(memory, "p1", "Kantor AI", 500)

    class Notifier:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def send_html(self, text: str, *, silent: bool = False) -> bool:
            self.lines.append(text)
            return True

    notifier = Notifier()
    result = send_weekly_report(memory, notifier)
    assert result["status"] == "WEEKLY_REPORT_SENT" and result["sent"] is True
    assert notifier.lines and "REKAPITULASI VIEWS MINGGUAN" in notifier.lines[0]


def test_send_weekly_report_survives_broken_notifier(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _episode(memory, "p1", "Kantor AI", 500)

    class Broken:
        def send_html(self, text: str, *, silent: bool = False) -> bool:
            raise OSError("telegram mati")

    result = send_weekly_report(memory, Broken())
    assert result["sent"] is False
    assert result["status"] == "WEEKLY_REPORT_TIDAK_TERKIRIM"
    assert "Total Video Tayang" in result["text"]
