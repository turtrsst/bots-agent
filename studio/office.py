"""Perakitan kantor virtual: persona, tool, group chat, dan routing antar-bot.

Perbaikan penting dibanding skrip linear biasa:

1. Tiap bot **mengeksekusi tool-nya sendiri** (caller == executor). Di AutoGen 0.2,
   pesan berisi ``tool_calls`` dikembalikan ke peserta GroupChat yang punya fungsi
   tersebut — itu yang dilakukan ``func_call_filter``. Kalau eksekusi didelegasikan
   ke agen executor terpisah yang duduk di ruang rapat, pemilihan pembicara jadi
   liar (executor tidak pernah benar-benar "berbicara", lalu selector memilih acak).
   Implementasi tool tetap satu objek bersama (``StudioTools``).
2. Peserta pertama adalah ``Creator_Kamu`` (llm_config=False). Dengan begitu
   transisi awal selalu Creator → Vito, bukan undian LLM.
3. Semua bot memakai ``human_input_mode="NEVER"``; satu-satunya titik interaksi
   manusia adalah tool Bima (human-in-the-loop), sehingga bot tidak menggantung
   menunggu input di tengah rapat.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from autogen import ConversableAgent, GroupChat, GroupChatManager, register_function

from .config import Settings
from .personas import Persona, compose_system_message, load_persona
from .report import collect_files, write_run_report, youtube_url_from_receipt

TERMINATION_KEYWORDS = ("PIPELINE_COMPLETE", "AWAITING_CREATOR_UPLOAD")
TERMINATION_KEYWORD = TERMINATION_KEYWORDS[0]  # alias kompatibilitas
STAGES = ("full", "ideation", "production")

TOOL_DESCRIPTIONS: dict[str, str] = {
    "research_current_trends": (
        "Ambil tren YouTube/tren web HARI INI yang nyata (YouTube Data API, Tavily, atau "
        "SerpAPI) untuk topik tertentu. Panggil sebelum menyusun brief; kalau hasilnya "
        "'RISET TIDAK TERSEDIA', jangan mengarang angka tren."
    ),
    "save_scene_breakdown": (
        "Simpan scene breakdown mesin (JSON) ke ./workspace/script_production.json. File ini "
        "dibaca Remotion & modul concat; wajib diisi setelah naskah Markdown selesai."
    ),
    "assemble_story_from_script": (
        "Rakit semua scene dari ./workspace/script_production.json: Whisper per scene, render "
        "Remotion (aset disalin ke public/kra_temp), lalu concat. Jalur utama bila naskah JSON ada."
    ),
    "save_content_brief": (
        "Simpan content brief (3 judul, thumbnail, angle, audiens, keyword) ke "
        "./workspace/content_brief.json. Panggil setelah brief selesai, jangan hanya menulis di chat."
    ),
    "save_production_script": (
        "Simpan naskah produksi final ke ./workspace/script_production.md dengan penanda "
        "[CREATOR A-ROLL] / [AI B-ROLL]. Panggil setiap selesai menulis atau merevisi."
    ),
    "request_and_check_creator_files": (
        "Kirim daftar tugas rekaman ke Creator (manusia) dan tunggu sampai file A-Roll "
        "benar-benar ada di ./input_creator/. Mengembalikan STATUS UPLOADED bila siap."
    ),
    "assemble_and_render_video": (
        "Render rekaman mentah Creator dari ./input_creator/ menjadi video final di "
        "./output_ready/ (ffmpeg 1080p60 + normalisasi audio -14 LUFS bila aktif)."
    ),
    "inspect_rendered_output": (
        "Audit bukti fisik file video di ./output_ready/: ukuran, hash, header MP4, "
        "probe resolusi, dan laporan render. Ini satu-satunya sumber kebenaran kualitas."
    ),
    "upload_to_youtube": (
        "Upload video final ke YouTube Data API v3 dengan metadata SEO + Chapters. "
        "Tanpa client_secret.json, tool menulis receipt simulasi."
    ),
}


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    role: str
    persona_file: str
    description: str
    fallback_prompt: str
    protocol: str
    default_reply: str
    tools: tuple[str, ...] = ()
    handoff: tuple[str, ...] = ()


AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        key="vito",
        name="Vito_TrendStrategist",
        role="Trend & Title Strategist",
        persona_file="trend-researcher.md",
        description=(
            "Vito — riset tren, 3 opsi judul ber-CTR tinggi, konsep thumbnail, dan angle unik. "
            "Bicara pertama setelah topik masuk, lalu menyerahkan brief ke Clara_Scriptwriter."
        ),
        fallback_prompt=(
            "Kamu Vito, Trend Strategist. Buat 3 opsi judul (<55 karakter), konsep thumbnail, "
            "dan angle unik; simpan dengan save_content_brief, lalu serahkan ke Clara_Scriptwriter."
        ),
        protocol=(
            "## ALUR KERJAMU\n"
            "0. Panggil `research_current_trends` lebih dulu untuk melihat tren nyata hari ini. "
            "Kalau hasilnya 'RISET TIDAK TERSEDIA', jangan mengarang angka tren.\n"
            "1. Riset sudut pandang yang belum jenuh untuk topik Creator.\n"
            "2. Tulis brief: 3 judul (<55 karakter), 3 konsep thumbnail, angle, target audiens, "
            "3 keyword.\n"
            "3. Panggil `save_content_brief(...)` (file otomatis menjadi content_brief.json).\n"
            "4. Baru setelah tool mengembalikan TERSIMPAN, serahkan ke Clara_Scriptwriter.\n"
            "Jangan pernah menulis naskah; itu tugas Clara."
        ),
        default_reply=(
            "Clara_Scriptwriter, brief saya sudah final di ./workspace/content_brief.json. "
            "Silakan tulis naskahnya sekarang."
        ),
        tools=("research_current_trends", "save_content_brief"),
        handoff=("Clara_Scriptwriter",),
    ),
    AgentSpec(
        key="clara",
        name="Clara_Scriptwriter",
        role="Retention Scriptwriter",
        persona_file="script-architect.md",
        description=(
            "Clara — penulis naskah retensi tinggi dengan pemisahan tegas [CREATOR A-ROLL] dan "
            "[AI B-ROLL], plus scene breakdown JSON untuk Remotion. Revisi bila ditolak Damon."
        ),
        fallback_prompt=(
            "Kamu Clara, Retention Scriptwriter. Tulis naskah 2 kolom dari brief Vito, tandai tegas "
            "[CREATOR A-ROLL] dan [AI B-ROLL], simpan dengan save_production_script, lalu minta audit "
            "Damon_RealityChecker."
        ),
        protocol=(
            "## ALUR KERJAMU\n"
            "1. Baca ./workspace/content_brief.json (hasil Vito) sebelum menulis.\n"
            "2. Naskah wajib punya: hook <= 15 detik, konflik/insight, bukti, CTA.\n"
            "3. Setiap baris naskah HARUS diawali penanda `[CREATOR A-ROLL]` (diucapkan manusia) "
            "atau `[AI B-ROLL]` (visual/b-roll). Sertakan timecode kasar.\n"
            "4. Dilarang: sapaan 'Halo guys', 'di video kali ini', intro panjang.\n"
            "5. Panggil `save_production_script(...)` (file otomatis menjadi script_production.md).\n"
            "6. WAJIB juga panggil `save_scene_breakdown(...)` dengan JSON scene breakdown "
            "(project_id, title, fps, scenes[]: scene_id, type REMOTION_DYNAMIC/CREATOR_AROLL, "
            "narration_text, broll_asset atau source_video, highlight_words). Ini yang dibaca "
            "Remotion & concat. Kalau tool membalas ERROR SCHEMA/ERROR JSON, perbaiki lalu simpan ulang.\n"
            "7. Baru serahkan ke Damon_RealityChecker. Bila ditolak, revisi dan SIMPAN ULANG kedua file."
        ),
        default_reply=(
            "Damon_RealityChecker, naskah revisi saya sudah tersimpan di ./workspace/script_production.md "
            "beserta scene breakdown ./workspace/script_production.json. Mohon audit ulang."
        ),
        tools=("save_production_script", "save_scene_breakdown"),
        handoff=("Damon_RealityChecker",),
    ),
    AgentSpec(
        key="damon",
        name="Damon_RealityChecker",
        role="Reality Checker / QC dengan hak veto",
        persona_file="reality-checker.md",
        # Deskripsi ikut disiarkan sebagai "introductions" ke semua bot: jangan memuat
        # kata kunci vonis (SCRIPT PASS/VIDEO PASS) supaya tidak dianggap status nyata.
        description=(
            "Damon — auditor sinis dengan hak veto. Memutuskan naskah Clara layak diproduksi dan "
            "memverifikasi bukti fisik video Raka lewat tool inspect_rendered_output."
        ),
        fallback_prompt=(
            "Kamu Damon, Reality Checker dengan hak veto. Tahap 1: audit naskah Clara, beri SCRIPT PASS "
            "atau tolak. Tahap 2: setelah Raka render, WAJIB panggil inspect_rendered_output pada "
            "'final_render.mp4'; beri VIDEO PASS hanya bila bukti fisik valid."
        ),
        protocol=(
            "## DUA GERBANG VETO\n"
            "**Gerbang 1 — naskah.** Periksa bukti di ./workspace/script_production.md: ada hook tajam, "
            "tidak ada 'Halo guys', pemisahan [CREATOR A-ROLL]/[AI B-ROLL] jelas, ada CTA. "
            "Lolos → tulis `SCRIPT PASS` dan serahkan ke Bima_StudioPM. Tidak lolos → tolak dengan "
            "3 poin perbaikan konkret dan kembalikan ke Clara_Scriptwriter.\n"
            "**Gerbang 2 — video.** Setelah Raka mengklaim render, WAJIB panggil "
            "`inspect_rendered_output('final_render.mp4')`. Hanya bila hasil tool berisi 'AUDIT LULUS' "
            "kamu boleh menulis `VIDEO PASS` dan menyerahkan ke Sora_YouTubePublisher. Bila 'AUDIT GAGAL', "
            "kembalikan ke Raka_Producer dengan bukti tool.\n"
            "Kamu tidak boleh merender, menulis naskah, atau mengunggah apa pun."
        ),
        default_reply=(
            "Saya belum menerima bukti tool yang cukup untuk memberi vonis. Kirim hasil render atau "
            "naskah final beserta path filenya."
        ),
        tools=("inspect_rendered_output",),
        handoff=("Clara_Scriptwriter", "Bima_StudioPM", "Raka_Producer", "Sora_YouTubePublisher"),
    ),
    AgentSpec(
        key="bima",
        name="Bima_StudioPM",
        role="Senior PM / Gatekeeper human-in-the-loop",
        persona_file="senior-pm-gatekeeper.md",
        description=(
            "Bima — PM yang menagih aset rekaman ke Creator (manusia) lewat tool "
            "request_and_check_creator_files dan baru mengizinkan Raka render setelah file terverifikasi."
        ),
        fallback_prompt=(
            "Kamu Bima, Senior PM. Setelah naskah lolos Damon, panggil "
            "request_and_check_creator_files untuk menagih 'a_roll_raw.mp4' dari Creator. Jangan izinkan "
            "Raka render sebelum status UPLOADED."
        ),
        protocol=(
            "## ALUR KERJAMU\n"
            "1. Bertindak hanya setelah Damon memberi `SCRIPT PASS`.\n"
            "2. Susun daftar aset rekaman yang jelas (A-Roll utama, b-roll tambahan, layar/overlay) "
            "berdasarkan naskah Clara.\n"
            "3. Panggil `request_and_check_creator_files(...)` dengan expected_filename 'a_roll_raw.mp4'.\n"
            "4. Bila hasilnya `STATUS UPLOADED`, serahkan ke Raka_Producer dengan spesifikasi render.\n"
            "5. Bila `STATUS BLOCKED`/`TIMEOUT`/`CANCELLED`, hentikan pipeline dan laporkan ke Creator; "
            "jangan menyuruh Raka bekerja.\n"
            "6. Bila tool mengembalikan `STATUS AWAITING` (mode daemon), Creator akan dihubungi lewat "
            "notifikasi. Tutup rapat sekarang dengan kata kunci AWAITING_CREATOR_UPLOAD — jangan "
            "menyuruh Raka render di fase ini."
        ),
        default_reply=(
            "Raka_Producer, file Creator sudah saya verifikasi ada di ./input_creator/. Silakan render."
        ),
        tools=("request_and_check_creator_files",),
        handoff=("Raka_Producer",),
    ),
    AgentSpec(
        key="raka",
        name="Raka_Producer",
        role="Media Producer / Editor otomatis",
        persona_file="media-producer.md",
        description=(
            "Raka — editor yang merakit final_render.mp4 dari naskah Clara: Remotion per scene + "
            "concat (assemble_story_from_script) atau render satu file (assemble_and_render_video)."
        ),
        fallback_prompt=(
            "Kamu Raka, Media Producer. Setelah Bima mengonfirmasi STATUS UPLOADED, jalankan "
            "assemble_and_render_video('a_roll_raw.mp4', 'final_render.mp4') dan serahkan hasilnya ke "
            "Damon_RealityChecker."
        ),
        protocol=(
            "## ALUR KERJAMU\n"
            "1. Bekerja hanya setelah Bima menyatakan `STATUS UPLOADED` (atau di fase produksi daemon).\n"
            "2. Bila ./workspace/script_production.json ada, itu jalur utama: panggil "
            "`assemble_story_from_script('script_production.json', 'final_render.mp4')` — "
            "scene dirakit dengan Whisper + Remotion lalu digabung concat.\n"
            "3. Bila naskah JSON belum ada, pakai `assemble_and_render_video('a_roll_raw.mp4', "
            "'final_render.mp4')`.\n"
            "4. Laporkan hasil tool apa adanya: engine (remotion/ffmpeg-fallback/simulation), "
            "jumlah scene yang gagal, ukuran, dan catatan.\n"
            "5. Serahkan ke Damon_RealityChecker untuk QC. Jangan mengklaim 'sudah bagus' tanpa audit."
        ),
        default_reply=(
            "Damon_RealityChecker, render selesai: ./output_ready/final_render.mp4. Silakan audit bukti fisiknya."
        ),
        tools=("assemble_and_render_video", "assemble_story_from_script"),
        handoff=("Damon_RealityChecker",),
    ),
    AgentSpec(
        key="sora",
        name="Sora_YouTubePublisher",
        role="YouTube Ops & SEO Specialist",
        persona_file="youtube-publisher.md",
        description=(
            "Sora — publisher terakhir. Setelah video lolos QC Damon, memanggil upload_to_youtube "
            "dengan metadata SEO + Chapters, lalu menutup rapat."
        ),
        fallback_prompt=(
            "Kamu Sora, YouTube Ops & SEO. Bekerja hanya setelah Damon memberi VIDEO PASS. Panggil "
            "upload_to_youtube dengan status 'unlisted' dan Chapters mulai 00:00, lalu laporkan URL dan "
            "akhiri dengan 'PIPELINE_COMPLETE'."
        ),
        protocol=(
            "## ALUR KERJAMU\n"
            "1. Bekerja hanya setelah Damon menyatakan `VIDEO PASS`.\n"
            "2. Susun judul final (<= 55 karakter, pakai pemenang dari brief), deskripsi dengan "
            "Chapters yang dimulai `00:00`, dan 5-10 tag.\n"
            "3. Panggil `upload_to_youtube(...)` dengan privacy_status 'unlisted'.\n"
            "4. Laporkan hasil tool: URL/receipt, peringatan SEO, mode live atau simulasi.\n"
            "5. Akhiri pesan dengan kata kunci persis: PIPELINE_COMPLETE."
        ),
        default_reply=(
            "Upload sudah saya proses. Video final ada di ./output_ready/. PIPELINE_COMPLETE"
        ),
        tools=("upload_to_youtube",),
        handoff=("Damon_RealityChecker",),
    ),
)

AGENT_BY_KEY = {spec.key: spec for spec in AGENT_SPECS}


def kickoff_message(topic: str, stage: str = "full", raw_file: str = "") -> str:
    if stage == "ideation":
        return (
            "FASE IDEASI DIMULAI (belum masuk produksi).\n"
            f"Topik dari Creator: '{topic}'.\n\n"
            "1. Vito_TrendStrategist — brief + 3 judul ber-CTR tinggi → `save_content_brief`.\n"
            "2. Clara_Scriptwriter — naskah [CREATOR A-ROLL]/[AI B-ROLL] → `save_production_script`, "
            "lalu scene breakdown JSON → `save_scene_breakdown`.\n"
            "3. Damon_RealityChecker — audit naskah, minta revisi bila perlu, setujui bila layak.\n"
            "4. Bima_StudioPM — setelah disetujui, panggil tool untuk menagih 'a_roll_raw.mp4' ke Creator.\n\n"
            # kata kunci terminasi tidak boleh muncul di pesan kickoff
            "Mode daemon: tool Bima tidak akan memblokir terminal. Bila hasilnya STATUS AWAITING, "
            "tutup rapat dengan kata kunci penutup yang ada di system prompt Bima. "
            "Jangan menyuruh Raka render di fase ini."
        )
    if stage == "production":
        raw = raw_file or "a_roll_raw.mp4"
        return (
            "FASE PRODUKSI DIMULAI.\n"
            f"Rekaman Creator: '{raw}' sudah tersedia di ./input_creator/.\n"
            f"Naskah & brief sudah ada di ./workspace/ dan sudah disetujui Damon.\n\n"
            "1. Raka_Producer — panggil `assemble_story_from_script('script_production.json', "
            "'final_render.mp4')` bila naskah JSON ada; kalau tidak, `assemble_and_render_video("
            "'a_roll_raw.mp4', 'final_render.mp4')`.\n"
            "2. Damon_RealityChecker — audit bukti fisik file hasil render.\n"
            "3. Sora_YouTubePublisher — upload dengan metadata SEO + Chapters, lalu tutup rapat "
            "dengan kata kunci penutup.\n\n"
            "Jangan mengulang riset/naskah; fokus ke produksi dan publikasi."
        )
    return (
        "PROYEK BARU DIMULAI!\n"
        f"Topik dari Creator: '{topic}'.\n\n"
        "Rapat produksi otonom, urutannya:\n"
        "1. Vito_TrendStrategist — content brief + 3 judul ber-CTR tinggi → `content_brief.json`.\n"
        "2. Clara_Scriptwriter — naskah retensi tinggi dengan [CREATOR A-ROLL]/[AI B-ROLL] → "
        "`script_production.md`, PLUS scene breakdown JSON → `script_production.json` "
        "(dipakai Remotion & concat).\n"
        "3. Damon_RealityChecker — audit naskah. Minta revisi bila perlu, setujui bila layak produksi.\n"
        "4. Bima_StudioPM — tagih rekaman 'a_roll_raw.mp4' dari Creator lewat tool.\n"
        "5. Raka_Producer — rakit 'final_render.mp4': pakai `assemble_story_from_script` bila "
        "naskah JSON ada, atau `assemble_and_render_video` untuk jalur satu file.\n"
        "6. Damon_RealityChecker — audit bukti fisik file video sebelum boleh tayang.\n"
        # JANGAN menulis kata kunci terminasi di sini: pesan kickoff ikut dibaca
        # _is_termination_msg dan akan menutup rapat di putaran pertama.
        "7. Sora_YouTubePublisher — upload metadata SEO penuh, lalu tutup rapat dengan kata kunci penutup "
        "yang ada di instruksimu.\n\n"
        "Aturan: jangan melompati tahap, jangan mengarang hasil tool, dan sebut nama bot tujuan "
        "di akhir pesanmu."
    )


@dataclass
class RunSummary:
    topic: str
    completed: bool
    rounds: int
    elapsed_seconds: float
    awaiting_creator: bool = False
    files: list[str] = field(default_factory=list)
    youtube_url: str = ""
    transcript_path: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.awaiting_creator:
            return "AWAITING_CREATOR_UPLOAD"
        if self.completed:
            return "PIPELINE_COMPLETE"
        return "BERHENTI TANPA PIPELINE_COMPLETE"


def _is_complete(message: dict[str, Any] | None) -> bool:
    content = str((message or {}).get("content") or "").upper()
    return any(keyword in content for keyword in TERMINATION_KEYWORDS)


def continuation_selector(last_speaker, groupchat):
    """Aturan pemilihan pembicara.

    AutoGen menaruh pesan berisi ``tool_calls`` lalu mengeksekusinya, tetapi giliran
    berikutnya jatuh ke bot lain menurut graf transisi — bot pemilik tool tidak pernah
    membacakan hasil tool-nya. Selector ini mengembalikan giliran ke pemilik tool
    (pesan terakhir ber-role ``tool``), dan menyerahkan sisanya ke pemilihan LLM
    (``"auto"``) yang dibatasi graf transisi.
    """
    messages = groupchat.messages
    if messages and messages[-1].get("role") == "tool":
        return last_speaker
    return "auto"


def build_transitions(
    agents: dict[str, ConversableAgent], creator: ConversableAgent, stage: str = "full"
) -> dict:
    """Graf pembicara per fase. Damon punya jalur balik (maker-checker loop)."""
    if stage == "ideation":
        return {
            creator: [agents["vito"]],
            agents["vito"]: [agents["clara"]],
            agents["clara"]: [agents["damon"]],
            agents["damon"]: [agents["clara"], agents["bima"]],
            # Bima sengaja tidak punya tujuan: rapat berakhir setelah ia menagih file.
        }
    if stage == "production":
        return {
            creator: [agents["raka"]],
            agents["raka"]: [agents["damon"]],
            agents["damon"]: [agents["raka"], agents["sora"]],
            agents["sora"]: [agents["damon"]],
        }
    # Tanpa self-loop: giliran setelah tool sudah dijamin oleh continuation_selector,
    # sehingga graf ini murni maju dan bot tidak bisa "ngobrol sendiri" berputar-putar.
    return {
        creator: [agents["vito"]],
        agents["vito"]: [agents["clara"]],
        agents["clara"]: [agents["damon"]],
        agents["damon"]: [
            agents["clara"],   # tolak naskah → revisi
            agents["bima"],    # SCRIPT PASS
            agents["raka"],    # VIDEO FAIL → render ulang
            agents["sora"],    # VIDEO PASS
        ],
        agents["bima"]: [agents["raka"], agents["damon"]],
        agents["raka"]: [agents["damon"]],
        agents["sora"]: [agents["damon"], agents["bima"]],
    }


class Office:
    """Wadah semua komponen yang sudah dirakit dan siap dijalankan."""

    def __init__(
        self,
        settings: Settings,
        tools: Any,
        agents: dict[str, ConversableAgent],
        creator: ConversableAgent,
        group_chat: GroupChat,
        manager: GroupChatManager,
        persona_sources: dict[str, Persona],
        stage: str = "full",
    ) -> None:
        self.settings = settings
        self.tools = tools
        self.agents = agents
        self.creator = creator
        self.group_chat = group_chat
        self.manager = manager
        self.persona_sources = persona_sources
        self.stage = stage

    # ------------------------------------------------------------------ run
    def run(self, topic: str, *, raw_file: str = "") -> RunSummary:
        started = time.monotonic()
        self.creator.initiate_chat(
            self.manager,
            message=kickoff_message(topic, self.stage, raw_file=raw_file),
            clear_history=True,
        )
        elapsed = time.monotonic() - started
        return self.summarize(topic, elapsed)

    def summarize(self, topic: str, elapsed: float) -> RunSummary:
        messages = list(self.group_chat.messages)
        completed = any(_is_complete(message) for message in messages)
        summary = RunSummary(
            topic=topic,
            completed=completed,
            awaiting_creator=any(
                "AWAITING_CREATOR_UPLOAD" in str(message.get("content") or "").upper()
                for message in messages
            ),
            rounds=len(messages),
            elapsed_seconds=elapsed,
            files=collect_files(self.settings.paths),
            youtube_url=youtube_url_from_receipt(self.settings.paths) or self._youtube_url_from_chat(),
            messages=messages,
        )
        summary.transcript_path = write_run_report(self.settings.paths, summary, self.settings.model)
        return summary

    # -------------------------------------------------------------- helpers
    def _youtube_url_from_chat(self) -> str:
        for message in reversed(self.group_chat.messages):
            match = re.search(r"https://youtu\.be/[\w-]+", str(message.get("content") or ""))
            if match:
                return match.group(0)
        return ""


def build_office(
    settings: Settings,
    *,
    stage: str = "full",
    memory_context: str = "",
) -> Office:
    """Rakit 6 bot, Creator, dan GroupChat dengan tool yang sudah terpasang.

    ``stage`` menentukan graf pembicara & pesan kickoff:

    * ``"full"``       — satu rapat panjang (CLI interaktif/rehearsal);
    * ``"ideation"``   — Vito → Clara → Damon → Bima, berhenti saat menagih rekaman;
    * ``"production"`` — Raka → Damon → Sora, dipakai daemon setelah file muncul.

    ``memory_context`` (dari :class:`studio.memory.MemoryStore`) disuntikkan ke
    system prompt Vito & Sora saja.
    """
    if stage not in STAGES:
        raise ValueError(f"stage '{stage}' tidak dikenal; pilih salah satu dari {STAGES}")
    from .tools import StudioTools  # import lokal agar modul ini ringan

    settings.paths.ensure()
    tools = StudioTools(settings)
    llm_config = settings.llm_config()

    agents: dict[str, ConversableAgent] = {}
    persona_sources: dict[str, Persona] = {}
    for spec in AGENT_SPECS:
        persona = load_persona(settings.paths.agents, spec.persona_file, spec.fallback_prompt)
        persona_sources[spec.key] = persona
        system_message = compose_system_message(
            persona,
            name=spec.name,
            role=spec.role,
            tools=spec.tools,
            handoff=spec.handoff,
        )
        if memory_context and spec.key in {"vito", "sora"}:
            system_message += "\n" + memory_context.strip() + "\n"
        agents[spec.key] = ConversableAgent(
            name=spec.name,
            system_message=system_message,
            description=spec.description,
            llm_config=llm_config,
            human_input_mode="NEVER",
            code_execution_config=False,
            max_consecutive_auto_reply=12,
            is_termination_msg=_is_complete,
            default_auto_reply=spec.default_reply,
        )

    tool_functions = tools.build_functions()
    for spec in AGENT_SPECS:
        for tool_name in spec.tools:
            register_function(
                tool_functions[tool_name],
                caller=agents[spec.key],
                executor=agents[spec.key],  # self-execute: tool call dirutekan balik oleh GroupChat
                name=tool_name,
                description=TOOL_DESCRIPTIONS[tool_name],
            )

    creator = ConversableAgent(
        name="Creator_Kamu",
        description="Creator (manusia) yang memberi topik dan mengunggah rekaman A-Roll.",
        llm_config=False,
        human_input_mode="NEVER",
        code_execution_config=False,
        is_termination_msg=_is_complete,
    )

    group_chat = GroupChat(
        agents=[creator, *(agents[spec.key] for spec in AGENT_SPECS)],
        messages=[],
        max_round=settings.max_rounds,
        allowed_or_disallowed_speaker_transitions=build_transitions(agents, creator, stage),
        speaker_transitions_type="allowed",
        speaker_selection_method=continuation_selector,
        send_introductions=True,
    )
    manager = GroupChatManager(
        groupchat=group_chat,
        llm_config=llm_config,
        human_input_mode="NEVER",
        is_termination_msg=_is_complete,
    )

    return Office(
        settings=settings,
        tools=tools,
        agents=agents,
        creator=creator,
        group_chat=group_chat,
        manager=manager,
        persona_sources=persona_sources,
        stage=stage,
    )
