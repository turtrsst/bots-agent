"""Memori jangka panjang studio (SQLite, tanpa dependensi tambahan).

Yang disimpan:

* **runs** — setiap rapat: topik, status, brief, path naskah/video, URL YouTube;
* **videos** — performa tiap video (views/CTR) supaya Vito belajar dari data nyata;
* **topics** — daftar topik yang sudah pernah dibahas, untuk menghindari pengulangan;
* **content_history** — katalog episode (memori episodik): topik, judul, keyword, status
  `SCRIPT_APPROVED → PUBLISHED`, `youtube_video_id`, dan `views_count`. Dijaga FTS5
  (bila tersedia) + skor kemiripan token, jadi Vito tidak mengulang cerita lama.

`prompt_context()` merangkum semuanya menjadi beberapa baris yang disuntikkan ke
system prompt Vito/Sora, sehingga rapat berikutnya tidak mulai dari nol.
"""

from __future__ import annotations

import json
import re
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


EPISODE_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT UNIQUE,
    created_at TEXT,
    updated_at TEXT,
    topic_summary TEXT,
    primary_title TEXT,
    all_titles_json TEXT,
    keywords TEXT,
    status TEXT DEFAULT 'SCRIPTED',
    youtube_video_id TEXT,
    youtube_url TEXT,
    views_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_content_history_created ON content_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_history_video ON content_history(youtube_video_id);
"""

# FTS5 opsional: sebagian build SQLite tidak menyertakannya. Kalau gagal, semua
# pencarian jatuh ke pemindaian penuh — hasil sama, hanya lebih lambat.
EPISODE_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS content_history_fts USING fts5(
    topic_summary, primary_title, keywords,
    content='content_history', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS content_history_ai AFTER INSERT ON content_history BEGIN
    INSERT INTO content_history_fts(rowid, topic_summary, primary_title, keywords)
    VALUES (new.id, new.topic_summary, new.primary_title, new.keywords);
END;
CREATE TRIGGER IF NOT EXISTS content_history_au AFTER UPDATE ON content_history BEGIN
    INSERT INTO content_history_fts(content_history_fts, rowid, topic_summary, primary_title, keywords)
    VALUES ('delete', old.id, old.topic_summary, old.primary_title, old.keywords);
    INSERT INTO content_history_fts(rowid, topic_summary, primary_title, keywords)
    VALUES (new.id, new.topic_summary, new.primary_title, new.keywords);
END;
"""

STOPWORDS_ID = {
    "yang", "dan", "di", "ke", "dari", "ini", "itu", "untuk", "dengan", "pada",
    "adalah", "cara", "bagaimana", "kenapa", "mengapa", "apakah", "dalam", "bisa",
    "akan", "atau", "oleh", "tentang", "saat", "ketika", "karena", "para", "sebuah",
    "kita", "kami", "anda", "kamu", "mereka", "juga", "tidak", "sudah", "belum",
}

SIMILARITY_THRESHOLD = 0.55  # ambang DUPLICATE_WARNING (spesifikasi KRA: 55%)


def tokenize(text: str) -> set[str]:
    """Token bermakna (tanpa stopword bahasa Indonesia)."""
    words = re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
    return {word for word in words if len(word) > 2 and word not in STOPWORDS_ID}


def token_similarity(text_a: str, text_b: str) -> float:
    """Kemiripan 0.0–1.0: irisan token ÷ himpunan token terkecil."""
    tokens_a = tokenize(text_a)
    tokens_b = tokenize(text_b)
    if not tokens_a or not tokens_b:
        return 0.0
    return round(len(tokens_a & tokens_b) / max(min(len(tokens_a), len(tokens_b)), 1), 2)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


