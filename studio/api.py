# Catatan: JANGAN memakai `from __future__ import annotations` di modul ini.
# Model Pydantic didefinisikan di dalam factory (supaya FastAPI tetap opsional),
# dan FastAPI tidak bisa me-resolve anotasi string dari closure.

"""Bridge HTTP untuk ekosistem KRA: `n100n` (scheduler lokal) → studio daemon.

Dua kelompok endpoint:

**Siklus tunggal**

* ``POST /kra/n100n-trigger``    — fase ``IDEATION_SCRIPT`` atau ``PRODUCE_AND_UPLOAD``

**Dual-loop (jadwal n100n)**

* ``POST /kra/schedule/1-start-script``      — 08:00: rapat naskah (Vito/Clara/Damon/Bima)
* ``GET  /kra/schedule/2-check-gate``        — tiap 30 menit: sudah siap render?
* ``POST /kra/schedule/3-produce-and-upload``— render Raka + QC Damon + upload Sora

**Diagnostik**

* ``GET  /health``  • ``GET /status`` • ``GET /jobs/{job_id}``

Jalankan dengan ``python virtual_office_superbots.py --serve --port 8100``.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .daemon import DEFAULT_SCHEDULE_TOPIC, Daemon
from .state import BUSY, READY_RENDER, gate


def create_app(daemon: Daemon, *, autostart: bool = True) -> Any:
    """Bangun aplikasi FastAPI; import fastapi ditunda agar CLI tetap ringan."""
    try:
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover - hanya saat ekstra belum dipasang
        raise RuntimeError(
            "Mode --serve butuh FastAPI. Pasang dengan: "
            'pip install -r requirements-daemon.txt (atau pip install "fastapi>=0.110" uvicorn)'
        ) from exc

    class N100nTriggerPayload(BaseModel):
        phase: str = Field("IDEATION_SCRIPT", description="IDEATION_SCRIPT atau PRODUCE_AND_UPLOAD")
        topic_or_prompt: str = Field("", description="Topik/ide untuk fase ideasi")
        raw_video_filename: str = Field("a_roll_raw.mp4", description="File rekaman di input_creator/")
        remotion_composition: str = Field("", description="Composition Remotion (opsional)")

    class StartScriptPayload(BaseModel):
        topic_hint: str = Field(
            "Cari tren cerita teknologi/AI terbaru hari ini",
            description="Arah topik untuk Vito & Clara",
        )
        require_creator_aroll: bool = Field(
            True,
            description="False = mode faceless (TTS), pipeline langsung READY_FOR_RENDER",
        )

    @asynccontextmanager
    async def lifespan(_: Any):
        if autostart:
            await daemon.start()
        try:
            yield
        finally:
            if autostart:
                await daemon.stop()

    app = FastAPI(
        title="Kantor Rama AI — Studio Bridge",
        version="2.1.0",
        summary="Webhook n100n → daemon AutoGen (Vito, Clara, Bima, Raka, Damon, Sora)",
        lifespan=lifespan,
    )
    app.state.daemon = daemon

    # ------------------------------------------------------------ diagnostik
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "daemon_running": daemon._running,
            "queue": daemon.queue.qsize(),
            "pipeline_status": daemon.state.status(),
            "capabilities": daemon.capabilities.as_dict(),
        }

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return daemon.status()

    @app.get("/jobs/{job_id}")
    async def job_detail(job_id: str) -> dict[str, Any]:
        job = daemon.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job tidak ditemukan")
        return job.as_dict()

    # ------------------------------------------------- dual-loop n100n (1/3)
    @app.post("/kra/schedule/1-start-script", status_code=202)
    async def start_script(payload: StartScriptPayload) -> dict[str, Any]:
        """08:00 — mulai rapat naskah (Vito → Clara → Damon → Bima)."""
        running = daemon.active_job()
        if running is not None:
            raise HTTPException(
                status_code=409,
                detail=f"pipeline masih sibuk dengan {running.id} ({running.phase}/{running.status})",
            )
        topic = payload.topic_hint.strip() or daemon.settings.schedule_topic or DEFAULT_SCHEDULE_TOPIC
        job = daemon.submit_ideation(
            topic, source="n100n", require_aroll=payload.require_creator_aroll
        )
        return {
            "status": "STARTED",
            "phase": "IDEATION_SCRIPT",
            "mode": "creator_aroll" if payload.require_creator_aroll else "faceless_tts",
            "topic": topic,
            "job": job.as_dict(),
            "pipeline_status": daemon.state.status(),
        }

    # ------------------------------------------------- dual-loop n100n (2/3)
    @app.get("/kra/schedule/2-check-gate")
    async def check_gate(expected_file: str = "a_roll_raw.mp4") -> dict[str, Any]:
        """Tiap 30 menit — sudah boleh render atau belum."""
        filename = Path(expected_file).name
        creator_file = daemon.settings.paths.input_creator / filename
        report = gate(daemon.state, creator_file, expected_file=filename)
        running = daemon.active_job()
        report["active_job"] = running.as_dict() if running else None
        report["queue"] = daemon.queue.qsize()
        return report

    # ------------------------------------------------- dual-loop n100n (3/3)
    @app.post("/kra/schedule/3-produce-and-upload", status_code=202)
    async def produce_and_upload(raw_video_filename: str = "a_roll_raw.mp4") -> dict[str, Any]:
        """Render Raka (TTS/Remotion/concat) → QC Damon → upload Sora."""
        filename = Path(raw_video_filename).name
        creator_file = daemon.settings.paths.input_creator / filename
        file_exists = creator_file.exists() and creator_file.stat().st_size > 0
        state = daemon.state.read()
        busy_status = state.get("status")

        running = daemon.active_job("production")
        if running is not None:
            return {"status": "ALREADY_RUNNING", "job": running.as_dict()}

        faceless_ready = busy_status == READY_RENDER
        if not file_exists and not faceless_ready:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{filename}' belum ada di {daemon.settings.paths.input_creator} dan pipeline "
                    f"tidak dalam mode faceless (status: {busy_status})"
                ),
            )
        if busy_status in BUSY:
            raise HTTPException(
                status_code=409, detail=f"pipeline sedang sibuk ({busy_status}); coba lagi nanti"
            )

        job = daemon.submit_production(
            str(creator_file) if file_exists else "",
            source="n100n",
            topic=str((state.get("details") or {}).get("topic") or ""),
        )
        return {
            "status": "PRODUCTION_STARTED",
            "target": "final_render.mp4 -> YouTube",
            "mode": "creator_aroll" if file_exists else "faceless_tts",
            "job": job.as_dict(),
            "pipeline_status": daemon.state.status(),
        }

    # ---------------------------------------------------- memori episodik
    @app.get("/kra/memory/episodes")
    async def memory_episodes(limit: int = 15) -> dict[str, Any]:
        """Katalog episode terakhir (dipakai dashboard/n100n untuk audit topik)."""
        episodes = daemon.memory.recent_episodes(limit)
        return {"count": len(episodes), "episodes": episodes, "fts": daemon.memory.fts}

    @app.post("/kra/memory/sync-views")
    async def sync_views(limit: int = 50) -> dict[str, Any]:
        """Tarik views terbaru dari YouTube untuk evaluasi Vito (cron mingguan n100n)."""
        report = daemon.memory.sync_views(
            daemon.settings.youtube_api_key, limit=max(1, min(limit, 50))
        )
        return {"status": report["status"], **report}

    # ------------------------------------------------------ endpoint tunggal
    @app.post("/kra/n100n-trigger", status_code=202)
    async def n100n_trigger(payload: N100nTriggerPayload) -> dict[str, Any]:
        phase = payload.phase.strip().upper()
        if phase == "PRODUCE_AND_UPLOAD":
            filename = Path(payload.raw_video_filename).name
            source = daemon.settings.paths.input_creator / filename
            if not source.exists() or source.stat().st_size == 0:
                raise HTTPException(
                    status_code=409,
                    detail=f"'{filename}' belum ada (atau 0 byte) di {daemon.settings.paths.input_creator}",
                )
            job = daemon.submit_production(str(source), source="n100n")
        elif phase == "IDEATION_SCRIPT":
            topic = payload.topic_or_prompt.strip() or daemon.settings.schedule_topic or DEFAULT_SCHEDULE_TOPIC
            job = daemon.submit_ideation(topic, source="n100n")
        else:
            raise HTTPException(
                status_code=422,
                detail="phase harus 'IDEATION_SCRIPT' atau 'PRODUCE_AND_UPLOAD'",
            )
        return {"status": "queued", "phase": phase, "job": job.as_dict()}

    return app


def run_server(daemon: Daemon, host: str, port: int) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Mode --serve butuh uvicorn: pip install uvicorn") from exc
    app = create_app(daemon, autostart=True)
    uvicorn.run(app, host=host, port=port, log_level="info")
