"""Telegram two-way: webhook controller + pendaftaran `setWebhook`.

Padanan `kra_telegram_webhook.py` (spec Creator). Bedanya, perintah **benar-benar
menggerakkan pipeline** (bukan hanya menulis status):

===============  ============================================================
Perintah         Efek di kantor virtual
===============  ============================================================
``/status``      status pipeline + antrean + ringkasan memori
``/riwayat``     5 episode terakhir dari katalog SQLite (status + views + link)
``/thumb N``     pilih variasi thumbnail A/B → dipakai Sora saat upload
``/approve``     setujui naskah → Raka mulai render (job produksi)
``/help``        panduan singkat
===============  ============================================================

Keamanan: bila ``TELEGRAM_WEBHOOK_SECRET`` diisi, header
``X-Telegram-Bot-Api-Secret-Token`` wajib cocok (403 bila tidak). Bila
``TELEGRAM_CHAT_ID`` diisi, update dari chat lain diabaikan.

Tanpa `python-telegram-bot`: cukup stdlib + FastAPI yang sudah dipakai bridge.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from .config import Settings

try:  # dipakai sebagai anotasi route; FastAPI opsional (bridge tetap bisa diimpor tanpa itu)
    from fastapi import Request  # noqa: F401
except ImportError:  # pragma: no cover - hanya bila FastAPI belum dipasang
    from urllib.request import Request  # noqa: F401

HELP_TEXT = "\n".join(
    [
        "🤖 <b>PANDUAN KONTROL TELEGRAM KRA</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "/status — status pipeline & antrean render",
        "/riwayat — 5 konten terakhir dari memori SQLite",
        "/thumb 1|2|3 — pilih variasi thumbnail YouTube",
        "/approve — setujui naskah & mulai render Raka",
        "/help — panduan ini",
    ]
)

Transport = Any


# --------------------------------------------------------------------- util
def parse_command(text: str) -> tuple[str, str]:
    """`"/thumb 2"` → ``("thumb", "2")``; teks biasa → ``("", text)``."""
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return "", raw
    command, _, argument = raw.partition(" ")
    return command.lstrip("/").split("@")[0].lower(), argument.strip()


def _episodes_text(daemon: Any) -> str:
    episodes = daemon.memory.recent_episodes(5)
    if not episodes:
        return "📭 Belum ada riwayat konten di memori SQLite KRA."
    lines = ["🧠 <b>5 RIWAYAT KONTEN TERAKHIR DI KRA</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for episode in episodes:
        views = int(episode.get("views_count") or 0)
        views_text = f" · {views:,} views".replace(",", ".") if views else ""
        title = episode.get("primary_title") or "(tanpa judul)"
        lines.append(
            f"• <code>{episode.get('project_id')}</code> | <b>{title}</b> "
            f"[<i>{episode.get('status')}</i>]{views_text}"
        )
        if episode.get("youtube_url"):
            lines.append(f"   🔗 {episode['youtube_url']}")
    return "\n".join(lines)


def _status_text(daemon: Any) -> str:
    state = daemon.state.read()
    details = state.get("details") or {}
    stats = daemon.memory.stats()
    running = daemon.active_job()
    lines = [
        "⚙️ <b>STATUS KANTOR VIRTUAL KRA</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pipeline: <code>{state.get('status', 'IDLE')}</code>",
        f"Job aktif: <code>{running.id if running else '-'}</code>",
        f"Antrean: {daemon.queue.qsize()}",
        f"Episode memori: {stats.get('episodes', 0)} "
        f"({stats.get('published_episodes', 0)} tayang)",
        f"Views tercatat: {stats.get('total_views', 0)}",
        f"Diperbarui: {state.get('updated_at') or '-'}",
    ]
    if details:
        trimmed = {key: details[key] for key in list(details)[:6]}
        lines.append("<pre>" + json.dumps(trimmed, indent=2, ensure_ascii=False) + "</pre>")
    return "\n".join(lines)


def _approve(daemon: Any) -> dict[str, Any]:
    status = daemon.state.status()
    from .state import READY_RENDER, WAITING_CREATOR

    if status == READY_RENDER:
        job = daemon.submit_production("", source="telegram", topic=str(
            (daemon.state.read().get("details") or {}).get("topic") or ""
        ))
        return {
            "reply": (
                "🚀 <b>NASKAH DISETUJUI!</b> Raka_Producer mulai render Remotion + concat "
                f"(job <code>{job.id}</code>)."
            ),
            "job": job.as_dict(),
        }
    if status == WAITING_CREATOR:
        files = daemon._video_files()
        if files:
            job = daemon.submit_production(str(files[-1]), source="telegram")
            return {
                "reply": (
                    "🎬 Rekaman Creator terdeteksi — render dimulai "
                    f"(job <code>{job.id}</code>)."
                ),
                "job": job.as_dict(),
            }
        return {
            "reply": (
                "⚠️ Pipeline menunggu rekaman A-Roll. Taruh file di "
                "<code>input_creator/</code>, bot akan mendeteksinya otomatis."
            ),
            "job": None,
        }
    return {
        "reply": (
            f"⚠️ Belum ada naskah yang menunggu persetujuan (status: <code>{status}</code>). "
            "Mulai rapat dulu dengan /topic atau jadwal 08:00."
        ),
        "job": None,
    }


def handle_command(daemon: Any, text: str, *, via: str = "webhook") -> dict[str, Any]:
    """Jalankan satu perintah Telegram; kembalikan balasan + efek sampingnya."""
    command, argument = parse_command(text)
    if not command:
        return {
            "status": "no_text",
            "command": "",
            "reply": "",
        }

    if command in {"help", "start"}:
        return {"status": "ok", "command": command, "reply": HELP_TEXT}

    if command == "status":
        return {"status": "ok", "command": command, "reply": _status_text(daemon)}

    if command == "riwayat":
        return {"status": "ok", "command": command, "reply": _episodes_text(daemon)}

    if command == "thumb":
        if not argument.split(" ")[0].isdigit():
            return {
                "status": "ok",
                "command": command,
                "reply": (
                    "⚠️ Format salah. Gunakan <code>/thumb 1</code>, "
                    "<code>/thumb 2</code>, atau <code>/thumb 3</code>."
                ),
            }
        from .thumbnail import choose_thumbnail

        index = int(argument.split(" ")[0])
        chosen = choose_thumbnail(daemon.settings, index, via=via)
        if chosen is None:
            return {
                "status": "ok",
                "command": command,
                "reply": f"❌ Thumbnail variasi #{index} belum dirender Raka di "
                "<code>output_ready/thumbnails/</code>.",
            }
        daemon.state.update(daemon.state.status(), selected_thumbnail=chosen.name)
        return {
            "status": "ok",
            "command": command,
            "reply": (
                f"✅ <b>THUMBNAIL DIPILIH:</b> variasi #{index} (<code>{chosen.name}</code>) "
                "akan dipasang saat upload YouTube."
            ),
            "photo": str(chosen),
        }

    if command == "approve":
        result = _approve(daemon)
        return {"status": "ok", "command": command, **result}

    return {
        "status": "ok",
        "command": command,
        "reply": (
            f"❓ Perintah tidak dikenali: <code>{text[:120]}</code>\n"
            "Ketik <code>/help</code> untuk daftar perintah."
        ),
    }


# ------------------------------------------------------------------ kirim
def reply_notifier(daemon: Any, chat_id: str) -> Any:
    """Balasan dikirim ke chat pengirim (atau notifier utama bila tanpa token)."""
    from .notify import TelegramNotifier

    token = daemon.settings.telegram_token
    if token and chat_id:
        return TelegramNotifier(token, chat_id)
    return daemon.notifier


def deliver(daemon: Any, result: dict[str, Any], *, chat_id: str = "") -> dict[str, Any]:
    """Kirim balasan (foto bila ada) lalu kembalikan ringkasan tanpa isi HTML."""
    notifier = reply_notifier(daemon, chat_id)
    sent = False
    photo = str(result.get("photo") or "")
    if photo:
        sender = getattr(notifier, "send_photo", None)
        if callable(sender):
            sent = bool(sender(photo, caption=result.get("reply", "")))
        if not sent:
            sent = bool(notifier.send(result.get("reply", ""), silent=True))
    elif result.get("reply"):
        sent = bool(notifier.send(result.get("reply", ""), silent=True))
    return {"sent": sent, "photo": bool(photo)}


# ------------------------------------------------------------------ route
def register_telegram_webhook(app: Any, daemon: Any) -> None:
    """Pasang ``POST /kra/telegram-webhook`` pada aplikasi FastAPI."""
    from fastapi import Header, HTTPException

    @app.post("/kra/telegram-webhook")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str = Header(default=""),
    ) -> dict[str, Any]:
        settings: Settings = daemon.settings
        expected = settings.telegram_webhook_secret
        if expected and x_telegram_bot_api_secret_token != expected:
            raise HTTPException(status_code=403, detail="secret token webhook tidak cocok")

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - body bukan JSON
            return {"status": "bad_request"}

        message = body.get("message") or body.get("edited_message") or {}
        text = str(message.get("text") or "").strip()
        chat_id = str((message.get("chat") or {}).get("id") or "")

        allowed = settings.telegram_chat_id
        if allowed and chat_id and chat_id != str(allowed):
            return {"status": "ignored_unauthorized_chat"}
        if not text:
            return {"status": "no_text"}

        result = handle_command(daemon, text, via="telegram")
        delivery = deliver(daemon, result, chat_id=chat_id)
        return {
            "status": result.get("status", "ok"),
            "command": result.get("command", ""),
            "reply": result.get("reply", ""),
            "delivered": delivery["sent"],
            "job": (result.get("job") or {}).get("id", ""),
        }


# --------------------------------------------------------------- setWebhook
def _default_transport(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = urlencode(payload).encode("utf-8")
    request = UrlRequest(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL milik Telegram
        return json.loads(response.read().decode("utf-8"))


def set_webhook(
    webhook_url: str,
    *,
    token: str,
    secret: str = "",
    transport: Transport | None = None,
    api_base: str = "https://api.telegram.org",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Daftarkan URL webhook ke Telegram (``setWebhook``); tanpa jaringan → jujur."""
    if not token:
        return {"ok": False, "status": "NO_TOKEN", "description": "TELEGRAM_BOT_TOKEN kosong"}
    url = str(webhook_url or "").strip()
    if not url.startswith("https://"):
        return {
            "ok": False,
            "status": "URL_TIDAK_HTTPS",
            "description": "Telegram hanya menerima webhook https:// (pakai ngrok/cloudflare tunnel)",
        }
    payload: dict[str, Any] = {
        "url": url,
        "allowed_updates": json.dumps(["message", "edited_message"]),
    }
    if secret:
        payload["secret_token"] = secret
    poster = transport or _default_transport
    try:
        result = poster(f"{api_base.rstrip('/')}/bot{token}/setWebhook", payload, timeout)
    except (URLError, OSError, ValueError) as exc:
        return {"ok": False, "status": "ERROR", "description": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": bool(result.get("ok", False)),
        "status": "WEBHOOK_TERDAFTAR" if result.get("ok") else "GAGAL",
        "description": str(result.get("description") or ""),
        "url": url,
    }


def delete_webhook(*, token: str, transport: Transport | None = None, api_base: str = "https://api.telegram.org") -> dict[str, Any]:
    if not token:
        return {"ok": False, "status": "NO_TOKEN"}
    poster = transport or _default_transport
    try:
        result = poster(f"{api_base.rstrip('/')}/bot{token}/deleteWebhook", {}, 15.0)
    except (URLError, OSError, ValueError) as exc:
        return {"ok": False, "status": "ERROR", "description": f"{type(exc).__name__}: {exc}"}
    return {"ok": bool(result.get("ok", False)), "status": "WEBHOOK_DIHAPUS"}
