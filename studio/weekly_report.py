"""Rekapitulasi performa views mingguan dari katalog episode SQLite.

Padanan `kra_weekly_report.py` (spec Creator) di dalam paket studio. Membaca
tabel ``content_history`` (yang sama dengan memori episodik §8), lalu menyusun
pesan HTML untuk Telegram:

* jumlah video tayang, akumulasi views, dan rata-rata per video;
* **Top 3** episode berdasarkan ``views_count`` beserta tautan YouTube;
* catatan kejujuran: angka berasal dari sinkronisasi terakhir, bukan real-time.

Tidak memakai `requests` dan tidak memanggil jaringan — angka diambil dari hasil
:meth:`MemoryStore.sync_views` yang sudah tersimpan di SQLite. Endpoint
``POST /kra/schedule/weekly-report`` menjalankan laporan ini.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

TOP_PERFORMERS = 3


@dataclass
class WeeklyReport:
    total_videos: int = 0
    total_views: int = 0
    avg_views: int = 0
    top: list[dict[str, Any]] = field(default_factory=list)
    generated_at: str = ""
    last_sync: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.total_videos == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "EMPTY" if self.empty else "REPORT_READY",
            "total_videos": self.total_videos,
            "total_views": self.total_views,
            "avg_views": self.avg_views,
            "top": self.top,
            "generated_at": self.generated_at,
            "last_sync": self.last_sync,
            "notes": self.notes,
        }


def _thousands(value: int) -> str:
    return f"{int(value):,}".replace(",", ".")


def build_weekly_report(memory: Any, *, top: int = TOP_PERFORMERS) -> WeeklyReport:
    """Susun rekap dari episode berstatus ``PUBLISHED`` (tanpa mengarang angka)."""
    limit = max(1, min(int(top), 10))
    episodes = memory.recent_episodes(200)
    published = [item for item in episodes if item.get("status") == "PUBLISHED"]
    total_videos = len(published)
    total_views = sum(int(item.get("views_count") or 0) for item in published)
    avg_views = int(total_views / total_videos) if total_videos else 0

    ranked = sorted(published, key=lambda item: int(item.get("views_count") or 0), reverse=True)
    top_items = [
        {
            "rank": index,
            "title": item.get("primary_title") or "(tanpa judul)",
            "project_id": item.get("project_id", ""),
            "views": int(item.get("views_count") or 0),
            "url": item.get("youtube_url") or "",
            "published_at": (item.get("updated_at") or item.get("created_at") or "")[:10],
        }
        for index, item in enumerate(ranked[:limit], start=1)
    ]

    notes: list[str] = []
    if total_videos and total_views == 0:
        notes.append(
            "Semua video masih 0 views di database — jalankan sinkronisasi views "
            "(POST /kra/schedule/sync-views) setelah YOUTUBE_API_KEY diisi."
        )
    if total_videos > len(published[-200:]):
        notes.append("Angka dihitung dari 200 episode terakhir di katalog.")

    return WeeklyReport(
        total_videos=total_videos,
        total_views=total_views,
        avg_views=avg_views,
        top=top_items,
        generated_at=time.strftime("%Y-%m-%d %H:%M"),
        last_sync=_last_sync(memory),
        notes=notes,
    )


def _last_sync(memory: Any) -> str:
    """Waktu sinkronisasi views terakhir bila tersedia (best-effort)."""
    getter = getattr(memory, "last_sync", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # noqa: BLE001 - informasi tambahan, bukan syarat laporan
            return ""
    return ""


def format_weekly_report(report: WeeklyReport) -> str:
    """Format HTML Telegram (tag yang didukung Telegram: b/i/code/a/pre)."""
    lines = [
        "📈 <b>[KRA KANTOR VIRTUAL] REKAPITULASI VIEWS MINGGUAN</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🎬 <b>Total Video Tayang:</b> {report.total_videos} video",
        f"👁️ <b>Akumulasi Views:</b> {_thousands(report.total_views)} views",
        f"📊 <b>Rata-rata per Video:</b> {_thousands(report.avg_views)} views",
        "",
        "🏆 <b>TOP 3 KONTEN TERPOPULER:</b>",
    ]
    if report.top:
        for item in report.top:
            title = item["title"]
            views = _thousands(item["views"])
            if item.get("url"):
                lines.append(f'{item["rank"]}. <a href="{item["url"]}"><b>{title}</b></a>')
            else:
                lines.append(f'{item["rank"]}. <b>{title}</b>')
            lines.append(f"   └ 👁️ <code>{views} views</code>")
    else:
        lines.append("<i>Belum ada video yang berstatus PUBLISHED.</i>")

    if report.notes:
        lines.append("")
        for note in report.notes:
            lines.append(f"ℹ️ {note}")

    lines.extend(
        [
            "",
            "🤖 <i>Vito &amp; Clara memakai data ini untuk memilih tema minggu depan. "
            "Angka = hasil sinkronisasi terakhir, bukan real-time.</i>",
        ]
    )
    if report.last_sync:
        lines.append(f"🕒 Sinkronisasi terakhir: <code>{report.last_sync}</code>")
    lines.append(f"📅 Disusun: {report.generated_at}")

    text = "\n".join(lines)
    return text if len(text) <= 3800 else text[:3790] + "…"


def send_weekly_report(memory: Any, notifier: Any, *, top: int = TOP_PERFORMERS) -> dict[str, Any]:
    """Bangun + kirim laporan ke Telegram; kembalikan ringkasan untuk endpoint."""
    report = build_weekly_report(memory, top=top)
    text = format_weekly_report(report)
    sent = False
    sender = getattr(notifier, "send_html", None) or getattr(notifier, "send", None)
    if callable(sender):
        try:
            sent = bool(sender(text))
        except Exception as exc:  # noqa: BLE001 - notifikasi gagal tidak boleh crash
            print(f"⚠️  Gagal kirim rekap mingguan: {type(exc).__name__}: {exc}", flush=True)
            sent = False
    return {
        "status": "WEEKLY_REPORT_SENT" if sent else "WEEKLY_REPORT_TIDAK_TERKIRIM",
        "sent": sent,
        "report": report.as_dict(),
        "text": text,
    }
