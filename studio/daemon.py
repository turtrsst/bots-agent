"""Daemon 24/7: antrean job, scheduler, file watcher, dan notifikasi.

Tiga tugas yang dijalankan bersamaan di satu event loop:

1. **worker** — menjalankan satu rapat pada satu waktu (AutoGen + render itu berat);
2. **watcher** — memantau `input_creator/`, dan begitu rekaman Creator muncul,
   otomatis mengantrekan fase produksi (tidak perlu mengetik `DONE`);
3. **scheduler** — memicu fase ideasi sesuai `STUDIO_SCHEDULE` (mis. `07:00`).

Semua langkah memblokir (AutoGen, ffmpeg) dijalankan lewat `asyncio.to_thread`
supaya webhook/status tetap responsif.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

from .config import Settings
from .media import Capabilities, detect_capabilities
from .memory import MemoryStore, new_run_id
from .notify import Notifier, build_notifier, format_job_result
from .office import RunSummary, build_office
from .state import (
    BUSY,
    ERROR,
    PUBLISHED,
    READY_RENDER,
    RENDERING,
    SCRIPTING,
    WAITING_CREATOR,
    PipelineState,
)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi"}
PHASES = ("ideation", "production")
DEFAULT_SCHEDULE_TOPIC = (
    "Riset tren konten hari ini, pilih satu topik yang belum pernah kami bahas, "
    "lalu siapkan brief dan naskahnya."
)


# =====================================================================
# Util jadwal
# =====================================================================
def parse_schedule(raw: str) -> list[dtime]:
    """'07:00, 16:30' → [time(7), time(16,30)] (entri tidak valid dilewati)."""
    times: list[dtime] = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hour_text, minute_text = part.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except (ValueError, AttributeError):
            continue
        if 0 <= hour < 24 and 0 <= minute < 60:
            times.append(dtime(hour=hour, minute=minute))
    return sorted(set(times))


def next_run_at(times: list[dtime], now: datetime) -> datetime | None:
    """Waktu berikutnya (zona waktu sama dengan `now`), selalu di masa depan."""
    if not times:
        return None
    candidates = []
    for moment in times:
        candidate = now.replace(hour=moment.hour, minute=moment.minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    return min(candidates)


# =====================================================================
# Job
# =====================================================================
@dataclass
class Job:
    id: str
    phase: str
    topic: str = ""
    raw_file: str = ""
    source: str = "cli"
    status: str = "queued"  # queued | running | awaiting_creator | done | failed
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    run_id: str = ""
    status_detail: str = ""
    youtube_url: str = ""
    error: str = ""
    require_aroll: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase": self.phase,
            "topic": self.topic,
            "raw_file": self.raw_file,
            "source": self.source,
            "status": self.status,
            "status_detail": self.status_detail,
            "require_aroll": self.require_aroll,
            "run_id": self.run_id,
            "youtube_url": self.youtube_url,
            "error": self.error,
            "created_at": datetime.fromtimestamp(self.created_at).astimezone().isoformat(timespec="seconds"),
            "duration_seconds": round((self.finished_at or time.time()) - (self.started_at or self.created_at), 1),
        }


# =====================================================================
# Daemon
# =====================================================================
class Daemon:
    def __init__(
        self,
        settings: Settings,
        *,
        notifier: Notifier | None = None,
        memory: MemoryStore | None = None,
        office_factory: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        state: PipelineState | None = None,
    ) -> None:
        self.settings = settings
        settings.paths.ensure()
        self.notifier = notifier or build_notifier(settings)
        self.memory = memory or MemoryStore(settings.memory_db_path)
        self.state = state or PipelineState(settings.paths.pipeline_state)
        self.office_factory = office_factory or build_office
        self.clock = clock or (lambda: datetime.now().astimezone())

        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.jobs: dict[str, Job] = {}
        self._handled: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self._stopping: asyncio.Event | None = None
        self._running = False
        self._current: Job | None = None
        self._caps: Capabilities | None = None
        self._telegram_offset = 0

    # ----------------------------------------------------------- kapabilitas
    @property
    def capabilities(self) -> Capabilities:
        if self._caps is None:
            self._caps = detect_capabilities(self.settings)
        return self._caps

    # ---------------------------------------------------------------- submit
    def _new_job(
        self,
        phase: str,
        *,
        topic: str = "",
        raw_file: str = "",
        source: str = "cli",
        require_aroll: bool = True,
    ) -> Job:
        if phase not in PHASES:
            raise ValueError(f"phase '{phase}' tidak dikenal")
        job = Job(
            id=f"job-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}",
            phase=phase,
            topic=topic,
            raw_file=raw_file,
            source=source,
            require_aroll=require_aroll,
        )
        self.jobs[job.id] = job
        if len(self.jobs) > 50:
            for old_id in list(self.jobs)[:-50]:
                self.jobs.pop(old_id, None)
        self.queue.put_nowait(job)
        return job

    def submit_ideation(self, topic: str, *, source: str = "cli", require_aroll: bool = True) -> Job:
        job = self._new_job(
            "ideation", topic=topic.strip(), source=source, require_aroll=require_aroll
        )
        self.state.update(
            SCRIPTING,
            topic=job.topic,
            job_id=job.id,
            require_creator_aroll=require_aroll,
            source=source,
        )
        return job

    def active_job(self, phase: str | None = None) -> Job | None:
        """Job yang sedang berjalan/menunggu — dipakai untuk idempotensi webhook."""
        for job in self.jobs.values():
            if job.status in {"queued", "running"} and (phase is None or job.phase == phase):
                return job
        return None

    def submit_production(
        self, raw_file: str, *, source: str = "watcher", topic: str = "", dedupe: bool = True
    ) -> Job:
        """Antrekan produksi; `dedupe` mencegah n100n memicu render berkali-kali."""
        if dedupe:
            running = self.active_job("production")
            if running is not None:
                return running
        job = self._new_job("production", topic=topic, raw_file=str(raw_file), source=source)
        self.state.update(RENDERING, raw_file=Path(raw_file).name, job_id=job.id, source=source)
        return job

    # ---------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "current_job": self._current.as_dict() if self._current else None,
            "queue": self.queue.qsize(),
            "jobs": [job.as_dict() for job in list(self.jobs.values())[-10:]],
            "memory": self.memory.stats(),
            "capabilities": self.capabilities.as_dict(),
            "pending_files": sorted(path.name for path in self._video_files()),
            "schedule": self.settings.schedule,
            "notifier": type(self.notifier).__name__,
            "pipeline_state": self.state.read(),
        }

    def status_text(self) -> str:
        data = self.status()
        caps = data["capabilities"]
        lines = [
            "📊 STATUS STUDIO",
            f"Daemon: {'jalan' if data['running'] else 'berhenti'} | antrean: {data['queue']}",
            f"Pipeline: {self.state.as_text()}",
            f"Job aktif: {(data['current_job'] or {}).get('id', '-')}",
            f"Memori: {data['memory']['runs']} rapat, {data['memory']['videos']} video, "
            f"{data['memory']['awaiting_creator']} menunggu rekaman",
            "Media: "
            + ", ".join(
                f"{key}={'✓' if value else '✗'}"
                for key, value in caps.items()
                if key != "remotion_dir"
            ),
        ]
        if data["pending_files"]:
            lines.append("File di input_creator: " + ", ".join(data["pending_files"][:5]))
        for job in data["jobs"][-3:]:
            lines.append(f"- {job['id']} [{job['phase']}] {job['status']}")
        return "\n".join(lines)

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self.settings.paths.ensure()
        self._running = True
        self._stopping = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._worker(), name="studio-worker"),
            asyncio.create_task(self._watch_loop(), name="studio-watch"),
        ]
        if parse_schedule(self.settings.schedule):
            self._tasks.append(asyncio.create_task(self._schedule_loop(), name="studio-schedule"))
        if getattr(self.notifier, "supports_inbound", False):
            self._tasks.append(asyncio.create_task(self._telegram_loop(), name="studio-telegram"))
        await asyncio.to_thread(self._resume_pending)

    async def stop(self) -> None:
        """Shutdown kooperatif: set event, bangunkan worker, baru batalkan task sisa."""
        if self._stopping is not None:
            self._stopping.set()
        self.queue.put_nowait(None)  # sentinel: worker keluar dari queue.get()
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=5)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=2)
        self._tasks.clear()
        self._running = False

    async def run_forever(self) -> None:
        await self.start()
        assert self._stopping is not None
        try:
            await self._stopping.wait()
        finally:
            await self.stop()

    # ---------------------------------------------------------------- worker
    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job is None:  # sentinel shutdown dari stop()
                    return
                await self._execute(job)
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception as exc:  # noqa: BLE001 - worker tidak boleh mati
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                self.queue.task_done()

    async def drain(self, limit: int = 20) -> None:
        """Proses semua job yang sudah diantrekan (dipakai test / mode sekali jalan)."""
        for _ in range(limit):
            try:
                job = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if job is None:
                    continue
                await self._execute(job)
            finally:
                self.queue.task_done()

    async def _execute(self, job: Job) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._current = job
        try:
            summary = await asyncio.to_thread(self._run_job, job)
        except Exception as exc:  # noqa: BLE001 - error apa pun harus jadi notifikasi
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = "failed"
            job.finished_at = time.time()
            self.state.update(ERROR, job_id=job.id, phase=job.phase, error=job.error)
            await asyncio.to_thread(
                self.notifier.send, f"❌ Job {job.id} gagal\nTopik: {job.topic or '-'}\n{job.error}"
            )
            return
        finally:
            self._current = None

        job.finished_at = time.time()
        job.status_detail = summary.status
        job.youtube_url = summary.youtube_url
        job.status = (
            "awaiting_creator" if summary.awaiting_creator else ("done" if summary.completed else "failed")
        )

    def _run_job(self, job: Job) -> RunSummary:
        if job.phase == "ideation":
            return self._run_ideation(job)
        return self._run_production(job)

    # -------------------------------------------------------------- fase kerja
    def _run_ideation(self, job: Job) -> RunSummary:
        office = self.office_factory(
            self.settings,
            stage="ideation",
            memory_context=self.memory.prompt_context(),
            require_creator_aroll=job.require_aroll,
        )
        self.state.update(SCRIPTING, job_id=job.id, topic=job.topic, run_id=job.run_id or None)
        summary = office.run(job.topic)

        run_id = new_run_id()
        job.run_id = run_id
        brief = self._read_json(self.settings.paths.workspace / "content_brief.json")
        self.memory.record_run(
            run_id,
            job.topic,
            status=summary.status,
            stage="ideation",
            brief=brief,
            script_path=self._script_path(),
            meta={"rounds": summary.rounds, "source": job.source, "model": self.settings.model},
        )

        if summary.awaiting_creator and job.require_aroll:
            self.state.update(
                WAITING_CREATOR,
                run_id=run_id,
                job_id=job.id,
                expected_file="a_roll_raw.mp4",
                script_file="script_production.json",
            )
            self.notifier.send(self._creator_checklist(summary, job))
        elif job.require_aroll and not summary.completed and not summary.awaiting_creator:
            self.state.update(
                ERROR,
                run_id=run_id,
                job_id=job.id,
                error=f"ideasi berhenti: {summary.status}",
            )
            self.notifier.send(
                format_job_result(
                    title="Rapat ideasi berhenti",
                    status=summary.status,
                    topic=job.topic,
                    note="Cek workspace/run_transcript.md untuk melihat titik macetnya.",
                )
            )
        else:
            # Faceless (tanpa A-Roll) atau naskah selesai → langsung siap render.
            self.state.update(
                READY_RENDER,
                run_id=run_id,
                job_id=job.id,
                script_file="script_production.json",
                faceless=not job.require_aroll,
            )
            self.notifier.send(
                format_job_result(
                    title="Naskah siap dirender"
                    if not job.require_aroll
                    else "Rapat ideasi selesai (siap produksi)",
                    status=summary.status,
                    topic=job.topic,
                    files=summary.files,
                    note="n100n boleh memicu fase produksi (mode faceless)."
                    if not job.require_aroll
                    else "",
                )
            )
        return summary

    def _run_production(self, job: Job) -> RunSummary:
        pending = self.memory.runs_by_status("AWAITING_CREATOR_UPLOAD")
        run_id = pending[0]["id"] if pending else new_run_id()
        topic = pending[0]["topic"] if pending else (job.topic or "Produksi video")
        job.run_id = run_id
        job.topic = job.topic or topic

        raw_name = Path(job.raw_file).name
        self.state.update(RENDERING, job_id=job.id, run_id=run_id, raw_file=raw_name)
        office = self.office_factory(
            self.settings, stage="production", memory_context=self.memory.prompt_context()
        )
        summary = office.run(topic, raw_file=raw_name)

        video = self.settings.paths.output_ready / "final_render.mp4"
        self.memory.record_run(
            run_id,
            topic,
            status=summary.status,
            stage="production",
            script_path=self._script_path(),
            video_path=str(video) if video.exists() else "",
            youtube_url=summary.youtube_url,
            meta={"rounds": summary.rounds, "raw_file": raw_name, "source": job.source},
        )
        if summary.completed and summary.youtube_url and not summary.youtube_url.startswith("SIMULASI"):
            self.memory.record_video(run_id, self._brief_title() or topic, summary.youtube_url)

        if summary.completed:
            self.state.update(
                PUBLISHED,
                run_id=run_id,
                job_id=job.id,
                youtube_url=summary.youtube_url,
                video="output_ready/final_render.mp4",
            )
            self._archive_processed(job.raw_file)
            self.notifier.send(
                format_job_result(
                    title="✅ Video selesai diproduksi",
                    status=summary.status,
                    topic=topic,
                    youtube_url=summary.youtube_url,
                    files=summary.files,
                )
            )
        else:
            self.state.update(
                ERROR,
                run_id=run_id,
                job_id=job.id,
                error=f"produksi berhenti: {summary.status}",
            )
            self.notifier.send(
                format_job_result(
                    title="⚠️ Produksi berhenti sebelum selesai",
                    status=summary.status,
                    topic=topic,
                    note="Cek workspace/run_transcript.md.",
                )
            )
        return summary

    # -------------------------------------------------------------- watcher
    def _archive_processed(self, raw_file: str) -> Path | None:
        """Pindahkan rekaman ke input_creator/processed/ agar tidak diproses ulang."""
        if not raw_file:
            return None
        source = Path(raw_file)
        if not source.is_absolute():
            source = self.settings.paths.input_creator / source
        if not source.exists():
            return None
        archive = self.settings.paths.processed
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / source.name
        if target.exists():
            target = archive / f"{source.stem}_{datetime.now():%Y%m%d-%H%M%S}{source.suffix}"
        shutil.move(str(source), str(target))
        self._handled = {key for key in self._handled if not key.startswith(f"{source.resolve()}:")}
        return target

    def _video_files(self) -> list[Path]:
        folder = self.settings.paths.input_creator
        if not folder.exists():
            return []
        return [
            path
            for path in sorted(folder.iterdir())
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ]

    def check_uploads(self) -> list[Path]:
        """File video baru yang sudah stabil (tidak sedang ditulis)."""
        fresh: list[Path] = []
        now = time.time()
        for path in self._video_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size == 0:
                continue
            key = f"{path.resolve()}:{int(stat.st_mtime)}:{stat.st_size}"
            if key in self._handled:
                continue
            if now - stat.st_mtime < self.settings.upload_settle:
                continue  # masih di-copy; tunggu sampai ukurannya stabil
            self._handled.add(key)
            fresh.append(path)
        return fresh

    async def _watch_loop(self) -> None:
        while not self._stopped():
            for path in await asyncio.to_thread(self.check_uploads):
                job = self.submit_production(str(path), source="watcher")
                await asyncio.to_thread(
                    self.notifier.send,
                    f"📥 Rekaman '{path.name}' terdeteksi di input_creator.\n"
                    f"Job {job.id} masuk antrean render.",
                )
            await self._sleep(self.settings.watch_interval)

    def _resume_pending(self) -> None:
        if self.state.status() in BUSY:
            # Render/ideasi lain masih jalan — jangan menumpuk job (gate atomik).
            return
        pending = self.memory.runs_by_status("AWAITING_CREATOR_UPLOAD")
        if not pending:
            return
        files = self.check_uploads()
        if files:
            job = self.submit_production(str(files[-1]), source="resume", topic=pending[0]["topic"])
            self.notifier.send(
                f"🔄 Ditemukan rekaman untuk rapat yang tertunda.\nJob {job.id} melanjutkan produksi."
            )

    # ------------------------------------------------------------- scheduler
    async def _schedule_loop(self) -> None:
        times = parse_schedule(self.settings.schedule)
        while times and not self._stopped():
            now = self.clock()
            upcoming = next_run_at(times, now)
            if upcoming is None:
                return
            delay = max(1.0, (upcoming - now).total_seconds())
            await self._sleep(min(delay, 3600.0))
            if self.clock() >= upcoming:
                topic = self.settings.schedule_topic or DEFAULT_SCHEDULE_TOPIC
                job = self.submit_ideation(topic, source="schedule")
                await asyncio.to_thread(
                    self.notifier.send,
                    f"⏰ Jadwal {upcoming:%H:%M} terpicu.\nJob {job.id} — topik: {topic[:120]}",
                )
                await self._sleep(60.0)  # hindari submit ganda pada menit yang sama

    # -------------------------------------------------------------- telegram
    async def _telegram_loop(self) -> None:
        poll = getattr(self.notifier, "poll", None)
        if poll is None:  # pragma: no cover - notifier tanpa inbound
            return
        while not self._stopped():
            updates, offset = await asyncio.to_thread(poll, self._telegram_offset)
            self._telegram_offset = offset
            for update in updates:
                await self._handle_command(update)
            await self._sleep(3.0)

    async def _handle_command(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        command, _, argument = text.partition(" ")
        command = command.lstrip("/").split("@")[0].lower()
        argument = argument.strip()

        if command == "status":
            await asyncio.to_thread(self.notifier.send, self.status_text())
        elif command in {"help", "start"}:
            await asyncio.to_thread(
                self.notifier.send,
                "\n".join(
                    [
                        "🤖 Perintah studio",
                        "/status — ringkasan antrean & memori",
                        "/topic <ide> — mulai rapat ideasi baru",
                        "/produce — render file terbaru di input_creator",
                        "/help — bantuan",
                    ]
                ),
            )
        elif command == "topic":
            if not argument:
                await asyncio.to_thread(self.notifier.send, "Kirim: /topic <ide konten>")
                return
            job = self.submit_ideation(argument, source="telegram")
            await asyncio.to_thread(self.notifier.send, f"🧠 Rapat ideasi diantrekan: {job.id}")
        elif command == "produce":
            files = self._video_files()
            if not files:
                await asyncio.to_thread(
                    self.notifier.send,
                    f"Belum ada file video di {self.settings.paths.input_creator}",
                )
                return
            job = self.submit_production(str(files[-1]), source="telegram")
            await asyncio.to_thread(self.notifier.send, f"🎬 Render diantrekan: {job.id}")

    # ---------------------------------------------------------------- helper
    def _stopped(self) -> bool:
        return self._stopping is not None and self._stopping.is_set()

    async def _sleep(self, seconds: float) -> None:
        """Tidur yang bisa dibangunkan seketika oleh stop()."""
        if self._stopping is None:
            await asyncio.sleep(seconds)
            return
        if self._stopping.is_set():
            return
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    def _creator_checklist(self, summary: RunSummary, job: Job) -> str:
        todo_path = self.settings.paths.workspace / "creator_todo_list.md"
        todo = ""
        if todo_path.exists():
            try:
                todo = todo_path.read_text(encoding="utf-8")[:1200]
            except OSError:
                todo = ""
        return "\n".join(
            [
                "🎬 Naskah siap, Bos. Giliran Anda merekam.",
                f"Topik: {job.topic}",
                "",
                todo.strip() or "Lihat workspace/creator_todo_list.md",
                "",
                f"Taruh file rekaman di: {self.settings.paths.input_creator}",
                "Bot memantau folder itu dan akan otomatis mulai render — tidak perlu mengetik apa pun.",
                f"Run ID: {job.run_id}",
            ]
        )

    def _script_path(self) -> str:
        path = self.settings.paths.workspace / "script_production.md"
        return str(path) if path.exists() else ""

    def _brief_title(self) -> str:
        brief = self._read_json(self.settings.paths.workspace / "content_brief.json")
        try:
            return str(brief["titles"][0]["title"])
        except (KeyError, IndexError, TypeError):
            return ""

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
