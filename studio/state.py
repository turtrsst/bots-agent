"""Status pipeline yang dibaca scheduler n100n (dual-loop).

File: ``workspace/pipeline_state.json``

    IDLE ──► SCRIPTING_IN_PROGRESS ──► WAITING_FOR_CREATOR ──┐
                     │                      (butuh A-Roll)   │
                     └──► READY_FOR_RENDER ◄─────────────────┘
                                  │
                                  ▼
                             RENDERING ──► PUBLISHED_COMPLETE
                                  │
                                  └──► ERROR

Penulisan bersifat **atomik** (tulis file sementara lalu ``os.replace``) supaya
watcher n100n tidak pernah membaca JSON setengah jadi.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

IDLE = "IDLE"
SCRIPTING = "SCRIPTING_IN_PROGRESS"
WAITING_CREATOR = "WAITING_FOR_CREATOR"
READY_RENDER = "READY_FOR_RENDER"
RENDERING = "RENDERING"
QC_UPLOADING = "QC_AND_UPLOADING"
PUBLISHED = "PUBLISHED_COMPLETE"
ERROR = "ERROR"

STATUSES = (IDLE, SCRIPTING, WAITING_CREATOR, READY_RENDER, RENDERING, QC_UPLOADING, PUBLISHED, ERROR)
BUSY = (SCRIPTING, RENDERING, QC_UPLOADING)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class PipelineState:
    """Status satu pipeline (satu mesin). Aman dipakai dari thread berbeda."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- baca
    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"status": IDLE, "details": {}, "updated_at": ""}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": IDLE, "details": {}, "updated_at": "", "warning": "state rusak"}
        if not isinstance(data, dict) or "status" not in data:
            return {"status": IDLE, "details": {}, "updated_at": "", "warning": "state kosong"}
        data.setdefault("details", {})
        return data

    def status(self) -> str:
        return str(self.read().get("status") or IDLE)

    # --------------------------------------------------------------- tulis
    def update(self, status: str, **details: Any) -> dict[str, Any]:
        if status not in STATUSES:
            raise ValueError(f"status tidak dikenal: {status}")
        previous = self.read()
        merged = dict(previous.get("details") or {})
        merged.update({key: value for key, value in details.items() if value is not None})
        data = {
            "status": status,
            "details": merged,
            "updated_at": _now(),
            "previous_status": previous.get("status", IDLE),
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        return data

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    # ------------------------------------------------------------- ringkasan
    def as_text(self) -> str:
        data = self.read()
        details = data.get("details") or {}
        extra = ", ".join(f"{key}={value}" for key, value in list(details.items())[:4])
        return f"{data['status']} ({data.get('updated_at') or '-'}) {extra}".strip()


def gate(
    state: PipelineState,
    creator_file: Path,
    *,
    expected_file: str = "a_roll_raw.mp4",
) -> dict[str, Any]:
    """Jawaban `/kra/schedule/2-check-gate`: boleh render sekarang atau belum."""
    data = state.read()
    status = str(data.get("status") or IDLE)
    file_exists = creator_file.exists() and creator_file.stat().st_size > 0
    details = data.get("details") or {}

    if status == WAITING_CREATOR and file_exists:
        ready, reason = True, "rekaman Creator sudah masuk"
    elif status == READY_RENDER:
        ready, reason = True, "mode faceless: naskah siap tanpa rekaman"
    elif status in BUSY:
        ready, reason = False, f"pipeline sedang sibuk ({status})"
    elif status in (PUBLISHED, ERROR):
        ready, reason = False, f"siklus sebelumnya {status}; tunggu jadwal berikutnya"
    else:
        ready, reason = False, "menunggu rapat naskah atau rekaman Creator"

    return {
        "pipeline_status": status,
        "creator_file_detected": file_exists,
        "ready_to_produce": ready,
        "expected_file": expected_file,
        "reason": reason,
        "details": details,
        "updated_at": data.get("updated_at", ""),
    }
