"""Memori jangka panjang studio (SQLite, tanpa dependensi tambahan).

Yang disimpan:

* **runs** — setiap rapat: topik, status, brief, path naskah/video, URL YouTube;
* **videos** — performa tiap video (views/CTR) supaya Vito belajar dari data nyata;
* **topics** — daftar topik yang sudah pernah dibahas, untuk menghindari pengulangan.

`prompt_context()` merangkum semuanya menjadi beberapa baris yang disuntikkan ke
system prompt Vito/Sora, sehingga rapat berikutnya tidak mulai dari nol.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    topic TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'full',
    brief_json TEXT,
    script_path TEXT,
    video_path TEXT,
    youtube_url TEXT,
    error TEXT,
    meta_json TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    title TEXT,
    url TEXT,
    published_at TEXT,
    views INTEGER NOT NULL DEFAULT 0,
    ctr REAL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS topics (
    topic TEXT PRIMARY KEY,
    run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


class MemoryStore:
    """Akses SQLite sederhana; satu koneksi per operasi (aman antar-thread)."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    # ------------------------------------------------------------- plumbing
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # pembaca (status/webhook) tidak memblokir worker
            conn.executescript(SCHEMA)

    # ----------------------------------------------------------------- runs
    def record_run(
        self,
        run_id: str,
        topic: str,
        *,
        status: str,
        stage: str = "full",
        brief: dict[str, Any] | None = None,
        script_path: str = "",
        video_path: str = "",
        youtube_url: str = "",
        error: str = "",
        meta: dict[str, Any] | None = None,
    ) -> str:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, created_at, updated_at, topic, status, stage, brief_json,
                                  script_path, video_path, youtube_url, error, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    status=excluded.status,
                    stage=excluded.stage,
                    brief_json=COALESCE(excluded.brief_json, runs.brief_json),
                    script_path=COALESCE(NULLIF(excluded.script_path, ''), runs.script_path),
                    video_path=COALESCE(NULLIF(excluded.video_path, ''), runs.video_path),
                    youtube_url=COALESCE(NULLIF(excluded.youtube_url, ''), runs.youtube_url),
                    error=excluded.error,
                    meta_json=COALESCE(excluded.meta_json, runs.meta_json)
                """,
                (
                    run_id,
                    now,
                    now,
                    topic,
                    status,
                    stage,
                    json.dumps(brief, ensure_ascii=False) if brief is not None else None,
                    script_path,
                    video_path,
                    youtube_url,
                    error,
                    json.dumps(meta, ensure_ascii=False) if meta else None,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO topics (topic, run_id, created_at) VALUES (?, ?, ?)",
                (topic.strip(), run_id, now),
            )
        return run_id

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"status", "stage", "script_path", "video_path", "youtube_url", "error", "topic"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE runs SET {assignments}, updated_at=? WHERE id=?",
                (*updates.values(), _now(), run_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (max(1, limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def runs_by_status(self, status: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
        return [dict(row) for row in rows]

    def used_topics(self, limit: int = 40) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT topic FROM topics ORDER BY created_at DESC LIMIT ?", (max(1, limit),)
            ).fetchall()
        return [row["topic"] for row in rows]

    # --------------------------------------------------------------- videos
    def record_video(
        self,
        run_id: str,
        title: str,
        url: str,
        *,
        views: int = 0,
        ctr: float | None = None,
        notes: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO videos (run_id, title, url, published_at, views, ctr, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, title, url, _now(), int(views), ctr, notes),
            )

    def record_performance(self, url: str, *, views: int, ctr: float | None = None, notes: str = "") -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE videos SET views=?, ctr=COALESCE(?, ctr), notes=? WHERE url=?",
                (int(views), ctr, notes, url),
            )
            return cursor.rowcount

    def recent_videos(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM videos ORDER BY published_at DESC LIMIT ?", (max(1, limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- konteks
    def prompt_context(self, limit: int = 5) -> str:
        """Ringkasan siap-tempel untuk system prompt bot."""
        runs = [run for run in self.recent_runs(limit * 2) if run["status"] in {
            "PIPELINE_COMPLETE", "AWAITING_CREATOR_UPLOAD", "BERHENTI TANPA PIPELINE_COMPLETE"
        }]
        videos = self.recent_videos(limit)
        if not runs and not videos:
            return ""

        lines = [f"## MEMORI STUDIO ({len(runs)} rapat terakhir, {len(videos)} video tercatat)", ""]
        for run in runs[:limit]:
            title = ""
            if run.get("brief_json"):
                try:
                    titles = json.loads(run["brief_json"]).get("titles") or []
                    if titles:
                        title = str(titles[0].get("title", ""))[:80]
                except (json.JSONDecodeError, AttributeError):
                    title = ""
            line = f"- {run['created_at'][:10]} | {run['topic'][:90]} | {run['status']}"
            if title:
                line += f" | judul: {title}"
            if run.get("youtube_url"):
                line += f" | {run['youtube_url']}"
            lines.append(line)
        if videos:
            lines.append("")
            lines.append("Performa video (pakai ini untuk memilih sudut pandang, bukan mengarang statistik):")
            for video in videos:
                extra = f"{video['views']} views"
                if video.get("ctr") is not None:
                    extra += f", CTR {video['ctr']}%"
                if video.get("notes"):
                    extra += f" — {video['notes'][:60]}"
                lines.append(f"- {video['title'][:80]} → {extra}")
        lines.extend(
            [
                "",
                "Aturan memori: jangan mengulang topik/judul di atas; kalau topik mirip, "
                "ambil sudut pandang yang benar-benar baru. Jangan mengarang angka performa.",
            ]
        )
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total_runs = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
            done = conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE status='PIPELINE_COMPLETE'"
            ).fetchone()["n"]
            awaiting = conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE status='AWAITING_CREATOR_UPLOAD'"
            ).fetchone()["n"]
            videos = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]
            views = conn.execute("SELECT COALESCE(SUM(views), 0) AS n FROM videos").fetchone()["n"]
        return {
            "runs": total_runs,
            "completed": done,
            "awaiting_creator": awaiting,
            "videos": videos,
            "total_views": views,
            "db": str(self.path),
        }
