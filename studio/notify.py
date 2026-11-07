"""Notifikasi keluar: Telegram Bot API atau konsol.

Tidak ada dependensi wajib — memakai `urllib` dari stdlib. `transport` bisa
di-inject pada test sehingga tidak perlu jaringan.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Settings

if TYPE_CHECKING:  # pragma: no cover - hanya untuk anotasi
    from .config import StudioPaths

MAX_MESSAGE = 3800  # batas Telegram 4096, sisakan ruang
MAX_CAPTION = 900


class Notifier(Protocol):
    supports_inbound: bool

    def send(self, text: str, *, silent: bool = False) -> bool: ...


def _chunks(text: str, size: int = MAX_MESSAGE) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    while text:
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    return chunks


class NullNotifier:
    supports_inbound = False

    def send(self, text: str, *, silent: bool = False) -> bool:
        return True

    def send_html(self, text: str, *, silent: bool = False) -> bool:
        return True

    def send_document(self, path: Path | str, caption: str = "") -> bool:
        return False


class ConsoleNotifier:
    """Fallback: cetak ke stdout (dipakai saat Telegram belum dikonfigurasi)."""

    supports_inbound = False

    def send(self, text: str, *, silent: bool = False) -> bool:
        print("\n📣 [NOTIFIKASI STUDIO]", flush=True)
        for line in (text or "").strip().splitlines():
            print(f"   {line}", flush=True)
        return True

    def send_html(self, text: str, *, silent: bool = False) -> bool:
        return self.send(_strip_tags(text), silent=silent)

    def send_document(self, path: Path | str, caption: str = "") -> bool:
        print(f"📎 [LAMPIRAN] {Path(path).name} — kanal konsol tidak mengirim file", flush=True)
        return False


def _strip_tags(text: str) -> str:
    """HTML Telegram → teks biasa untuk kanal konsol."""
    import html
    import re

    without_tags = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(without_tags)


def _default_transport(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = urlencode(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL milik Telegram
        return json.loads(response.read().decode("utf-8"))


def _default_document_transport(
    url: str,
    fields: dict[str, str],
    filename: str,
    data: bytes,
    timeout: float,
) -> dict[str, Any]:
    """Multipart/form-data minimal dengan stdlib (tanpa `requests`)."""
    boundary = "----KRA" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
        f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
    ).encode()
    body += data
    body += f"\r\n--{boundary}--\r\n".encode()
    request = Request(
        url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL milik Telegram
        return json.loads(response.read().decode("utf-8"))


class TelegramNotifier:
    """Kirim pesan ke chat Telegram; bisa juga membaca perintah masuk."""

    supports_inbound = True

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout: float = 10.0,
        transport: Callable[[str, dict[str, Any], float], dict[str, Any]] | None = None,
        document_transport: Callable[..., dict[str, Any]] | None = None,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.transport = transport or _default_transport
        self.document_transport = document_transport or _default_document_transport
        self.api_base = api_base.rstrip("/")

    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    def send(self, text: str, *, silent: bool = False) -> bool:
        ok = True
        for chunk in _chunks(text):
            payload = {
                "chat_id": self.chat_id,
                "text": chunk,
                "disable_notification": "true" if silent else "false",
            }
            try:
                result = self.transport(self._url("sendMessage"), payload, self.timeout)
                if not result.get("ok", False):
                    ok = False
            except Exception as exc:  # noqa: BLE001 - notifikasi tidak boleh mematikan daemon
                print(f"⚠️  Gagal kirim Telegram: {type(exc).__name__}: {exc}", flush=True)
                ok = False
        return ok

    def send_html(self, text: str, *, silent: bool = False) -> bool:
        """Kirim pesan dengan `parse_mode=HTML` (dipakai notifikasi naskah & upload)."""
        ok = True
        for chunk in _chunks(text):
            payload = {
                "chat_id": self.chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "disable_notification": "true" if silent else "false",
            }
            try:
                result = self.transport(self._url("sendMessage"), payload, self.timeout)
                if not result.get("ok", False):
                    ok = False
            except Exception as exc:  # noqa: BLE001 - notifikasi tidak boleh mematikan daemon
                print(f"⚠️  Gagal kirim Telegram (HTML): {type(exc).__name__}: {exc}", flush=True)
                ok = False
        return ok

    def send_document(self, path: Path | str, caption: str = "") -> bool:
        """Kirim file (naskah JSON, checklist rekaman) sebagai lampiran."""
        file_path = Path(path)
        if not file_path.is_file():
            print(f"⚠️  Lampiran '{file_path}' tidak ada; tidak dikirim.", flush=True)
            return False
        fields = {
            "chat_id": self.chat_id,
            "caption": _strip_tags(caption)[:MAX_CAPTION],
            "parse_mode": "HTML",
        }
        try:
            result = self.document_transport(
                self._url("sendDocument"),
                fields,
                file_path.name,
                file_path.read_bytes(),
                self.timeout * 3,
            )
            return bool(result.get("ok", False))
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  Gagal kirim lampiran Telegram: {type(exc).__name__}: {exc}", flush=True)
            return False

    def poll(self, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        payload = {"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["message"])}
        try:
            result = self.transport(self._url("getUpdates"), payload, self.timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  Gagal polling Telegram: {type(exc).__name__}: {exc}", flush=True)
            return [], offset
        updates = result.get("result") or []
        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
        return updates, offset


def build_notifier(settings: Settings) -> Notifier:
    """Pilih kanal notifikasi sesuai konfigurasi."""
    mode = settings.notify if settings.notify in {"auto", "console", "telegram", "off"} else "auto"
    if mode == "off":
        return NullNotifier()
    if settings.telegram_token and settings.telegram_chat_id:
        return TelegramNotifier(settings.telegram_token, settings.telegram_chat_id)
    if mode == "telegram":
        print("⚠️  STUDIO_NOTIFY=telegram tetapi token/chat_id belum diisi; memakai konsol.", flush=True)
    return ConsoleNotifier()


def format_job_result(
    *,
    title: str,
    status: str,
    topic: str,
    youtube_url: str = "",
    files: list[str] | None = None,
    note: str = "",
) -> str:
    lines = [f"*{title}*", f"Topik: {topic[:160]}", f"Status: {status}"]
    if youtube_url:
        lines.append(f"YouTube: {youtube_url}")
    if files:
        lines.append("Berkas: " + ", ".join(Path(item).name for item in files[:8]))
    if note:
        lines.append(note)
    return "\n".join(lines)


# =====================================================================
# Notifikasi fase pipeline (dipakai daemon & tool bot)
# =====================================================================
def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def send_html(notifier: Notifier, text: str, *, silent: bool = False) -> bool:
    """Kirim HTML bila kanal mendukung; fallback ke `send()` biasa."""
    sender = getattr(notifier, "send_html", None)
    if callable(sender):
        return bool(sender(text, silent=silent))
    return bool(notifier.send(_strip_tags(text), silent=silent))


def send_document(notifier: Notifier, path: Path | str, caption: str = "") -> bool:
    """Kirim lampiran bila kanal mendukung (Telegram); kanal lain melaporkan `False`."""
    sender = getattr(notifier, "send_document", None)
    if callable(sender):
        return bool(sender(Path(path), caption))
    return False


def format_script_ready(title: str, scene_count: int, *, require_aroll: bool) -> str:
    if require_aroll:
        mode = (
            "🎥 <b>MENUNGGU A-ROLL CREATOR</b>\n"
            "Rekam lalu taruh <code>a_roll_raw.mp4</code> di folder <code>input_creator/</code>."
        )
    else:
        mode = (
            "🤖 <b>FULL AUTO (TTS + REMOTION)</b>\n"
            "Tidak butuh rekaman Anda — Raka merender otomatis pada pemantauan berikutnya."
        )
    return (
        "📝 <b>[KRA OFFICE] NASKAH CERITA SELESAI!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Judul:</b> {title}\n"
        f"🎬 <b>Total Scene:</b> {scene_count} Scene\n"
        "✅ <b>Audit Damon:</b> SCRIPT PASS\n\n"
        f"<b>Status Produksi:</b>\n{mode}"
    )


def format_upload_done(
    title: str,
    *,
    privacy: str,
    video_url: str,
    studio_url: str,
    engine: str,
    simulated: bool = False,
) -> str:
    header = (
        "🚀 <b>[KRA OFFICE] VIDEO BERHASIL DI-UPLOAD KE YOUTUBE!</b>"
        if not simulated
        else "🧪 <b>[KRA OFFICE] UPLOAD SIMULASI (BUKAN LIVE)</b>"
    )
    lines = [
        header,
        "━━━━━━━━━━━━━━━━━━━━",
        f"🏷️ <b>Judul:</b> {title}",
        f"🔒 <b>Status Privasi:</b> <code>{(privacy or 'unlisted').upper()}</code>",
        f"🛠️ <b>Engine:</b> {engine}",
        "✅ <b>QC Damon:</b> VIDEO PASS",
        "",
        "🔗 <b>Tonton:</b>",
        video_url or "(link belum tersedia)",
        f"🎛️ <b>YouTube Studio:</b>\n{studio_url or '(link belum tersedia)'}",
    ]
    if simulated:
        lines.append("\n⚠️ <i>Receipt ini simulasi: client_secret.json belum dipasang.</i>")
    return "\n".join(lines)


def notify_script_ready(
    notifier: Notifier,
    paths: StudioPaths,
    *,
    require_aroll: bool = True,
) -> str:
    """Kirim ringkasan naskah + lampiran JSON ke Creator."""
    script_path = paths.workspace / "script_production.json"
    data = _read_json(script_path)
    brief = _read_json(paths.workspace / "content_brief.json")
    title = str(data.get("title") or "").strip()
    if not title:
        titles = brief.get("titles") or []
        if titles and isinstance(titles[0], dict):
            title = str(titles[0].get("title") or "").strip()
    title = title or "(judul belum tersedia)"
    scenes = data.get("scenes") or []

    ok = send_html(notifier, format_script_ready(title, len(scenes), require_aroll=require_aroll))
    attached = False
    for candidate, caption in (
        (script_path, "📄 File JSON naskah cerita dari Clara_Scriptwriter"),
        (paths.workspace / "creator_todo_list.md", "🎬 Checklist rekaman dari Bima_StudioPM"),
    ):
        if candidate.is_file() and send_document(notifier, candidate, caption):
            attached = True
    if not ok:
        return "NOTIFIKASI TELEGRAM NASKAH GAGAL (cek token/chat id)"
    return "NOTIFIKASI TELEGRAM NASKAH TERKIRIM" + (" + LAMPIRAN" if attached else "")


def notify_youtube_uploaded(notifier: Notifier, paths: StudioPaths) -> str:
    """Kirim laporan upload + link YouTube/Studio ke Creator."""
    receipt = _read_json(paths.output_ready / "youtube_publish_receipt.json")
    script = _read_json(paths.workspace / "script_production.json")
    story = _read_json(paths.output_ready / "final_render.render.json")

    title = str(receipt.get("title") or script.get("title") or "Video KRA terbaru")
    privacy = str(receipt.get("privacy") or "unlisted")
    video_url = str(receipt.get("video_url") or receipt.get("url") or "")
    studio_url = str(receipt.get("studio_url") or "https://studio.youtube.com/")
    status = str(receipt.get("status") or "")
    simulated = status == "UPLOADED_SIMULATION" or not video_or_live(receipt)
    engine = str(story.get("engine") or "")
    if engine == "ffmpeg-fallback":
        engine = "FFmpeg (fallback, tanpa Remotion)"
    elif engine == "remotion":
        engine = "Whisper + Remotion + Concat"
    engine = engine or "Whisper + Remotion + Concat"

    ok = send_html(
        notifier,
        format_upload_done(
            title,
            privacy=privacy,
            video_url=video_url,
            studio_url=studio_url,
            engine=engine,
            simulated=simulated,
        ),
    )
    if not ok:
        return "NOTIFIKASI TELEGRAM UPLOAD GAGAL (cek token/chat id)"
    return "NOTIFIKASI TELEGRAM YOUTUBE TERKIRIM"


def video_or_live(receipt: dict[str, Any]) -> bool:
    """`True` hanya bila receipt berisi video_id (upload live sungguhan)."""
    return bool(str(receipt.get("video_id") or "").strip())
