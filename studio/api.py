"""Bridge HTTP untuk ekosistem KRA: `n100n` (scheduler lokal) → studio daemon.

Endpoint utama:

* ``GET  /health``              — cek hidup + kapabilitas media
* ``GET  /status``              — antrean job, memori, file yang menunggu
* ``POST /kra/n100n-trigger``   — fase ``IDEATION_SCRIPT`` atau ``PRODUCE_AND_UPLOAD``
* ``GET  /jobs/{job_id}``       — detail satu job

Jalankan dengan ``python virtual_office_superbots.py --serve --port 8100``.
"""

# Catatan: JANGAN memakai `from __future__ import annotations` di modul ini.
# Model Pydantic didefinisikan di dalam factory (supaya FastAPI tetap opsional),
# dan FastAPI tidak bisa me-resolve anotasi string dari closure.

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .daemon import DEFAULT_SCHEDULE_TOPIC, Daemon


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
        version="2.0.0",
        summary="Webhook n100n → daemon AutoGen (Vito, Clara, Bima, Raka, Damon, Sora)",
        lifespan=lifespan,
    )
    app.state.daemon = daemon

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "daemon_running": daemon._running,
            "queue": daemon.queue.qsize(),
            "capabilities": daemon.capabilities.as_dict(),
        }

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return daemon.status()

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

    @app.get("/jobs/{job_id}")
    async def job_detail(job_id: str) -> dict[str, Any]:
        job = daemon.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job tidak ditemukan")
        return job.as_dict()

    return app


def run_server(daemon: Daemon, host: str, port: int) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Mode --serve butuh uvicorn: pip install uvicorn") from exc
    app = create_app(daemon, autostart=True)
    uvicorn.run(app, host=host, port=port, log_level="info")
