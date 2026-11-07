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
python virtual_office_superbots.py --serve           # bridge HTTP untuk webhook n100n (port 8100)
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

---

## 2. Tim & pembagian tool

Setiap bot memegang **hanya** tool sesuai perannya. Tool yang sama tidak dibagi dua bot
(lihat perbaikan #3).

| Bot | Peran | Tool | Menyerahkan ke |
|---|---|---|---|
| `Creator_Kamu` | Anda (manusia) | — | Vito |
| `Vito_TrendStrategist` | riset tren **nyata**, judul, thumbnail | `research_current_trends`, `save_content_brief` | Clara |
| `Clara_Scriptwriter` | naskah retensi tinggi, A-Roll/B-Roll | `save_production_script`, `save_scene_breakdown` | Damon |
| `Damon_RealityChecker` | QC + hak veto (2 gerbang) | `inspect_rendered_output` | Clara / Bima / Raka / Sora |
| `Bima_StudioPM` | gatekeeper human-in-the-loop | `request_and_check_creator_files` | Raka |
| `Raka_Producer` | render ffmpeg 1080p60 / Remotion per scene | `assemble_and_render_video`, `assemble_story_from_script` | Damon |
| `Sora_YouTubePublisher` | SEO + upload YouTube | `upload_to_youtube` | — (menutup rapat) |

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
| Whisper | `word_timestamps=True` → `[{word, start, end}]`; `durationInSeconds = max(end)` |
| Stage aset | file disalin ke `remotion_studio/public/kra_temp/` — Chromium Remotion menolak `file://`, ini yang membuat `staticFile()` aman |
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
caption tidak berkedip). Salin ke `remotion_studio/src/`, lalu set
`STUDIO_REMOTION_DIR`.

Bila Node/Remotion belum siap, setiap scene tetap dirender dengan FFmpeg
(conform 1080p + burn-in SRT) dan laporan menyebut `engine: "ffmpeg-fallback"` —
Raka wajib mengatakannya di ruang rapat, bukan mengaku sudah memakai Remotion.

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
| `GET /health` | hidup + kapabilitas media (`ffmpeg`, `whisper`, `remotion`) |
| `GET /status` | antrean, job terakhir, isi memori, file yang menunggu |
| `GET /jobs/{id}` | detail satu job |

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

## 8. Memori jangka panjang

`workspace/studio.db` (SQLite, WAL, tanpa dependensi tambahan):

| Tabel | Isi |
|---|---|
| `runs` | setiap rapat: topik, status, brief, path naskah/video, URL YouTube, error |
| `videos` | video yang tayang + performa (`views`, `ctr`, catatan) |
| `topics` | topik yang pernah dibahas |

Sebelum rapat dimulai, `MemoryStore.prompt_context()` merangkum riwayat dan
menyuntikkannya ke system prompt **Vito dan Sora saja**:

```text
## MEMORI STUDIO (3 rapat terakhir, 2 video tercatat)
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
  research.py                 # tren nyata: YouTube Data API, Tavily, SerpAPI
  memory.py                   # SQLite: runs, videos, topics, prompt_context()
  notify.py                   # Telegram Bot API (urllib) + konsol
  daemon.py                   # antrean job, watcher, scheduler, perintah Telegram
  api.py                      # FastAPI: /kra/n100n-trigger, /health, /status
  report.py                   # transkrip & ringkasan run
  rehearsal.py                # pipeline deterministik tanpa LLM
agents/*.md                   # persona 6 bot
remotion_template/            # Root.tsx + StoryScene.tsx untuk kantor-rama-ai/remotion_studio
tests/                        # 103 test: unit, daemon, API, story, e2e GroupChat dengan LLM palsu
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
pytest            # 103 test
ruff check .
```

- `tests/test_tools.py` — filesystem, path traversal, render nyata (ffmpeg palsu), audit, upload, async gate.
- `tests/test_office.py` — struktur 6 bot, kepemilikan tool, graf transisi per fase, terminasi.
- `tests/test_rehearsal.py` — pipeline deterministik tanpa LLM.
- `tests/test_media.py` — deteksi kapabilitas, langkah yang gagal **dilaporkan** bukan disembunyikan, SRT/subtitle filter.
- `tests/test_memory.py` — upsert run, performa video, konteks memori, persistensi antar-proses.
- `tests/test_daemon.py` — jadwal, watcher (file muncul sekali), antrean job, resume, perintah Telegram, shutdown.
- `tests/test_api.py` — webhook n100n: 202, 409, 422, sanitasi path.
- `tests/test_research.py` — parsing YouTube/Tavily, fallback antar-provider, dan perilaku jujur tanpa kredensial.
- `tests/test_story.py` — validasi skema naskah, ekstraksi kata Whisper, staging `kra_temp`, render Remotion (npx palsu), concat, dan fallback FFmpeg.
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
