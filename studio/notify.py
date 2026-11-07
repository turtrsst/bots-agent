"""Notifikasi keluar: Telegram Bot API atau konsol.

Tidak ada dependensi wajib — memakai `urllib` dari stdlib. `transport` bisa
di-inject pada test sehingga tidak perlu jaringan.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Settings

MAX_MESSAGE = 3800  # batas Telegram 4096, sisakan ruang


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


class ConsoleNotifier:
    """Fallback: cetak ke stdout (dipakai saat Telegram belum dikonfigurasi)."""

    supports_inbound = False

    def send(self, text: str, *, silent: bool = False) -> bool:
        print("\n📣 [NOTIFIKASI STUDIO]", flush=True)
        for line in (text or "").strip().splitlines():
            print(f"   {line}", flush=True)
        return True


def _default_transport(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = urlencode(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
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
        api_base: str = "https://api.telegram.org",
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.transport = transport or _default_transport
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
