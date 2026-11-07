"""LLM palsu deterministik untuk menguji routing GroupChat tanpa API key.

Bukan mock mainan: seluruh alur AutoGen tetap berjalan (pemilihan pembicara,
``func_call_filter``, eksekusi tool sungguhan, terminasi). Yang diganti hanya
``OpenAIWrapper.create`` sehingga tiap bot mengembalikan jawaban yang sudah
ditulis sesuai tahap pipeline.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from openai.types.chat import ChatCompletion

AGENT_NAMES = (
    "Vito_TrendStrategist",
    "Clara_Scriptwriter",
    "Damon_RealityChecker",
    "Bima_StudioPM",
    "Raka_Producer",
    "Sora_YouTubePublisher",
)
CREATOR = "Creator_Kamu"

BRIEF_JSON = json.dumps(
    {
        "topic": "Otomasi produksi video",
        "titles": [
            {"title": "Otomasi Produksi Video — Ini yang Tidak Dibilang Orang", "chars": 52, "hook": "loss aversion"},
            {"title": "Otomasi Produksi Video: 3 Kesalahan Fatal", "chars": 46, "hook": "relatable"},
            {"title": "Otomasi Produksi Video Tanpa Buang Waktu", "chars": 45, "hook": "efisiensi"},
        ],
        "thumbnails": [{"concept": "wajah kaget + teks 3 kata"}],
        "audience": "creator solo",
        "keywords": ["ai agent", "otomasi konten"],
        "promise": "paham alur kerja dalam 8 menit",
    },
    ensure_ascii=False,
)

SCRIPT_V1 = "# NASKAH v1\nHalo guys, di video kali ini kita bahas otomasi.\n[CREATOR A-ROLL] Intro.\n"
SCRIPT_V2 = (
    "# NASKAH v2\n"
    "00:00 [CREATOR A-ROLL] Kamu masih buang 6 jam untuk satu video.\n"
    "00:20 [AI B-ROLL] Timeline editing penuh.\n"
    "02:00 [CREATOR A-ROLL] Pisahkan riset dan naskah.\n"
    "06:30 [CREATOR A-ROLL] Komentar 'WORKFLOW' untuk template.\n"
    "CTA di akhir.\n"
)

DESCRIPTION = "00:00 Hook\n00:30 Konteks\n02:00 Solusi\n06:30 CTA"


def _completion(message: dict[str, Any]) -> ChatCompletion:
    finish = "tool_calls" if message.get("tool_calls") else "stop"
    payload: dict[str, Any] = {
        "id": f"chatcmpl-mock-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": message.get("content"),
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }
    if message.get("tool_calls"):
        payload["choices"][0]["message"]["tool_calls"] = message["tool_calls"]
    response = ChatCompletion.model_validate(payload)
    # OpenAIWrapper biasanya menempelkan atribut ini saat memanggil API asli.
    response.message_retrieval_function = lambda resp: [choice.message for choice in resp.choices]
    response.cost = 0.0
    response.config_id = 0
    return response


def _tool_call(name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ]
    }


class MockLLM:
    """Menggantikan OpenAIWrapper.create dengan skenario pipeline lengkap."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.agents_used: list[str] = []

    # ---------------------------------------------------------------- install
    def install(self, monkeypatch) -> None:
        from autogen.oai.client import OpenAIWrapper

        mock = self

        def create(client, **config: Any) -> ChatCompletion:
            # plain function (bukan bound method) supaya descriptor-nya tetap
            # menerima instance OpenAIWrapper sebagai argumen pertama
            return mock.create(client, **config)

        monkeypatch.setattr(OpenAIWrapper, "create", create)

    # ------------------------------------------------------------------ core
    def create(self, client, **config: Any) -> ChatCompletion:
        messages = config.get("messages") or []
        system = next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), "")
        self.requests.append({"system": system, "messages": messages})

        if "role play game" in system.lower():
            return _completion({"content": self.select_speaker(messages)})

        agent = self._detect_agent(system)
        self.agents_used.append(agent)
        return _completion(self.reply(agent, messages))

    # -------------------------------------------------------------- discovery
    @staticmethod
    def _detect_agent(system: str) -> str:
        # compose_system_message menulis "Kamu adalah **Nama_Bot**"; persona lain
        # juga menyebut nama rekan, jadi cari penanda tebal dulu.
        marked = re.search(r"Kamu adalah \*\*([A-Za-z_]+)\*\*", system)
        if marked and marked.group(1) in AGENT_NAMES:
            return marked.group(1)
        for name in AGENT_NAMES:
            if name in system:
                return name
        raise AssertionError(f"Tidak bisa mengenali agen dari system prompt: {system[:200]!r}")

    @staticmethod
    def _transcript(messages: list[dict[str, Any]]) -> str:
        # system prompt berisi nama tool & kata kunci ("SCRIPT PASS", "RENDER SELESAI"),
        # jadi jangan diikutkan saat menentukan tahap pipeline
        return "\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") != "system"
        )

    @staticmethod
    def _last_group_message(messages: list[dict[str, Any]]) -> dict[str, Any]:
        for message in reversed(messages):
            if message.get("name") in AGENT_NAMES or message.get("name") == CREATOR:
                return message
        return {}

    def _last_speaker(self, messages: list[dict[str, Any]]) -> str:
        return self._last_group_message(messages).get("name", CREATOR)

    # ------------------------------------------------------------- selector
    def select_speaker(self, messages: list[dict[str, Any]]) -> str:
        message = self._last_group_message(messages)
        last = message.get("name", CREATOR)
        if last == CREATOR:
            return "Vito_TrendStrategist"

        # Pesan tool hasil eksekusi: beri giliran lagi ke bot yang sama.
        if message.get("role") == "tool":
            return last

        # Nama tujuan yang disebut bot di akhir pesannya.
        content = str(message.get("content") or "")
        for name in AGENT_NAMES:
            if name in content and name != last:
                return name
        return self._fallback_next(last)

    @staticmethod
    def _fallback_next(last: str) -> str:
        order = list(AGENT_NAMES)
        try:
            return order[order.index(last) + 1]
        except (ValueError, IndexError):
            return "Vito_TrendStrategist"

    # ---------------------------------------------------------------- skenario
    def reply(self, agent: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        text = self._transcript(messages)

        if agent == "Vito_TrendStrategist":
            if "TERSIMPAN: ./workspace/content_brief.json" not in text:
                return _tool_call("save_content_brief", content=BRIEF_JSON)
            return {"content": "Brief final di ./workspace/content_brief.json. Clara_Scriptwriter, tulis naskahnya."}

        if agent == "Clara_Scriptwriter":
            saves = text.count("TERSIMPAN: ./workspace/script_production.md")
            if saves == 0:
                return _tool_call("save_production_script", content=SCRIPT_V1)
            if "SCRIPT REJECT" in text and saves < 2:
                return _tool_call("save_production_script", content=SCRIPT_V2)
            return {"content": "Naskah siap. Damon_RealityChecker, mohon audit."}

        if agent == "Damon_RealityChecker":
            if "RENDER SELESAI" in text:
                if "AUDIT LULUS" in text:
                    return {"content": "VIDEO PASS. Sora_YouTubePublisher, silakan upload dengan metadata SEO."}
                return _tool_call("inspect_rendered_output", video_filename="final_render.mp4")
            if "SCRIPT REJECT" in text:
                return {"content": "SCRIPT PASS setelah revisi. Bima_StudioPM, tagih rekaman Creator."}
            return {"content": "SCRIPT REJECT: hook terlalu lambat dan masih ada 'Halo guys'. Clara_Scriptwriter, revisi."}

        if agent == "Bima_StudioPM":
            if re.search(r"STATUS UPLOADED(?: \(SIMULASI\))?:", text):
                return {"content": "File Creator terverifikasi. Raka_Producer, render final_render.mp4 sekarang."}
            return _tool_call(
                "request_and_check_creator_files",
                required_files_info="- [ ] A-Roll semua segmen\n- [ ] 2 b-roll",
                expected_filename="a_roll_raw.mp4",
            )

        if agent == "Raka_Producer":
            if "RENDER SELESAI" in text:
                return {"content": "Render selesai di ./output_ready/final_render.mp4. Damon_RealityChecker, audit."}
            return _tool_call(
                "assemble_and_render_video",
                raw_video_filename="a_roll_raw.mp4",
                output_filename="final_render.mp4",
            )

        if agent == "Sora_YouTubePublisher":
            if "UPLOAD YOUTUBE" in text:
                return {"content": "Upload selesai, receipt di ./output_ready/youtube_publish_receipt.json. PIPELINE_COMPLETE"}
            return _tool_call(
                "upload_to_youtube",
                video_filename="final_render.mp4",
                title="Otomasi Produksi Video Tanpa Buang Waktu",
                description=DESCRIPTION,
                tags=["ai agent", "otomasi konten", "workflow"],
                privacy_status="unlisted",
            )

        raise AssertionError(f"Agen tak dikenal: {agent}")


def extract_tool_calls(messages: list[dict[str, Any]]) -> list[str]:
    """Helper untuk assertion di test."""
    names: list[str] = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            names.append(call["function"]["name"])
    return names


def mentions_pipeline_complete(messages: list[dict[str, Any]]) -> bool:
    return any(re.search(r"PIPELINE_COMPLETE", str(m.get("content") or "")) for m in messages)
