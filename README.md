# bots-agent — Autonomous Virtual Creator Office

Enam "Super Bot" AutoGen yang duduk di satu ruang rapat: berdebat, saling mereview,
memakai tool sungguhan (tulis file, render video, audit bukti, upload YouTube), dan
berhenti sendiri saat video selesai dipublikasikan.

Proyek ini adalah implementasi yang **sudah diperbaiki dan diuji** dari arsitektur
`GroupChat` + `GroupChatManager` di artikel "Virtual Office Superbots". Versi asli
tidak akan berjalan sesuai deskripsinya — lihat [Perbaikan penting](#perbaikan-penting).

Sejak v2.0 kantor ini berjalan **24/7 tanpa terminal**: daemon membangunkan Vito
sesuai jadwal, memantau folder `input_creator/`, merender dengan ffmpeg/Whisper/
auto-editor/Remotion, mengingat performa video sebelumnya, dan mengabari Anda lewat
Telegram. Lihat [Daemon 24/7](#6-daemon-247) dan [Integrasi KRA & n100n](#7-integrasi-kra--n100n).

Sejak v2.1 kantor ini juga bisa **faceless**: tanpa rekaman Creator sama sekali. Clara
menulis narasi, mesin TTS (ElevenLabs → Edge-TTS) mengisi suara, Whisper menyelaraskan
caption, Remotion merakit. Scheduler dual-loop `n100n` (08:00 + polling 30 menit)
cukup memanggil tiga endpoint. Lihat [Mode faceless & scheduler dual-loop](#7b-mode-faceless--scheduler-dual-loop).

```
Creator_Kamu (topik)
      │
      ▼
Vito ──brief──▶ Clara ──naskah──▶ Damon ──SCRIPT REJECT──▶ Clara (revisi)
                                    │
                                    │ SCRIPT PASS
                                    ▼
                                  Bima ──tool──▶ Creator mengunggah a_roll_raw.mp4
                                    │
                                    ▼
                                  Raka ──render──▶ Damon ──VIDEO FAIL──▶ Raka
                                                     │
                                                     │ VIDEO PASS
                                                     ▼
                                                   Sora ──upload──▶ PIPELINE_COMPLETE
```

---

## 1. Mulai cepat

```bash
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# (opsional) konfigurasi
cp .env.example .env      # isi STUDIO_API_KEY

# 1) Coba seluruh alur TANPA API key dan TANPA biaya (rehearsal deterministik)
python virtual_office_superbots.py --rehearsal -t "Cara membangun tim AI Agent"

# 2) Jalankan rapat otonom sungguhan
python virtual_office_superbots.py -t "Workflow editing video 2026"

# 3) Mode faceless: tanpa rekaman Creator, narasi diisi TTS
python virtual_office_superbots.py -t "Kenapa startup AI butuh content engine" --faceless

# Tanpa argumen, topik ditanya interaktif seperti skrip asli
python virtual_office_superbots.py
```

Perintah lain:

```bash
python virtual_office_superbots.py --doctor          # cek API key, persona, ffmpeg, Whisper, memori
python virtual_office_superbots.py --mock-creator    # file A-Roll dummy otomatis (CI/demo)
python virtual_office_superbots.py --real-render     # aktifkan ffmpeg asli
python virtual_office_superbots.py --status          # antrean daemon + isi memori

# mode 24/7 (tanpa terminal, tanpa input())
python virtual_office_superbots.py --daemon          # scheduler + file watcher + notifikasi
python virtual_office_superbots.py --serve           # bridge HTTP + dashboard + webhook Telegram (port 8100)

# dashboard visual + Telegram dua arah + Shorts
python virtual_office_superbots.py --serve           # buka http://127.0.0.1:8100/ (§7e)
python virtual_office_superbots.py --set-webhook https://xxxx.ngrok-free.app/kra/telegram-webhook
python virtual_office_superbots.py --shorts          # potong Shorts 9:16 dari final_render.mp4
```

Keluaran ada di `workspace/`, `input_creator/`, `output_ready/`:

| Berkas | Isi |
|---|---|
| `workspace/content_brief.json` | brief Vito (judul, thumbnail, angle) |
| `workspace/script_production.md` | naskah Clara, ditandai `[CREATOR A-ROLL]` / `[AI B-ROLL]` |
| `workspace/script_production.json` | scene breakdown mesin (dibaca Remotion + concat) |
| `workspace/props_*.json` | props per scene yang dikirim ke Remotion (bisa diperiksa manual) |
| `workspace/creator_todo_list.md` | instruksi rekaman dari Bima ke Anda |
| `workspace/run_transcript.md` | transkrip rapat + daftar berkas |
| `workspace/studio_log.jsonl` | log audit setiap tool |
| `output_ready/final_render.mp4` | hasil render Raka |
| `output_ready/final_render.render.json` | *side-car* bukti render (engine, hash, probe) |
| `output_ready/youtube_publish_receipt.json` | receipt upload (live atau simulasi) |
| `workspace/pipeline_state.json` | status pipeline untuk gate `n100n` (§7b) |
| `input_creator/auto_bg_*.jpg` | gambar latar AI otomatis per scene (§7c) |
| `input_creator/auto_broll_*.mp4` | stock video B-Roll otomatis per scene (§7c) |
| `output_ready/thumbnails/thumb_1..3.jpg` | thumbnail YouTube 1280×720 (§7d) |
| `workspace/vito_realtime_radar.json` | arsip radar tren real-time Vito (§7d) |
| `workspace/chat_logs.json` | 100 pesan terakhir GroupChat untuk dashboard (§7e) |
| `workspace/thumbnail_choice.json` | variasi thumbnail pilihan Creator (`/thumb 2`) (§7e) |
| `output_ready/shorts/short_<epoch>.mp4` | YouTube Shorts 9:16 hasil auto-cutter (§7e) |

---

## 2. Tim & pembagian tool

Setiap bot memegang **hanya** tool sesuai perannya. Tool yang sama tidak dibagi dua bot
(lihat perbaikan #3).

| Bot | Peran | Tool | Menyerahkan ke |
|---|---|---|---|
| `Creator_Kamu` | Anda (manusia) | — | Vito |
| `Vito_TrendStrategist` | riset tren **nyata**, judul, thumbnail | `scan_viral_trends_and_youtube_outliers`, `research_current_trends`, `get_recent_content_memory`, `check_topic_similarity_in_memory`, `save_content_brief` | Clara |
| `Clara_Scriptwriter` | naskah retensi tinggi, A-Roll/B-Roll | `save_production_script`, `save_scene_breakdown` | Damon |
| `Damon_RealityChecker` | QC + hak veto (2 gerbang) | `inspect_rendered_output`, `check_topic_similarity_in_memory`, `register_new_episode_to_memory` | Clara / Bima / Raka / Sora |
| `Bima_StudioPM` | gatekeeper human-in-the-loop | `request_and_check_creator_files`, `send_direct_alert_to_creator` | Raka |
| `Raka_Producer` | render ffmpeg 1080p60 / Remotion per scene | `assemble_and_render_video`, `assemble_story_from_script` | Damon |
| `Sora_YouTubePublisher` | SEO + upload YouTube | `upload_to_youtube`, `send_direct_alert_to_creator` | — (menutup rapat) |

Persona tiap bot adalah file Markdown di [`agents/`](agents) (gaya
`msitarzewski/agency-agents`, lengkap dengan frontmatter YAML). Ubah nada/kriteria bot
cukup dengan mengedit `.md`-nya — tanpa menyentuh Python.

---

## 3. Perbaikan penting

### Yang membuat skrip asli tidak berjalan

| # | Masalah pada versi artikel | Akibat | Perbaikan di repo ini |
|---|---|---|---|
| 1 | `register_function(..., caller=bot, executor=tool_executor)` lalu `tool_executor` **ikut masuk GroupChat** | Executor tidak pernah benar-benar bicara → graf transisi punya simpul mati; `func_call_filter` memilih executor, lalu LLM memilih pembicara berikutnya secara acak | Tiap bot **mengeksekusi tool-nya sendiri** (`caller == executor`); `StudioTools` tetap satu objek bersama. Executor tidak lagi jadi peserta |
| 2 | `register_function` dipanggil dengan **bound method** | Langsung `AttributeError: 'method' object has no attribute '_name'` saat start | Method dibungkus `functools.wraps` agar AutoGen bisa menempelkan `_name`/`_description` |
| 3 | `save_document` dipakai Vito **dan** Clara | `func_call_filter` menemukan 2 kandidat → tool call Vito dieksekusi oleh Clara, atribusi kacau | Tool dipisah per peran: `save_content_brief` & `save_production_script` |
| 4 | Pesan pembuka berisi string `PIPELINE_COMPLETE` | `_is_termination_msg` langsung `True` → rapat selesai di putaran 1, tidak ada bot yang bicara | Kata kunci dihapus dari pesan kickoff; hanya bot yang boleh menuliskannya |
| 5 | Setelah tool dieksekusi, giliran lompat ke bot berikutnya | Bot pemilik tool tak pernah membacakan hasilnya; pembicaraan terasa "tiba-tiba" | `continuation_selector`: pesan ber-role `tool` selalu dikembalikan ke pemilik tool, sisanya `auto` |
| 6 | `human_input_mode` default `TERMINATE` | Bot berhenti menunggu input manusia di tengah rapat | Semua bot `human_input_mode="NEVER"`; satu-satunya interaksi manusia ada di tool Bima |
| 7 | `input()` di dalam tool tanpa penanganan non-TTY/timeout | Crash `EOFError` di CI, atau menggantung selamanya | Ada `STUDIO_CREATOR_TIMEOUT`, `--mock-creator`, dan status `STATUS BLOCKED` yang jelas |
| 8 | Laporan render di-*hardcode* ("1080p, -14 LUFS, 3 thumbnail") | Damon "mengaudit" klaim palsu | Laporan nyata: engine (`ffmpeg`/`simulation`), ukuran, SHA-256, probe `ffprobe`, side-car JSON. Simulasi ditandai eksplisit |
| 9 | Nama file dari LLM langsung dipakai | Path traversal (`../../.ssh/...`) | Semua path divalidasi di dalam folder studio |
| 10 | Tidak ada jejak audit | Sulit tahu bot mengarang atau tidak | `run_transcript.md`, `run_messages.json`, `studio_log.jsonl` |
| 11 | API key *fallback* berupa string di dalam kode | Rawan bocor | Hanya dari environment / `.env` |
| 12 | Upload YouTube tanpa cache token | OAuth browser diminta setiap kali | `token.json` dipakai ulang, refresh otomatis |

### Perbaikan tambahan v2.0 (mode 24/7)

| # | Masalah | Perbaikan |
|---|---|---|
| 13 | Tool Bima memblokir `input()` → daemon mustahil | `STUDIO_ASYNC_GATE`: tool mengembalikan `STATUS AWAITING`, checklist dikirim via Telegram, rapat ditutup dengan `AWAITING_CREATOR_UPLOAD` |
| 14 | `Daemon.stop()` menggantung saat Ctrl-C | Shutdown kooperatif (event + sentinel antrean) dengan `asyncio.wait` berbatas waktu, `cancel()` hanya sebagai jaring terakhir |
| 15 | Webhook menerima nama file yang belum ada lalu seolah sukses | `409` bila file belum ada/0 byte; nama disanitasi dengan `Path(...).name` |
| 16 | Render mengklaim sukses padahal ada langkah yang tak jalan | Setiap langkah `ok`/`skipped`/`failed` di sidecar; mode produksi menolak artefak simulasi |
| 17 | Bot mengarang tren dari ingatan LLM | `research_current_trends` memanggil API nyata; tanpa kredensial → `RISET TIDAK TERSEDIA` dan dilarang mengarang angka |
| 18 | Rekaman yang masuk saat bot mati tertinggal | `_resume_pending()` saat start: rapat `AWAITING_CREATOR_UPLOAD` + file tersedia → job produksi otomatis |

### Yang dipertahankan dari desain asli

- `GroupChat` + `GroupChatManager` dengan `allowed_or_disallowed_speaker_transitions`.
- Damon punya jalur balik ke Clara dan Raka → **maker–checker loop** yang nyata.
- Bima menghentikan pipeline untuk menunggu file dari manusia (human-in-the-loop).
- Tiap bot terikat tool sesuai bidangnya, bukan sekadar chatbot.

---

## 4. Konfigurasi

Semua lewat environment (atau `.env`, dimuat otomatis):

| Variabel | Default | Fungsi |
|---|---|---|
| **LLM** | | |
| `STUDIO_API_KEY` | — | API key LLM (fallback: `OPENAI_API_KEY`) |
| `STUDIO_BASE_URL` | OpenAI | Endpoint kompatibel OpenAI (Gemini, OpenRouter, Groq, LM Studio) |
| `STUDIO_MODEL` | `gpt-4o` | Nama model |
| `STUDIO_TEMPERATURE` | `0.4` | Kreativitas |
| `STUDIO_MAX_ROUNDS` | `40` | Batas putaran group chat |
| **Mode tool** | | |
| `STUDIO_MOCK_CREATOR` | `0` | `1` = file A-Roll dummy otomatis |
| `STUDIO_REAL_RENDER` | `0` | `1` = jalankan pipeline media asli (butuh ffmpeg) |
| `STUDIO_ASYNC_GATE` | `0` | `1` = tool Bima tidak pernah menunggu `input()` (otomatis `1` di `--daemon`) |
| `STUDIO_CREATOR_TIMEOUT` | `0` | Batas tunggu upload Creator (detik, `0` = tanpa batas) |
| **Media** | | |
| `STUDIO_RENDER_FPS` | `60` | FPS output (resolusi selalu 1920×1080) |
| `STUDIO_RENDER_CRF` | `20` | Kualitas x264 (kecil = lebih bagus) |
| `STUDIO_WHISPER_MODEL` | `base` | Model Whisper: `tiny`/`base`/`small`/`medium`/`large` |
| `STUDIO_REMOTION_DIR` | — | Folder repo Remotion KRA untuk intro/B-roll |
| `STUDIO_REMOTION_COMPOSITION` | — | Nama composition default |
| `STUDIO_BGM` | — | Musik latar (dicampur −24 dB) |
| **Suara narasi (faceless)** | | |
| `STUDIO_TTS_PROVIDER` | `auto` | `auto`/`elevenlabs`/`edge`/`off` |
| `STUDIO_ELEVENLABS_API_KEY` | — | Kunci ElevenLabs (fallback: `ELEVENLABS_API_KEY`) |
| `STUDIO_ELEVENLABS_VOICE_ID` | `pNInz6obpgDQGcFmaJgB` | Voice ElevenLabs |
| `STUDIO_EDGE_TTS_VOICE` | `id-ID-ArdiNeural` | Suara Edge-TTS (`id-ID-GadisNeural` untuk wanita) |
| `STUDIO_TTS_RATE` | `+5%` | Kecepatan bicara TTS |
| `STUDIO_REQUIRE_AROLL` | `1` | `0` = faceless: jangan tunggu rekaman Creator |
| **Notifikasi Telegram** | | |
| `STUDIO_TELEGRAM_TOKEN` | — | Token bot (alias: `TELEGRAM_BOT_TOKEN`) |
| `STUDIO_TELEGRAM_CHAT_ID` | — | Chat ID Anda (alias: `TELEGRAM_CHAT_ID`) |
| **Visual otomatis (B-Roll)** | | |
| `STUDIO_BROLL_PROVIDER` | `auto` | `auto`/`dalle`/`flux`/`pexels`/`gradient`/`off` |
| `STUDIO_IMAGE_API_KEY` | — | Kunci gambar OpenAI (fallback: `OPENAI_API_KEY`); kosong = Flux gratis |
| `PEXELS_API_KEY` | — | Stock video gratis dari pexels.com/api |
| `STUDIO_THUMBNAILS` | `1` | `0` = lewati thumbnail YouTube |
| `YOUTUBE_API_KEY` | — | Alias `STUDIO_YOUTUBE_API_KEY`, dipakai radar outlier Vito |
| **Riset tren (Vito)** | | |
| `STUDIO_YOUTUBE_API_KEY` | — | YouTube Data API v3 (API key saja, tanpa OAuth) |
| `TAVILY_API_KEY` | — | Fallback tren web |
| `SERPAPI_API_KEY` | — | Fallback pencarian YouTube |
| **Daemon** | | |
| `STUDIO_SCHEDULE` | — | Jadwal ideasi, mis. `07:00,16:00` (jam lokal) |
| `STUDIO_SCHEDULE_TOPIC` | — | Topik tetap untuk jadwal (kosong = Vito memilih dari memori) |
| `STUDIO_WATCH_INTERVAL` | `5` | Interval polling `input_creator/` (detik) |
| `STUDIO_UPLOAD_SETTLE` | `4` | File dianggap selesai bila umurnya > N detik |
| `STUDIO_DB` | `workspace/studio.db` | Lokasi database memori |
| `STUDIO_NOTIFY` | `auto` | `auto`/`telegram`/`console`/`off` |
| `STUDIO_TELEGRAM_TOKEN` | — | Token bot dari @BotFather |
| `STUDIO_TELEGRAM_CHAT_ID` | — | Chat tujuan notifikasi & perintah masuk |
| **Upload** | | |
| `STUDIO_YOUTUBE_CATEGORY` | `22` | Kategori YouTube (22 = People & Blogs) |

Contoh memakai Gemini (endpoint kompatibel OpenAI):

```env
STUDIO_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
STUDIO_MODEL=gemini-2.0-flash
STUDIO_API_KEY=...
```

> Model harus mendukung **function calling** dengan baik. Model kecil sering lupa
> memanggil tool atau menulis naskah di chat tanpa menyimpannya — cek
> `workspace/studio_log.jsonl` untuk memastikan tool benar-benar dipanggil.

---

## 5. Mesin media (render sungguhan)

Tanpa `--real-render`, Raka menulis artefak **simulasi** (file dummy + laporan
`engine: "simulation"`) supaya alur bisa diuji tanpa video asli. Dengan
`--real-render`/`STUDIO_REAL_RENDER=1`, Raka menjalankan pipeline enam langkah yang
sama dengan engine KRA:

| # | Langkah | Perintah | Butuh |
|---|---|---|---|
| 1 | `normalize_audio` | `ffmpeg -af loudnorm=I=-14:TP=-1.5:LRA=11` | ffmpeg |
| 2 | `cut_dead_air` | `auto-editor --edit audio:threshold=0.04 --margin 0.15sec,0.25sec` | auto-editor |
| 3 | `whisper` | `model.transcribe(language="id", word_timestamps=True)` → `.srt` + `.json` | openai-whisper |
| 4 | `remotion` | `npx remotion render <composition> --props=...` (intro/hook) | Node + repo Remotion |
| 5 | `concat` | normalisasi tiap segmen ke 1080p60 (scale/pad/setsar/fps) lalu `concat` | ffmpeg |
| 6 | `finalize` | burn-in subtitle + BGM `-24dB` → `ffprobe` | ffmpeg (+ffprobe) |

Setiap langkah dicatat sebagai `ok` / `skipped` / `failed` **beserta alasannya** di
`output_ready/final_render.render.json`:

```json
{
  "engine": "ffmpeg",
  "steps": [
    {"name": "normalize_audio", "status": "ok", "seconds": 12.4},
    {"name": "cut_dead_air", "status": "skipped", "detail": "auto-editor tidak terpasang"},
    {"name": "whisper", "status": "failed", "detail": "ModuleNotFoundError: No module named 'whisper'"}
  ],
  "failed_steps": ["whisper"],
  "specs": {"has_video": true, "duration_s": 183.4, "width": 1920, "height": 1080, "fps": "60/1"}
}
```

Prinsipnya: **langkah yang tidak bisa dijalankan dilewati dan dilaporkan**, tidak
pernah diklaim berhasil. Bila `ffmpeg` sendiri tidak ada, tool mengembalikan
`ERROR RENDER ...` (bukan file palsu), dan bila mode produksi aktif tetapi hasilnya
masih artefak simulasi, Damon menolak dengan `AUDIT GAGAL`.

Instalasi binary:

```bash
sudo apt install ffmpeg            # atau: brew install ffmpeg
pip install -r requirements-media.txt   # auto-editor + openai-whisper
```

Damon mengaudit hasil lewat `inspect_rendered_output`: file ada, bukan 0 byte,
header MP4 valid, side-car ada, SHA-256 cocok, dan resolusi ≥ 1080p (dari `ffprobe`).

---

### 5b. Naskah JSON → Remotion → concat (jembatan seragam)

Naskah Markdown enak dibaca manusia, tetapi Remotion butuh **data**. Karena itu Clara
menyimpan dua artefak dari materi yang sama:

| Artefak | Untuk siapa |
|---|---|
| `workspace/script_production.md` | Creator (merekam) & Damon (audit naratif) |
| `workspace/script_production.json` | mesin: Remotion + modul concat |

```json
{
  "project_id": "story_kra_001",
  "title": "Rahasia di Balik Kantor AI Otonom",
  "fps": 30,
  "scenes": [
    {
      "scene_id": "scene_1_hook",
      "type": "REMOTION_DYNAMIC",
      "composition_id": "StoryScene",
      "narration_text": "Pernahkah kamu membayangkan kantor yang berjalan sendiri saat kamu tidur?",
      "visual_cue": "Dark futuristic server room with glowing neon nodes",
      "broll_asset": "broll_server.mp4",
      "visual_mode": "AI_IMAGE",
      "highlight_words": ["berjalan sendiri", "tidur"]
    },
    {
      "scene_id": "scene_2_aroll",
      "type": "CREATOR_AROLL",
      "composition_id": "CreatorOverlay",
      "source_video": "a_roll_raw.mp4",
      "lower_third_text": "Rama - Founder KRA",
      "narration_text": "Ini yang terjadi tiap pagi di kantor kami."
    }
  ]
}
```

Tool Clara memvalidasi skema ini (`studio/story.py::validate_story`) dan membalas
`ERROR SCHEMA`/`ERROR JSON` bila ada yang kurang — bukan menyimpan diam-diam.

Raka lalu memanggil:

```python
assemble_story_from_script("script_production.json", "final_render.mp4")
```

Yang terjadi per scene (semua tercatat di `.render.json`):

| Tahap | Detail |
|---|---|
| Sumber suara | `CREATOR_AROLL` + file ada → audio/rekaman Creator; selain itu → TTS (`audioSrc`) |
| Whisper | `word_timestamps=True` → `[{word, start, end}]`; `durationInSeconds = max(end) + 0.35` |
| Stage aset | video, gambar latar, dan audio TTS disalin ke `remotion_studio/public/kra_temp/` — Chromium Remotion menolak `file://`, ini yang membuat `staticFile()` aman |
| Render | `npx remotion render <composition_id> out.mp4 --props=workspace/props_<nn>_<scene>.json` |
| Concat | `npx ffmpeg-concat -t directionalWarp` (via `xvfb-run` bila headless), fallback demuxer ffmpeg dengan `-c copy` |
| Bersih | `public/kra_temp/` dikosongkan setelah selesai |

Dua masalah klasik ikut diselesaikan:

* **blokir `file://`** — aset selalu lewat `public/kra_temp/` + `staticFile()`;
* **durasi statis** — `calculateMetadata` di `Root.tsx` mengubah `durationInSeconds`
  (hasil Whisper) menjadi `durationInFrames`, jadi scene sepanjang apa pun audionya
  tidak terpotong atau membeku di ujung.

Template komponennya ada di [`remotion_template/`](remotion_template) —
`Root.tsx` (composition `StoryScene` **dan** `CreatorOverlay`) dan `StoryScene.tsx`
(caption per kata, highlight dari Clara, lower third, tahan kata terakhir 0.25 s agar
caption tidak berkedip). Komponen itu menerima **salah satu** `videoSrc` (A-Roll/B-Roll)
**atau** `audioSrc` (narasi TTS, video otomatis di-*mute*) plus `bgImageSrc` opsional
dengan Ken Burns. Salin ke `remotion_studio/src/`, lalu set
`STUDIO_REMOTION_DIR`.

Bila Node/Remotion belum siap, setiap scene tetap dirender dengan FFmpeg (conform
1080p, audio Creator/TTS dipetakan langsung, burn-in SRT bila kata Whisper tersedia)
dan laporan menyebut `engine: "ffmpeg-fallback"` — Raka wajib mengatakannya di ruang
rapat, bukan mengaku sudah memakai Remotion.

---

## 6. Daemon 24/7

```bash
python virtual_office_superbots.py --daemon
```

Tiga loop berjalan bersamaan di satu event loop:

| Loop | Tugas |
|---|---|
| **worker** | menjalankan satu rapat pada satu waktu (AutoGen + render itu berat) |
| **watcher** | memantau `input_creator/`; file video baru → job produksi otomatis masuk antrean |
| **scheduler** | memicu fase ideasi sesuai `STUDIO_SCHEDULE` (mis. `07:00`) |
| **telegram** | membaca perintah masuk: `/status`, `/topic <ide>`, `/produce`, `/help` |

Alur nyata tanpa Anda menyentuh terminal:

1. 07:00 — Vito bangun, ambil tren YouTube hari ini, brief + naskah + QC selesai.
2. Bima mengirim **checklist rekaman** ke Telegram dan rapat ditutup dengan status
   `AWAITING_CREATOR_UPLOAD` (bukan menggantung menunggu `Enter`).
3. Anda merekam, lalu menaruh file di `input_creator/`.
4. Watcher mendeteksi file (menunggu `STUDIO_UPLOAD_SETTLE` detik agar copy selesai),
   mengantrekan fase produksi, Raka render, Damon audit, Sora upload, lalu notifikasi
   berisi URL video.

Job yang gagal tidak menghentikan daemon: statusnya `failed`, alasannya dikirim ke
notifikasi, dan rapat berikutnya tetap jalan. Setelah restart, daemon memeriksa
kembali file yang sudah ada untuk rapat berstatus `AWAITING_CREATOR_UPLOAD`
(`_resume_pending`) — jadi rekaman yang masuk saat bot mati tidak hilang.

Menjalankan permanen (systemd, Linux):

```ini
# /etc/systemd/system/kantor-rama.service
[Unit]
Description=Kantor Rama AI — studio daemon
After=network-online.target

[Service]
WorkingDirectory=/opt/kantor-rama-ai
EnvironmentFile=/opt/kantor-rama-ai/.env
ExecStart=/opt/kantor-rama-ai/.venv/bin/python virtual_office_superbots.py --daemon
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now kantor-rama
journalctl -u kantor-rama -f
```

macOS: `launchd` dengan `KeepAlive`. Windows: Task Scheduler ("At startup") atau
`nssm`. Untuk Telegram, buat bot lewat @BotFather, kirim `/start` ke bot, lalu ambil
`chat.id` Anda dari `https://api.telegram.org/bot<TOKEN>/getUpdates`.

---

## 7. Integrasi KRA & n100n

Studio ini bukan pulau terpisah: ia memakai struktur folder `kantor-rama-ai`.

```env
STUDIO_ROOT=/path/ke/kantor-rama-ai
STUDIO_REMOTION_DIR=/path/ke/kantor-rama-ai/remotion_studio
```

```
kantor-rama-ai/
├── input_creator/      # Anda menaruh a_roll_raw.mp4 di sini
├── workspace/          # brief, naskah, transkrip, studio.db
├── output_ready/       # final_render.mp4 + sidecar
└── remotion_studio/    # repo Remotion (intro/B-roll)
```

Bridge HTTP untuk scheduler lokal `n100n`:

```bash
python virtual_office_superbots.py --serve --host 0.0.0.0 --port 8100
```

| Endpoint | Fungsi |
|---|---|
| `POST /kra/n100n-trigger` | fase `IDEATION_SCRIPT` atau `PRODUCE_AND_UPLOAD` |
| `POST /kra/schedule/1-start-script` | **loop 1** (08:00): rapat naskah Vito → Clara → Damon → Bima |
| `GET /kra/schedule/2-check-gate` | **loop 2** (tiap 30 menit): sudah boleh render? |
| `POST /kra/schedule/3-produce-and-upload` | Raka render → Damon QC → Sora upload |
| `GET /health` | hidup + kapabilitas media (`ffmpeg`, `whisper`, `remotion`) |
| `GET /status` | antrean, job terakhir, isi memori, status pipeline |
| `GET /jobs/{id}` | detail satu job |
| `POST /kra/schedule/sync-views` | cron Minggu 00:00: perbarui `views_count` dari YouTube (§7e) |
| `GET /` • `/dashboard` | **KRA Control Center**, 3 panel real-time (§7e) |
| `GET /api/state` • `/api/logs` • `/api/history` • `/api/thumbnails` | data JSON untuk dashboard (§7e) |
| `POST /api/thumbnail/select?index=N` | pilih variasi thumbnail A/B (§7e) |
| `POST /kra/telegram-webhook` | Telegram dua arah: `/status` `/riwayat` `/thumb` `/approve` (§7e) |

Payload & contoh (sama dengan `kra_raka_engine.py` milik KRA):

```bash
# pagi: ideasi + naskah
curl -s -X POST http://127.0.0.1:8100/kra/n100n-trigger \
  -H 'Content-Type: application/json' \
  -d '{"phase": "IDEATION_SCRIPT", "topic_or_prompt": "Tren AI minggu ini"}'

# sore: rekaman sudah ada → produksi + upload
curl -s -X POST http://127.0.0.1:8100/kra/n100n-trigger \
  -H 'Content-Type: application/json' \
  -d '{"phase": "PRODUCE_AND_UPLOAD", "raw_video_filename": "a_roll_raw.mp4",
       "remotion_composition": "Intro"}'
```

Respons `202` berisi `{"status": "queued", "phase": ..., "job": {...}}`. Bila file
rekaman belum ada, endpoint menjawab `409` — `n100n` tahu harus menunggu, bukan
menganggap render berhasil. Nama file disanitasi (`Path(...).name`), jadi
`../../etc/passwd` tidak bisa dipakai.

Contoh pemanggilan dari `n100n` (cron):

```cron
0 7  * * *  curl -s -X POST http://127.0.0.1:8100/kra/n100n-trigger -H 'Content-Type: application/json' -d '{"phase":"IDEATION_SCRIPT"}'
```

> `PRODUCE_AND_UPLOAD` **tidak wajib** dipanggil cron: watcher `input_creator/`
> sudah otomatis memicunya begitu rekaman muncul. Panggil endpoint itu bila `n100n`
> ingin memaksa render ulang.

Catatan soal `kra_raka_engine.py` yang Anda kirim: alurnya (normalize → auto-editor →
whisper → remotion → concat/burn → ffprobe) sudah diadopsi di `studio/media.py`, tetapi
beberapa detailnya diperbaiki — nama fungsi `step_6_ interrogate_final_specs` adalah
syntax error, `whisper.utils.get_writer(...)` berubah antar versi (di sini SRT ditulis
sendiri dari `segments`), dan argumen `--margin 0.15sec,0.25sec` serta `volume=-24dB`
tergantung versi binary. Engine baru juga menandai setiap langkah yang dilewati/gagal,
sesuatu yang tidak dilakukan skrip asli.

---

## 7b. Mode faceless & scheduler dual-loop

Pipeline bisa berjalan **tanpa satu pun sentuhan manusia**: Clara menulis naskah, mesin
TTS mengisi suara, Whisper menyelaraskan caption, Remotion merakit, Sora mengunggah.

```bash
# sekali jalan tanpa rekaman Creator
python virtual_office_superbots.py -t "Kenapa startup AI butuh content engine" --faceless
```

atau lewat `.env`: `STUDIO_REQUIRE_AROLL=0`. Bila `--faceless` aktif, Bima tidak pernah
menagih file, dan status pipeline langsung `READY_FOR_RENDER`.

### Mesin TTS (`studio/tts.py`)

| Urutan | Engine | Syarat | Catatan |
|---|---|---|---|
| 1 | **ElevenLabs** | `STUDIO_ELEVENLABS_API_KEY` | `eleven_multilingual_v2`, HTTP pakai `urllib` (tanpa SDK) |
| 2 | **Edge-TTS** | `pip install edge-tts` | gratis, `id-ID-ArdiNeural` / `id-ID-GadisNeural` |
| — | gagal semua | — | `TTSError` → scene ditandai `audio_provider: ""`, dilaporkan, tidak dipalsukan |

Setelah tersedia, audio **dinormalisasi** dengan FFmpeg
`silenceremove + loudnorm=I=-14:TP=-1.5:LRA=11` → 48 kHz stereo 192 kbps sebelum masuk
Whisper/Remotion.

```env
STUDIO_TTS_PROVIDER=auto            # auto | elevenlabs | edge | off
STUDIO_ELEVENLABS_API_KEY=xi-...
STUDIO_ELEVENLABS_VOICE_ID=pNInz6obpgDQGcFmaJgB
STUDIO_EDGE_TTS_VOICE=id-ID-ArdiNeural
STUDIO_TTS_RATE=+5%
```

Per scene, `StoryBuilder` memilih sumber suara:

1. `type: "CREATOR_AROLL"` **dan** file ada → pakai rekaman Creator (audio asli).
2. Selain itu → narasi TTS dari `narration_text` → Whisper → props `audioSrc`.
3. `audioSrc` terisi ⟹ `StoryScene` memutar `<Audio>` dan mem-*mute* video latar,
   caption per kata tetap jalan, latar tanpa video memakai `bgImageSrc` + Ken Burns
   (atau gradien gelap bila gambar juga tidak ada).

### Status pipeline (`workspace/pipeline_state.json`)

```
IDLE → SCRIPTING_IN_PROGRESS → WAITING_FOR_CREATOR → RENDERING → PUBLISHED_COMPLETE
                    ↘ READY_FOR_RENDER ↗                    ↘ ERROR
```

Ditulis **atomik** (`os.replace`), jadi `n100n` tidak pernah membaca JSON setengah jadi.
`GET /kra/schedule/2-check-gate` menjawab satu pertanyaan saja:

```json
{"pipeline_status": "WAITING_FOR_CREATOR", "creator_file_detected": true,
 "ready_to_produce": true, "expected_file": "a_roll_raw.mp4",
 "reason": "rekaman Creator sudah masuk", "updated_at": "2026-11-07T08:34:12+07:00"}
```

`ready_to_produce` bernilai `true` hanya jika `WAITING_FOR_CREATOR` + file ada (bukan 0
byte) **atau** status `READY_FOR_RENDER` (faceless). Status `SCRIPTING_IN_PROGRESS`,
`RENDERING`, `PUBLISHED_COMPLETE`, dan `ERROR` selalu `false` — render tidak pernah
tumpang tindih, dan file yang sudah dirender dipindahkan ke
`input_creator/processed/` supaya tidak diproses dua kali.

### Workflow n8n

Import `schedules/kra_autonomous_workflow.json` (n8n → *Workflows* → *Import from
File*), timezone `Asia/Jakarta`:

| Node | Pemicu | Aksi |
|---|---|---|
| 08:00 AM - Jadwal Rapat Script | cron `0 8 * * *` | `POST /kra/schedule/1-start-script` |
| Setiap 30 Menit - Cek Folder Creator | interval 30 menit | `GET /kra/schedule/2-check-gate` |
| Apakah File Creator Siap? | IF `ready_to_produce` | true → produksi, false → tunggu |
| Eksekusi Raka & Sora | cabang true | `POST /kra/schedule/3-produce-and-upload` |

Server tetap hidup lewat pm2 (setara `uvicorn kra_n100n_server:app`, di repo ini
aplikasinya adalah `studio/api.py`):

```bash
pm2 start "/opt/kantor-rama-ai/.venv/bin/python virtual_office_superbots.py --serve --port 8100" \
    --name "kra-superbots-api" --cwd /opt/kantor-rama-ai
pm2 save && pm2 startup
pm2 logs kra-superbots-api
```

`--serve` menjalankan daemon penuh (worker + watcher + scheduler + bridge), jadi satu
proses menangani webhook sekaligus memantau folder `input_creator/`.

**Contoh siklus sehari**

1. 08:00 — loop 1: Vito riset tren, Clara menulis `script_production.json`, Damon audit,
   Bima menutup rapat. Status: `WAITING_FOR_CREATOR`.
2. 08:30 — loop 2: file belum ada → `ready_to_produce: false`, tidak ada yang terjadi.
3. 11:07 — Anda menaruh `a_roll_raw.mp4`; karena `--serve` juga menjalankan watcher,
   produksi bisa terpicu tanpa n8n sama sekali.
4. 11:30 — loop 2: `ready_to_produce: true` → Raka render (scene A-Roll + scene TTS),
   concat, Damon QC, Sora upload. Status: `PUBLISHED_COMPLETE`, file mentah diarsipkan.

---

## 7c. Notifikasi Telegram & visual otomatis

### Telegram: laporan naskah + link YouTube

Cukup isi dua variabel (nama KRA juga diterima), lalu semua kabar datang ke HP:

```bash
export STUDIO_TELEGRAM_TOKEN="7123456789:AAH..."   # atau TELEGRAM_BOT_TOKEN
export STUDIO_TELEGRAM_CHAT_ID="987654321"          # atau TELEGRAM_CHAT_ID
```

| Momen | Yang dikirim |
|---|---|
| Naskah lulus Damon | judul, jumlah scene, mode produksi (menunggu A-Roll / full auto) — plus **lampiran** `script_production.json` dan `creator_todo_list.md` |
| Mulai render | satu baris: Raka sedang TTS → Whisper → Remotion → concat |
| Selesai upload | judul, privasi, engine, **link `youtu.be`** dan **link YouTube Studio** |

Implementasinya di `studio/notify.py` (`notify_script_ready`, `notify_youtube_uploaded`,
`send_html`, `send_document`) dan dipanggil otomatis oleh daemon. Pengiriman file memakai
multipart `urllib`, jadi **tidak menambah dependensi** (`requests` tidak dibutuhkan).

Bima dan Sora juga memegang tool `send_direct_alert_to_creator(bot_sender_name, message_text)`
untuk pesan ad-hoc ke Telegram Anda; isinya di-escape HTML supaya aman.

> Kejujuran status: receipt simulasi (`UPLOADED_SIMULATION`, tanpa `video_id`) dikirim
> sebagai **"UPLOAD SIMULASI (BUKAN LIVE)"** — bukan "video berhasil tayang".

### Visual otomatis: 3 tier + fallback

Scene `REMOTION_DYNAMIC` yang tidak punya `broll_asset`/`bg_image` manual tidak lagi
dibiarkan gelap. `studio/broll.py` (`ensure_visual`) mencoba berurutan:

| Tier | Engine | Syarat | Hasil |
|---|---|---|---|
| 1 | **DALL·E 3** | `STUDIO_IMAGE_API_KEY`/`OPENAI_API_KEY` | `auto_bg_<scene>.jpg` (1792×1024) |
| 2 | **Flux Pollinations** | tanpa key, tanpa biaya | `auto_bg_<scene>.jpg` (1920×1080) |
| 3 | **Pexels stock video** | `visual_mode: "STOCK_VIDEO"` + `PEXELS_API_KEY` | `auto_broll_<scene>.mp4` (HD 1920) |
| 4 | **Kanvas gelap FFmpeg** | ffmpeg ada, jaringan mati | `auto_bg_<scene>.jpg` |

Aturan mainnya:

- hanya untuk scene non-`CREATOR_AROLL` yang **belum** punya aset manual (aset manual
  selalu menang — tidak ada API yang dipanggil);
- file hasilnya di-cache di `input_creator/`, jadi render ulang tidak memanggil API lagi;
- kegagalan **tidak** menghentikan render: catatan seperti `flux: GAGAL (HTTP 503)`
  masuk ke `scenes[].notes`, dan scene memakai latar gradien Remotion;
- nama file disanitasi dari `scene_id`, jadi `../../etc/passwd` tidak bisa keluar folder.

```json
{
  "scene_id": "scene_02_data",
  "type": "REMOTION_DYNAMIC",
  "narration_text": "Pusat data mereka memproses jutaan keputusan per detik...",
  "visual_cue": "server room data center blinking lights",
  "visual_mode": "STOCK_VIDEO",
  "highlight_words": ["jutaan keputusan"]
}
```

`visual_mode` divalidasi saat menyimpan naskah (`AI_IMAGE` default), begitu juga
`visual_cue` yang kini diperlakukan sebagai **prompt visual** — lihat aturan di persona
Clara (§2).

---

## 7d. Thumbnail YouTube & radar tren real-time

### Thumbnail 1280×720 (`studio/thumbnail.py`)

Raka membuat thumbnail **setelah** concat, dari data Vito — bukan dari naskah:

```json
{
  "thumbnail_badge": "FAKTA BARU",
  "thumbnail_hooks": ["ROBOT BEKERJA MALAM", "NOL MANUSIA", "100% OTOMATIS"],
  "thumbnail_visual_prompt": "dark automated factory, robotic arms, blue laser light"
}
```

Alur:

1. latar AI lewat `studio/broll.py` (DALL·E → Flux → kanvas FFmpeg) → `auto_bg_thumb_base.jpg`;
2. latar disalin ke `remotion_studio/public/kra_temp/`, lalu
   `npx --yes remotion still YouTubeThumbnail out.jpg --props=... --image-format=jpeg --quality=90`;
3. tiga variasi warna (`#FFE600`, `#00F5D4`, `#FF2A54`) ditulis ke
   `workspace/thumb_props_1..3.json` dan `output_ready/thumbnails/thumb_1..3.jpg`;
4. pratinjau `thumb_1.jpg` dikirim ke Telegram (foto, bukan dokumen);
5. file > 2 MB ditandai di laporan (`oversized`) — batas keras YouTube.

Komponennya ada di [`remotion_template/YouTubeThumbnail.tsx`](remotion_template/YouTubeThumbnail.tsx)
dan didaftarkan sebagai `<Still id="YouTubeThumbnail" width={1280} height={720}>`.
Ciri yang lolos QC Damon: gradient gelap 95% di sisi teks, hook ≥ 96 px, badge aksen, bingkai tipis.

Saat Sora upload **live**, thumbnail dipasang otomatis
(`youtube.thumbnails().set(videoId=..., media_body=MediaFileUpload("output_ready/thumbnails/thumb_1.jpg"))`);
receipt mencatat `thumbnail.status` = `set` / `failed: ...` / `no_thumbnail`. Kegagalan
thumbnail **tidak** membatalkan upload.

Matikan dengan `STUDIO_THUMBNAILS=0`; laporan render lalu menulis
`thumbnails.status = "DIMATIKAN"`.

### Radar tren real-time (`studio/research.py`)

Tool Vito `scan_viral_trends_and_youtube_outliers(niche_query, geo_region="ID")` memanggil
`scan_radar()` yang menggabungkan tiga sumber:

| Sumber | Cara | Kunci |
|---|---|---|
| Google Trends RSS | `trends.google.com/trending/rss?geo=ID` | tidak perlu |
| Google News RSS | `news.google.com/rss/search?q=<niche> when:2d` | tidak perlu |
| YouTube outlier | `search.list` → `videos.list` → `channels.list`, lalu **Outlier Score = views ÷ subscriber** | `YOUTUBE_API_KEY` |
| Fallback YouTube | `yt-dlp ytsearchN:<niche> --dump-json` | tidak perlu (tanpa kuota) |

Contoh keluaran yang dipakai Vito: *"outlier 4.8x — channel 10 ribu subs, video 48 ribu
views dalam 5 hari"*. Hasil mentah disimpan di `workspace/vito_realtime_radar.json`
dan Vito **wajib** memakainya sebelum menulis brief (`why_viral_now`).

Kalau semua sumber gagal, tool membalas `RISET TIDAK TERSEDIA: ...` dan persona Vito
dilarang mengarang angka tren — sama seperti aturan `RISET TIDAK TERSEDIA` di §7.

---

## 7e. Control Center, Telegram dua arah & Shorts

### Dashboard KRA Control Center (`studio/dashboard.py`)

`--serve` kini juga menyajikan dashboard web: **http://127.0.0.1:8100/**. Tiga panel,
auto-refresh 3 detik, tanpa dependensi frontend:

| Panel | Isi | Sumber |
|---|---|---|
| 🤖 Live Agent Group Chat | percakapan Vito/Clara/Bima/Raka/Damon/Sora (termasuk panggilan tool) | `workspace/chat_logs.json` |
| 🧠 SQLite Content Memory | episode terakhir: judul, status, views, link YouTube | tabel `content_history` (§8) |
| ⚙️ Pipeline & Remotion Queue | status pipeline berjalan + `details` JSON + antrean | `workspace/pipeline_state.json` |

Butuh `pip install -r requirements-daemon.txt` (FastAPI + uvicorn). Padanan nama file spec
Creator: `kra_chat_logger.py` → `studio/chatlog.py`, `kra_dashboard_server.py` →
`studio/dashboard.py` (route-nya dipasang di `studio/api.py`, satu server port 8100).

Halaman HTML-nya **self-contained**: CSS + JS ditanam, tanpa CDN Tailwind/Alpine. Alasannya
praktis — mesin n100n sering tanpa internet, dan dashboard yang bergantung CDN akan tampil
sebagai teks polos saat offline. Palet & komponennya meniru Tailwind (slate + cyan), jadi
tampilan spec tetap konsisten.

Endpoint JSON (bisa dipakai Grafana/curl/panel lain):

```bash
curl -s http://127.0.0.1:8100/api/state            # status + runtime + ringkasan memori
curl -s http://127.0.0.1:8100/api/logs?limit=50    # chat log terakhir
curl -s http://127.0.0.1:8100/api/history          # katalog episode SQLite
curl -s http://127.0.0.1:8100/api/thumbnails       # variasi A/B + mana yang terpilih
curl -s -X POST "http://127.0.0.1:8100/api/thumbnail/select?index=2"
curl -s http://127.0.0.1:8100/api/thumbnail/2 -o pilihan.jpg
```

Pencatat percakapan ada di `studio/chatlog.py` (padanan `kra_chat_logger.py`): satu baris
per pesan (`sender`, `recipient`, `message`, `timestamp`), maksimum 100 pesan terakhir,
tulis atomik. `Office.run()` menjalankan thread pemantau yang menyalin pesan baru dari
`GroupChat.messages` **selagi rapat berjalan** (bukan setelah selesai), jadi panel chat
benar-benar "live" tanpa hook LLM.

### Telegram dua arah (`studio/telegram_webhook.py`)

Daftarkan sekali (butuh URL https publik — ngrok/cloudflared):

```bash
python virtual_office_superbots.py --set-webhook https://xxxx.ngrok-free.app/kra/telegram-webhook
# atau manual:
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://xxxx.ngrok-free.app/kra/telegram-webhook", "secret_token": "rahasia"}'
```

| Perintah | Efek nyata |
|---|---|
| `/status` | status pipeline, job aktif, antrean, jumlah episode & views |
| `/riwayat` | 5 episode terakhir: `project_id`, judul, status, views, link YouTube |
| `/thumb 1..3` | variasi thumbnail dipilih → disimpan, pratinjau dikirim, dipakai Sora saat upload |
| `/approve` | naskah disetujui → `READY_FOR_RENDER` memicu render Raka; `WAITING_FOR_CREATOR` menunggu file rekaman |
| `/help` | daftar perintah |

Keamanan: `TELEGRAM_WEBHOOK_SECRET` diisi → header `X-Telegram-Bot-Api-Secret-Token` wajib
cocok (403 bila tidak); `TELEGRAM_CHAT_ID` diisi → update dari chat lain diabaikan. Balasan
dikirim ke chat pengirim, bukan hanya ke chat default.

> `/approve` sengaja **tidak** mengarang status: kalau belum ada naskah (`IDLE`/`SCRIPTING`),
> bot menjawab terus terang dan pipeline tidak bergerak.

### YouTube Shorts otomatis (`studio/shorts.py`)

Setiap produksi yang selesai (`PUBLISHED_COMPLETE`) langsung memotong **45 detik** awal
`final_render.mp4` menjadi 1080×1920 vertikal di `output_ready/shorts/`:

```bash
ffmpeg -i final_render.mp4 -t 45 -filter_complex \
  "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:30[bg];\
   [0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];\
   [bg][fg]overlay=(W-w)/2:(H-h)/2[v]" \
  -map "[v]" -map "0:a?" -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p \
  -c:a aac -b:a 192k -movflags +faststart shorts/short_<epoch>.mp4
```

Teknik **blurred background stack**: latar = salinan penuh yang di-blur, video utama
di-*scale* utuh di tengah — tidak ada bagian frame 16:9 yang terpotong. Statusnya jujur:
`SHORTS_READY`, `SHORTS_GAGAL` (dengan potongan stderr ffmpeg), `SKIP_TANPA_FFMPEG`,
`DIMATIKAN`. Durasi dikunci 30–58 detik (`STUDIO_SHORTS_DURATION`), dan kegagalan Shorts
**tidak** mengubah status pipeline.

```bash
python virtual_office_superbots.py --shorts                  # dari final_render.mp4
python virtual_office_superbots.py --shorts master_lain.mp4  # dari master lain
```

### Sync views mingguan (n100n)

```bash
curl -s -X POST http://127.0.0.1:8100/kra/schedule/sync-views
# {"status":"SYNCED","videos":7,"updated":7,"views":48210,...} | NO_KEY | EMPTY
```

Node n8n siap impor ada di `schedules/kra_weekly_sync_views.json` (cron `0 0 * * 0` = Minggu
00:00 WIB). Endpoint dijalankan inline (satu request YouTube API, maks 50 video) supaya hasilnya
langsung bisa diperiksa scheduler; daemon juga menyinkronkan otomatis di awal rapat ideasi bila
`YOUTUBE_API_KEY` tersedia.

---

## 8. Memori jangka panjang

`workspace/studio.db` (SQLite, WAL, tanpa dependensi tambahan):

| Tabel | Isi |
|---|---|
| `runs` | setiap rapat: topik, status, brief, path naskah/video, URL YouTube, error |
| `videos` | video yang tayang + performa (`views`, `ctr`, catatan) |
| `topics` | topik yang pernah dibahas |
| `content_history` | **katalog episode** (memori episodik): topik, judul, keyword, status, `youtube_video_id`, `views_count` |

### Memori episodik (anti-duplikasi antar minggu)

Katalog episode dijaga dua lapis:

1. **FTS5** (`content_history_fts`, tersinkron lewat trigger) untuk pencarian cepat;
   bila build SQLite tidak punya FTS5, semuanya otomatis jatuh ke pemindaian penuh
   (`memory.fts == False`) dengan hasil yang sama.
2. **Skor kemiripan token** — irisan token ÷ himpunan token terkecil, stopword
   bahasa Indonesia dibuang. Ambang `DUPLICATE_WARNING` = **55 %**.

Alur di ruang rapat:

| Bot | Tool | Kapan |
|---|---|---|
| Vito | `get_recent_content_memory` | sebelum menyusun brief (lihat judul + views lama) |
| Vito & Damon | `check_topic_similarity_in_memory` | menguji calon topik; `DUPLICATE_WARNING` = ganti sudut |
| Damon | `register_new_episode_to_memory` | tepat setelah `SCRIPT PASS` (status `SCRIPT_APPROVED`) |
| Sora | *(otomatis)* | setelah upload → status `PUBLISHED` + `youtube_video_id` |

Sinkronisasi views untuk evaluasi Vito (kuota terpakai 1 request per 50 video):

```bash
curl -s -X POST "http://127.0.0.1:8100/kra/memory/sync-views"        # cron mingguan n100n
curl -s "http://127.0.0.1:8100/kra/memory/episodes?limit=15"          # audit topik
```

Daemon juga memanggilnya otomatis di awal rapat ideasi **bila** `YOUTUBE_API_KEY` ada.

Sebelum rapat dimulai, `MemoryStore.prompt_context()` merangkum riwayat dan
menyuntikkannya ke system prompt **Vito dan Sora saja**:

```text
## MEMORI STUDIO (3 rapat, 2 video, 4 episode tercatat)
- 2026-11-01 | Workflow editing video 2026 | PIPELINE_COMPLETE | judul: 5 Tool Editing ...
Performa video (pakai ini untuk memilih sudut pandang, bukan mengarang statistik):
- 5 Tool Editing Video → 2500 views, CTR 7.5% — CTR bagus
Aturan memori: jangan mengulang topik/judul di atas; ...
```

Mengisi performa (mis. setelah seminggu, dari YouTube Analytics atau `n100n`):

```python
from pathlib import Path
from studio.memory import MemoryStore

memory = MemoryStore(Path("workspace/studio.db"))
memory.record_performance("https://youtu.be/xxxx", views=18400, ctr=6.8, notes="judul A menang")
print(memory.stats())        # {'runs': 12, 'completed': 9, ..., 'total_views': 51000}
print(memory.prompt_context())
```

Memeriksa dari terminal: `python virtual_office_superbots.py --status`.

---

## 9. Upload YouTube sungguhan

1. Buat project di [Google Cloud Console](https://console.cloud.google.com/) → aktifkan
   **YouTube Data API v3**.
2. Buat kredensial **OAuth client ID → Desktop app**, unduh sebagai `client_secret.json`
   di root proyek (sudah masuk `.gitignore`).
3. Jalankan pipeline; Sora membuka browser sekali untuk consent, lalu menyimpan
   `token.json` dan memakainya lagi di run berikutnya.

Tanpa `client_secret.json`, Sora menulis `youtube_publish_receipt.json` berstatus
`UPLOADED_SIMULATION` — tidak ada klaim palsu bahwa video tayang. Privasi default
`unlisted`; `public` tidak pernah dipilih otomatis.

---

## 10. Arsitektur kode

```
virtual_office_superbots.py   # CLI tipis: --rehearsal | --daemon | --serve | --status | --doctor
studio/
  config.py                   # Settings (frozen), path studio, llm_config, deteksi env
  personas.py                 # muat agents/*.md + kontrak kerja
  tools.py                    # 7 tool nyata (riset, filesystem, media, YouTube)
  office.py                   # spesifikasi bot, graf transisi per fase, GroupChat
  media.py                    # pipeline ffmpeg/auto-editor/Whisper/Remotion + ffprobe
  story.py                    # jembatan JSON: naskah Clara → Whisper → Remotion → concat
  tts.py                      # voiceover faceless: ElevenLabs → Edge-TTS → loudnorm -14 LUFS
  broll.py                    # visual otomatis: DALL·E → Flux → Pexels → kanvas FFmpeg
  thumbnail.py                # thumbnail YouTube 1280x720 via `npx remotion still`
  state.py                    # pipeline_state.json atomik + gate untuk scheduler n100n
  research.py                 # tren nyata: YouTube API, Tavily, SerpAPI + radar outlier & RSS
  memory.py                   # SQLite: runs, videos, topics, katalog episode + FTS5 + views
  notify.py                   # Telegram: pesan HTML, lampiran multipart, laporan naskah/upload
  chatlog.py                  # chat_logs.json: 100 pesan terakhir GroupChat untuk dashboard
  dashboard.py                # KRA Control Center: HTML 3 panel + /api/state|logs|history|thumbnails
  telegram_webhook.py         # Telegram dua arah: /status /riwayat /thumb /approve + setWebhook
  shorts.py                   # YouTube Shorts 9:16: blur background stack via ffmpeg
  daemon.py                   # antrean job, watcher, scheduler, perintah Telegram
  api.py                      # FastAPI: /kra/schedule/1..3, /kra/n100n-trigger, /health, /status, dashboard
  report.py                   # transkrip & ringkasan run
  rehearsal.py                # pipeline deterministik tanpa LLM
agents/*.md                   # persona 6 bot
remotion_template/            # Root.tsx + StoryScene.tsx untuk kantor-rama-ai/remotion_studio
schedules/                    # kra_autonomous_workflow.json (n8n dual-loop) + kra_weekly_sync_views.json
tests/                        # 270 test: unit, daemon, API, dashboard, webhook, shorts, story, memori
requirements-daemon.txt       # fastapi + uvicorn (mode --serve)
requirements-media.txt        # openai-whisper (+ catatan ffmpeg/auto-editor/remotion)
```

Alasan pemisahan ini: orkestrasi AutoGen 0.2 (`office.py`) terisolasi dari tool dan
persona, sehingga mudah dimigrasikan ke AutoGen 0.4+ (`autogen-agentchat`) atau
Microsoft Agent Framework tanpa menulis ulang tool.

### Routing

`build_transitions()` menghasilkan graf:

```python
Creator → Vito                (selalu)
Vito    → Clara
Clara   → Damon
Damon   → Clara, Bima, Raka, Sora   ← hak veto dua arah
Bima    → Raka, Damon
Raka    → Damon
Sora    → Damon, Bima
```

Tidak ada self-loop. Giliran "membacakan hasil tool" dijamin oleh
`continuation_selector` (pesan ber-role `tool` selalu dikembalikan ke pemilik tool),
bukan oleh LLM — jadi bot tidak bisa mengobrol sendiri berputar-putar. Bila rapat
tetap macet, `max_round` menghentikannya dan laporan berstatus
`BERHENTI TANPA PIPELINE_COMPLETE`.

---

## 11. Test

```bash
pip install -r requirements-dev.txt
pytest            # 270 test
ruff check .
```

- `tests/test_tools.py` — filesystem, path traversal, render nyata (ffmpeg palsu), audit, upload, async gate.
- `tests/test_office.py` — struktur 6 bot, kepemilikan tool, graf transisi per fase, terminasi.
- `tests/test_rehearsal.py` — pipeline deterministik tanpa LLM.
- `tests/test_media.py` — deteksi kapabilitas, langkah yang gagal **dilaporkan** bukan disembunyikan, SRT/subtitle filter.
- `tests/test_memory.py` — upsert run, performa video, konteks memori, persistensi antar-proses, katalog episode (FTS5 + skor kemiripan, sinkronisasi views).
- `tests/test_daemon.py` — jadwal, watcher (file muncul sekali), antrean job, resume, perintah Telegram, shutdown.
- `tests/test_api.py` — webhook n100n: 202, 409, 422, sanitasi path; plus dual-loop `/kra/schedule/1..3`.
- `tests/test_research.py` — parsing YouTube/Tavily, fallback antar-provider, dan perilaku jujur tanpa kredensial.
- `tests/test_story.py` — validasi skema naskah, ekstraksi kata Whisper, staging `kra_temp`, render Remotion (npx palsu), concat, fallback FFmpeg, dan narasi TTS faceless.
- `tests/test_tts.py` — ElevenLabs (transport palsu), fallback Edge-TTS, normalisasi loudnorm, dan kegagalan yang dilaporkan jujur.
- `tests/test_state.py` — transisi status atomik dan matriks `gate()` (file, 0 byte, busy, faceless).
- `tests/test_notify.py` — pesan HTML Telegram, lampiran multipart, laporan naskah/upload, dan penandaan "SIMULASI".
- `tests/test_broll.py` — empat tier visual dengan transport palsu, cache, sanitasi nama, dan kegagalan jujur.
- `tests/test_thumbnail.py` — perencanaan varian dari brief, `npx remotion still` palsu, batas 2 MB, pratinjau Telegram.
- `tests/test_research.py` (radar) — parsing Google Trends/News RSS, Outlier Score, fallback yt-dlp, dan kejujuran tanpa jaringan.
- `tests/test_chatlog.py` — buffer 100 pesan, tulis atomik, pemulihan JSON rusak, infersi penerima, panggilan tool.
- `tests/test_office_chatlog.py` — `Office.run()` mencatat rapat berjalan (bukan setelah selesai), tanpa duplikasi antar-rapat, dan tetap tersimpan bila rapat crash.
- `tests/test_dashboard.py` — halaman 3 panel tanpa CDN, `/api/state|logs|history|thumbnails`, pemilihan thumbnail A/B, dan cron `sync-views` yang jujur tanpa key.
- `tests/test_telegram_webhook.py` — semua perintah (`/status` `/riwayat` `/thumb` `/approve`), validasi chat & secret token, serta `setWebhook` sukses/gagal.
- `tests/test_shorts.py` — filter blur-stack 1080×1920, batas durasi 30–58 detik, sanitasi nama, ffmpeg gagal dilaporkan.
- `tests/test_group_chat_e2e.py` — **GroupChat sungguhan** dengan `OpenAIWrapper.create`
  yang dipalsukan: membuktikan tool dieksekusi oleh bot yang tepat, loop
  Damon-menolak-Clara berjalan, dan rapat berhenti karena `PIPELINE_COMPLETE`, bukan
  kehabisan round.

`tests/mock_llm.py` berguna untuk eksperimen routing tanpa biaya API: skenario bot
dijawab deterministik, tetapi seluruh mesin AutoGen tetap berjalan.

---

## 12. Troubleshooting

| Gejala | Penyebab & solusi |
|---|---|
| Rapat selesai dengan 1 pesan | Ada pesan yang memuat `PIPELINE_COMPLETE` di luar giliran Sora. Cek template prompt Anda |
| `API key belum diset` | Isi `STUDIO_API_KEY`/`OPENAI_API_KEY`, atau jalankan `--rehearsal` |
| Bot hanya menulis di chat, file tidak ada | Model tidak mendukung tool calling. Ganti model, cek `studio_log.jsonl` |
| Bima berhenti di `STATUS BLOCKED` | Dijalankan tanpa TTY. Pakai `--mock-creator` atau taruh file lebih dulu |
| Render tetap `simulation` | `--real-render` belum aktif, `ffmpeg` tidak ada, atau input masih file dummy |
| `ERROR UPLOAD LIVE` | `client_secret.json` salah/scope belum disetujui; hapus `token.json` lalu ulangi |
| Rapat kehabisan round | Naikkan `--max-rounds`; lihat transkrip untuk bot yang berputar-putar |
| `STATUS AWAITING` muncul terus | Mode daemon menunggu file di `input_creator/`. Cek `/status` untuk melihat file yang terdeteksi |
| Daemon tidak bereaksi pada file baru | Pastikan ekstensinya video (`.mp4/.mov/.mkv/.webm/.avi`) dan umurnya > `STUDIO_UPLOAD_SETTLE` |
| Notifikasi tidak masuk | Isi `STUDIO_TELEGRAM_TOKEN` + `STUDIO_TELEGRAM_CHAT_ID`; kirim `/start` ke bot dulu. Cek `--doctor` |
| `webhook 409` | File `raw_video_filename` belum ada/0 byte di `input_creator/` — tunggu copy selesai |
| `RISET TIDAK TERSEDIA` | Isi `STUDIO_YOUTUBE_API_KEY` (cukup API key) atau Tavily/SerpAPI. Vito dilarang mengarang tren |
| Subtitle tidak ada / `whisper: failed` | `pip install -r requirements-media.txt`; cek `steps` di `.render.json` |
| Clara dapat `ERROR SCHEMA` | JSON scene breakdown belum sesuai kontrak (lihat §2 persona Clara / §5b); tool menyebut field yang salah |
| `remotion gagal` di laporan scene | Cek `STUDIO_REMOTION_DIR` menunjuk repo Remotion, `npx remotion` bisa jalan, dan `composition_id` terdaftar di `Root.tsx` |
| Video hitam / caption kosong | `words` kosong berarti Whisper tidak jalan; scene tetap dirender tanpa caption. Cek `engine` di `.render.json` |
| `kra_temp` menumpuk | Normalnya dibersihkan setelah build; sisa file muncul hanya bila proses mati di tengah render |
| `TTS gagal: edge-tts belum terpasang` | `pip install edge-tts`, atau isi `STUDIO_ELEVENLABS_API_KEY`. Tanpa keduanya scene jadi bisu — laporan menyebut `audio_provider: ""`, bukan suara palsu |
| Suara TTS terlalu cepat/lambat | Atur `STUDIO_TTS_RATE` (mis. `+0%`, `-5%`); Edge-TTS memakai format persen bertanda |
| ElevenLabs balas 401/429 | Kuota/kunci salah; pipeline otomatis jatuh ke Edge-TTS dan mencatatnya di `attempts` |
| Caption tidak sinkron dengan narasi | Whisper harus jalan (`pip install -r requirements-media.txt`); tanpa Whisper `words` kosong dan caption hilang |
| Notifikasi Telegram tidak masuk | Isi `STUDIO_TELEGRAM_TOKEN` + `STUDIO_TELEGRAM_CHAT_ID` (atau `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`); kirim `/start` ke bot dulu. `--doctor` menampilkan kanal aktif |
| Lampiran naskah tidak terkirim | File `script_production.json` harus ada di `workspace/`; kanal konsol memang tidak mengirim file (dicatat di log) |
| `dalle: GAGAL` / `flux: GAGAL` | Cek jaringan/kuota. Render tetap jalan dengan tier berikutnya; cek `scenes[].notes` di `.render.json` |
| Gambar B-Roll tidak muncul | `STUDIO_BROLL_PROVIDER=off`? `visual_cue` kosong? Scene sudah punya `broll_asset` manual? |
| `visual_mode` ditolak | Hanya `AI_IMAGE` atau `STOCK_VIDEO`; perbaiki naskah lalu simpan ulang |
| Thumbnail tidak dibuat | `STUDIO_THUMBNAILS=0`? `STUDIO_REMOTION_DIR` kosong? Composition `YouTubeThumbnail` belum terdaftar di `Root.tsx`? Cek `thumbnails.notes` di `.render.json` |
| `THUMBNAILS_GAGAL` tapi video jadi | Normal: thumbnail tidak pernah membatalkan render. Alasan ada di `notes` |
| Thumbnail > 2 MB | Turunkan `--quality` atau sederhanakan gambar; laporan menandainya di `oversized` |
| Thumbnail tidak terpasang di YouTube | Hanya pada upload **live** (`client_secret.json`); cek `thumbnail.status` di receipt |
| Vito berhenti di `DUPLICATE_WARNING` | Itu benar: topik ≥ 55 % mirip riwayat. Minta sudut baru, atau periksa `/kra/memory/episodes` |
| Dashboard tampil teks polos / 404 | `--serve` butuh FastAPI: `pip install -r requirements-daemon.txt`; buka `http://127.0.0.1:8100/` |
| Panel chat kosong padahal rapat jalan | `workspace/chat_logs.json` ditulis `Office.run()`; cek `StudioTools` log di `workspace/studio_log.jsonl` dan pastikan root studio sama |
| Webhook Telegram balas `403` | `TELEGRAM_WEBHOOK_SECRET` diset tetapi header `secret_token` tidak dikirim saat `setWebhook`; daftarkan ulang lewat `--set-webhook` |
| `/approve` bilang "belum ada naskah" | Pipeline memang `IDLE`/`SCRIPTING`; jalankan rapat dulu (`/topic …` atau jadwal 08:00) |
| Shorts `SKIP_TANPA_FFMPEG` | Install ffmpeg, atau set `STUDIO_SHORTS=0` bila tidak ingin Shorts |
| `python: no such module: fts5` | Build SQLite tanpa FTS5; modul otomatis memakai pemindaian penuh (`memory.fts = False`), tidak ada fitur yang hilang |
| `views_count` tetap 0 | `YOUTUBE_API_KEY` kosong atau video masih `UPLOADED_SIMULATION` (tidak punya `youtube_video_id`) |
| `RISET TIDAK TERSEDIA` dari radar | Tidak ada jaringan/kuota? Isi `YOUTUBE_API_KEY` atau `pip install yt-dlp`; Vito dilarang mengarang tren |
| `ready_to_produce` selalu false | Cek `pipeline_status`: `SCRIPTING_IN_PROGRESS`/`RENDERING` = sibuk, `PUBLISHED_COMPLETE`/`ERROR` = tunggu jadwal 08:00 berikutnya |
| Video Creator diproses dua kali | Seharusnya tidak terjadi: file diarsipkan ke `input_creator/processed/`. Hapus `workspace/pipeline_state.json` hanya bila status tertinggal setelah crash |

---

## 13. Catatan versi AutoGen

Repo ini memakai `pyautogen 0.2.35` karena artikel dan API yang dimaksud
(`GroupChat`, `GroupChatManager`, `register_function`, `allowed_or_disallowed_speaker_transitions`)
ada di jalur 0.2.x — yang sekarang berstatus **maintenance**. Untuk proyek baru,
AutoGen 0.4+ (`autogen-agentchat`: `RoundRobinGroupChat`, `SelectorGroupChat`,
`Swarm`) atau Microsoft Agent Framework adalah pilihan yang lebih aktif dikembangkan.
Struktur repo ini memudahkan migrasi: hanya `studio/office.py` yang perlu diganti,
tool dan persona tetap.

## 14. Keamanan

- `.env`, `client_secret.json`, `token.json` sudah di-`.gitignore`. Jangan commit.
- Token OAuth memberi akses upload ke channel Anda — perlakukan seperti password.
- Tool memvalidasi semua path relatif terhadap folder studio; bot tidak bisa menulis
  ke luar `workspace/`, `input_creator/`, `output_ready/`.
- Upload default `unlisted`, `selfDeclaredMadeForKids=false`.
