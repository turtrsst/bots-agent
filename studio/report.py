"""Penulisan laporan/transkrip hasil run (dipakai mode LLM maupun rehearsal)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .config import StudioPaths

RECEIPT_FILENAME = "youtube_publish_receipt.json"


def collect_files(paths: StudioPaths) -> list[str]:
    found: list[str] = []
    for folder in (paths.workspace, paths.output_ready, paths.input_creator):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.name != "studio_log.jsonl":
                try:
                    found.append(str(path.relative_to(paths.root)))
                except ValueError:
                    found.append(str(path))
    return found


def youtube_url_from_receipt(paths: StudioPaths) -> str:
    receipt = paths.output_ready / RECEIPT_FILENAME
    if not receipt.exists():
        return ""
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    if data.get("url"):
        return str(data["url"])
    if str(data.get("status", "")).startswith("UPLOADED_SIMULATION"):
        return "SIMULASI (belum ada client_secret.json)"
    return ""


def write_run_report(paths: StudioPaths, summary: Any, model: str) -> str:
    """Tulis run_transcript.md + run_messages.json, kembalikan path relatif transkrip."""
    paths.workspace.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Transkrip Virtual Creator Office",
        "",
        f"- Topik: **{summary.topic}**",
        f"- Status: **{summary.status}**",
        f"- Putaran: {summary.rounds}",
        f"- Durasi: {summary.elapsed_seconds:.1f} detik",
        f"- Model: `{model}`",
        f"- YouTube: {summary.youtube_url or '-'}",
        "",
        "## Berkas yang dihasilkan",
        "",
        *[f"- `{item}`" for item in summary.files],
        "",
        "## Percakapan",
        "",
    ]
    for message in summary.messages:
        name = message.get("name") or message.get("role") or "unknown"
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        lines.extend([f"### {name}", "", content, ""])
    transcript = paths.workspace / "run_transcript.md"
    transcript.write_text("\n".join(lines), encoding="utf-8")
    (paths.workspace / "run_messages.json").write_text(
        json.dumps(summary.messages, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    try:
        return str(transcript.relative_to(paths.root))
    except ValueError:
        return str(transcript)