class MemoryStore:
    """Akses SQLite sederhana; satu koneksi per operasi (aman antar-thread)."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fts = False
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
            conn.executescript(EPISODE_SCHEMA)
            try:
                conn.executescript(EPISODE_FTS_SCHEMA)
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False  # SQLite tanpa FTS5: tetap jalan dengan pemindaian penuh

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

    # ------------------------------------------------------------- episodes
    def register_episode(
        self,
        project_id: str,
        *,
        topic_summary: str,
        primary_title: str,
        keywords: Any = "",
        all_titles: list[str] | None = None,
        status: str = "SCRIPT_APPROVED",
    ) -> str:
        """Kunci satu episode di katalog (idempoten per `project_id`)."""
        project_id = (project_id or "").strip() or f"kra_{datetime.now():%Y%m%d%H%M%S}"
        if isinstance(keywords, list | tuple | set):
            keyword_text = ", ".join(str(item).strip() for item in keywords if str(item).strip())
        else:
            keyword_text = str(keywords or "").strip()
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO content_history
                    (project_id, created_at, updated_at, topic_summary, primary_title,
                     all_titles_json, keywords, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    topic_summary=excluded.topic_summary,
                    primary_title=excluded.primary_title,
                    all_titles_json=excluded.all_titles_json,
                    keywords=excluded.keywords,
                    status=excluded.status
                """,
                (
                    project_id,
                    now,
                    now,
                    (topic_summary or "").strip(),
                    (primary_title or "").strip(),
                    json.dumps(all_titles or [], ensure_ascii=False),
                    keyword_text,
                    status,
                ),
            )
        return project_id

    def mark_episode_published(
        self,
        project_id: str,
        *,
        video_id: str = "",
        url: str = "",
        title: str = "",
        status: str = "PUBLISHED",
    ) -> str:
        """Tandai episode sudah tayang (atau simulasi) beserta link YouTube-nya."""
        project_id = (project_id or "").strip()
        if not project_id:
            project_id = (video_id or "") or f"kra_{datetime.now():%Y%m%d%H%M%S}"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM content_history WHERE project_id=?", (project_id,)
            ).fetchone()
            now = _now()
            if row:
                conn.execute(
                    """
                    UPDATE content_history
                    SET status=?, youtube_video_id=?, youtube_url=?, updated_at=?,
                        primary_title=COALESCE(NULLIF(?, ''), primary_title)
                    WHERE project_id=?
                    """,
                    (status, video_id, url, now, title, project_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO content_history
                        (project_id, created_at, updated_at, topic_summary, primary_title,
                         keywords, status, youtube_video_id, youtube_url)
                    VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)
                    """,
                    (project_id, now, now, title, title, status, video_id, url),
                )
        return project_id

    def get_episode(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM content_history WHERE project_id=?", ((project_id or "").strip(),)
            ).fetchone()
        return dict(row) if row else None

    def recent_episodes(self, limit: int = 15) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM content_history ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [dict(row) for row in rows]

    def _episode_candidates(
        self, conn: sqlite3.Connection, candidate: str
    ) -> list[sqlite3.Row]:
        """Ambil kandidat lewat FTS5 bila ada; kalau gagal/kosong, pindai semua."""
        tokens = sorted(tokenize(candidate))
        if self.fts and tokens:
            query = " OR ".join(f'"{token}"' for token in tokens[:24])
            try:
                rows = conn.execute(
                    """
                    SELECT h.* FROM content_history_fts f
                    JOIN content_history h ON h.id = f.rowid
                    WHERE content_history_fts MATCH ?
                    """,
                    (query,),
                ).fetchall()
                if rows:
                    return rows
            except sqlite3.OperationalError:
                pass
        return conn.execute("SELECT * FROM content_history").fetchall()

    def check_topic_similarity(
        self, candidate: str, *, threshold: float = SIMILARITY_THRESHOLD, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Episode lama yang mirip dengan calon topik/judul (skor tertinggi dulu)."""
        candidate = (candidate or "").strip()
        if not tokenize(candidate):
            return []
        with self._connect() as conn:
            rows = self._episode_candidates(conn, candidate)
        matches: list[dict[str, Any]] = []
        for row in rows:
            past = " ".join(
                filter(
                    None,
                    [
                        row["topic_summary"] or "",
                        row["primary_title"] or "",
                        row["keywords"] or "",
                    ],
                )
            )
            score = token_similarity(candidate, past)
            if score >= threshold:
                matches.append(
                    {
                        "project_id": row["project_id"],
                        "created_at": (row["created_at"] or "")[:10],
                        "past_topic": row["topic_summary"] or "",
                        "past_title": row["primary_title"] or "",
                        "status": row["status"] or "",
                        "outlier_views": int(row["views_count"] or 0),
                        "similarity": score,
                        "similarity_percent": f"{int(round(score * 100))}%",
                    }
                )
        matches.sort(key=lambda item: item["similarity"], reverse=True)
        return matches[: max(1, int(limit))]

    def episodes_text(self, limit: int = 15) -> str:
        """Ringkasan katalog episode untuk tool Vito."""
        episodes = self.recent_episodes(limit)
        if not episodes:
            return (
                "MEMORI KRA KOSONG: belum ada riwayat episode. Semua topik bebas "
                "dieksekusi, tetapi tetap periksa radar tren."
            )
        lines = [f"DAFTAR RIWAYAT KONTEN KRA ({len(episodes)} TERAKHIR):"]
        for episode in episodes:
            views = int(episode.get("views_count") or 0)
            line = (
                f"- [{episode['status']}] {episode['created_at'][:10] if episode['created_at'] else '-'}"
                f" | {episode['project_id']} | {(episode['primary_title'] or '')[:90]}"
            )
            if episode.get("youtube_url"):
                line += f" | {episode['youtube_url']}"
            if views:
                line += f" | {views} views"
            lines.append(line)
        return "\n".join(lines)

    def sync_views(
        self,
        api_key: str,
        *,
        transport: Any = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Tarik `viewCount` terbaru dari YouTube Data API v3 untuk episode live."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT youtube_video_id FROM content_history "
                "WHERE youtube_video_id IS NOT NULL AND youtube_video_id != '' LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            video_ids = [row["youtube_video_id"] for row in rows if row["youtube_video_id"]]
            if not video_ids:
                return {"status": "EMPTY", "videos": 0, "updated": 0, "views": 0}
            if not api_key:
                return {"status": "NO_KEY", "videos": len(video_ids), "updated": 0, "views": 0}

            if transport is None:
                from .research import default_transport

                transport = default_transport
            url = (
                "https://www.googleapis.com/youtube/v3/videos?part=statistics&id="
                + ",".join(video_ids)
                + f"&key={api_key}"
            )
            try:
                payload = transport("GET", url, None)
            except Exception as exc:  # noqa: BLE001 - sinkronisasi gagal bukan bencana
                return {
                    "status": f"ERROR: {type(exc).__name__}",
                    "videos": len(video_ids),
                    "updated": 0,
                    "views": 0,
                }
            updated = 0
            total = 0
            for item in payload.get("items") or []:
                views = int((item.get("statistics") or {}).get("viewCount") or 0)
                cursor = conn.execute(
                    "UPDATE content_history SET views_count=? WHERE youtube_video_id=?",
                    (views, str(item.get("id") or "")),
                )
                updated += cursor.rowcount
                total += views
            return {"status": "SYNCED", "videos": len(video_ids), "updated": updated, "views": total}

    # -------------------------------------------------------------- konteks
    def prompt_context(self, limit: int = 5) -> str:
        """Ringkasan siap-tempel untuk system prompt bot."""
        runs = [run for run in self.recent_runs(limit * 2) if run["status"] in {
            "PIPELINE_COMPLETE", "AWAITING_CREATOR_UPLOAD", "BERHENTI TANPA PIPELINE_COMPLETE"
        }]
        videos = self.recent_videos(limit)
        episodes = self.recent_episodes(limit)
        if not runs and not videos and not episodes:
            return ""

        lines = [
            f"## MEMORI STUDIO ({len(runs)} rapat, {len(videos)} video, "
            f"{len(episodes)} episode tercatat)",
            "",
        ]
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
        if episodes:
            if len(lines) > 2:  # sudah ada daftar rapat di atas
                lines.append("")
            lines.append("Episode terakhir (jangan ulang topik/judul ini):")
            for episode in episodes:
                views = int(episode.get("views_count") or 0)
                suffix = f" → {views} views" if views else ""
                lines.append(
                    f"- [{episode['status']}] {(episode['primary_title'] or '')[:80]}{suffix}"
                )
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
            episodes = conn.execute("SELECT COUNT(*) AS n FROM content_history").fetchone()["n"]
            published = conn.execute(
                "SELECT COUNT(*) AS n FROM content_history WHERE status='PUBLISHED'"
            ).fetchone()["n"]
        return {
            "runs": total_runs,
            "completed": done,
            "awaiting_creator": awaiting,
            "videos": videos,
            "episodes": episodes,
            "published_episodes": published,
            "total_views": views,
            "db": str(self.path),
        }
