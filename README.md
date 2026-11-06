# bots-agent — Autonomous Virtual Creator Office

Enam "Super Bot" AutoGen yang duduk di satu ruang rapat: berdebat, saling mereview,
memakai tool sungguhan (tulis file, render video, audit bukti, upload YouTube), dan
berhenti sendiri saat video selesai dipublikasikan.

Proyek ini adalah implementasi yang **sudah diperbaiki dan diuji** dari arsitektur
`GroupChat` + `GroupChatManager` di artikel "Virtual Office Superbots". Versi asli
tidak akan berjalan sesuai deskripsinya — lihat [Perbaikan penting](#perbaikan-penting).

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
python virtual_office_superbots.py --doctor          # cek API key, persona, ffmpeg, OAuth
python virtual_office_superbots.py --mock-creator    # file A-Roll dummy otomatis (CI/demo)
python virtual_office_superbots.py --real-render     # aktifkan ffmpeg asli
```

Keluaran ada di `workspace/`, `input_creator/`, `output_ready/`:

| Berkas | Isi |
|---|---|
| `workspace/content_brief.json` | brief Vito (judul, thumbnail, angle) |
| `workspace/script_production.md` | naskah Clara, ditandai `[CREATOR A-ROLL]` / `[AI B-ROLL]` |
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
| `Vito_TrendStrategist` | riset tren, judul, thumbnail | `save_content_brief` | Clara |
| `Clara_Scriptwriter` | naskah retensi tinggi, A-Roll/B-Roll | `save_production_script` | Damon |
| `Damon_RealityChecker` | QC + hak veto (2 gerbang) | `inspect_rendered_output` | Clara / Bima / Raka / Sora |
| `Bima_StudioPM` | gatekeeper human-in-the-loop | `request_and_check_creator_files` | Raka |
| `Raka_Producer` | render ffmpeg 1080p60 | `assemble_and_render_video` | Damon |
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
| `STUDIO_API_KEY` | — | API key LLM (fallback: `OPENAI_API_KEY`) |
| `STUDIO_BASE_URL` | OpenAI | Endpoint kompatibel OpenAI (Gemini, OpenRouter, Groq, LM Studio) |
| `STUDIO_MODEL` | `gpt-4o` | Nama model |
| `STUDIO_TEMPERATURE` | `0.4` | Kreativitas |
| `STUDIO_MAX_ROUNDS` | `40` | Batas putaran group chat |
| `STUDIO_MOCK_CREATOR` | `0` | `1` = file A-Roll dummy otomatis |
| `STUDIO_REAL_RENDER` | `0` | `1` = jalankan ffmpeg asli |
| `STUDIO_CREATOR_TIMEOUT` | `0` | Batas tunggu upload Creator (detik, `0` = tanpa batas) |
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

## 5. Render video sungguhan

Secara default Raka menulis artefak **simulasi** (file dummy + laporan `engine:
"simulation"`), supaya alur bisa diuji tanpa video asli. Untuk produksi:

1. Rekam A-Roll sesuai `workspace/creator_todo_list.md`.
2. Simpan sebagai `input_creator/a_roll_raw.mp4`.
3. Jalankan dengan `--real-render` (butuh `ffmpeg` di `PATH`).

Raka lalu menjalankan:

```bash
ffmpeg -i a_roll_raw.mp4 \
  -vf "scale=1920:-2:flags=lanczos,fps=60" \
  -af "loudnorm=I=-14:TP=-1.5:LRA=11" \
  -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p \
  -c:a aac -b:a 192k -movflags +faststart final_render.mp4
```

Input tanpa audio otomatis dirender ulang tanpa filter audio (dicatat di laporan).
Untuk memotong jeda hening, pasang [auto-editor](https://github.com/WyattBlue/auto-editor)
dan jalankan sebagai pra-langkah sebelum `a_roll_raw.mp4`.

Damon mengaudit hasil ini lewat `inspect_rendered_output`: file ada, bukan 0 byte,
header MP4 valid, side-car ada, dan resolusi ≥ 1080p (dari `ffprobe`).

---

## 6. Upload YouTube sungguhan

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

## 7. Arsitektur kode

```
virtual_office_superbots.py   # CLI tipis: argumen, banner, ringkasan
studio/
  config.py                   # Settings, path studio, llm_config
  personas.py                 # muat agents/*.md + kontrak kerja
  tools.py                    # 6 tool nyata (filesystem, ffmpeg, YouTube)
  office.py                   # spesifikasi bot, graf transisi, GroupChat
  report.py                   # transkrip & ringkasan run
  rehearsal.py                # pipeline deterministik tanpa LLM
agents/*.md                   # persona 6 bot
tests/                        # unit test + e2e GroupChat dengan LLM palsu
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

## 8. Test

```bash
pip install -r requirements-dev.txt
pytest            # 31 test
ruff check .
```

- `tests/test_tools.py` — validasi filesystem, path traversal, render, audit, upload.
- `tests/test_office.py` — struktur 6 bot, kepemilikan tool, graf transisi, terminasi.
- `tests/test_rehearsal.py` — pipeline deterministik tanpa LLM.
- `tests/test_group_chat_e2e.py` — **GroupChat sungguhan** dengan `OpenAIWrapper.create`
  yang dipalsukan: membuktikan tool dieksekusi oleh bot yang tepat, loop
  Damon-menolak-Clara berjalan, dan rapat berhenti karena `PIPELINE_COMPLETE`, bukan
  kehabisan round.

`tests/mock_llm.py` berguna untuk eksperimen routing tanpa biaya API: skenario bot
dijawab deterministik, tetapi seluruh mesin AutoGen tetap berjalan.

---

## 9. Troubleshooting

| Gejala | Penyebab & solusi |
|---|---|
| Rapat selesai dengan 1 pesan | Ada pesan yang memuat `PIPELINE_COMPLETE` di luar giliran Sora. Cek template prompt Anda |
| `API key belum diset` | Isi `STUDIO_API_KEY`/`OPENAI_API_KEY`, atau jalankan `--rehearsal` |
| Bot hanya menulis di chat, file tidak ada | Model tidak mendukung tool calling. Ganti model, cek `studio_log.jsonl` |
| Bima berhenti di `STATUS BLOCKED` | Dijalankan tanpa TTY. Pakai `--mock-creator` atau taruh file lebih dulu |
| Render tetap `simulation` | `--real-render` belum aktif, `ffmpeg` tidak ada, atau input masih file dummy |
| `ERROR UPLOAD LIVE` | `client_secret.json` salah/scope belum disetujui; hapus `token.json` lalu ulangi |
| Rapat kehabisan round | Naikkan `--max-rounds`; lihat transkrip untuk bot yang berputar-putar |

---

## 10. Catatan versi AutoGen

Repo ini memakai `pyautogen 0.2.35` karena artikel dan API yang dimaksud
(`GroupChat`, `GroupChatManager`, `register_function`, `allowed_or_disallowed_speaker_transitions`)
ada di jalur 0.2.x — yang sekarang berstatus **maintenance**. Untuk proyek baru,
AutoGen 0.4+ (`autogen-agentchat`: `RoundRobinGroupChat`, `SelectorGroupChat`,
`Swarm`) atau Microsoft Agent Framework adalah pilihan yang lebih aktif dikembangkan.
Struktur repo ini memudahkan migrasi: hanya `studio/office.py` yang perlu diganti,
tool dan persona tetap.

## 11. Keamanan

- `.env`, `client_secret.json`, `token.json` sudah di-`.gitignore`. Jangan commit.
- Token OAuth memberi akses upload ke channel Anda — perlakukan seperti password.
- Tool memvalidasi semua path relatif terhadap folder studio; bot tidak bisa menulis
  ke luar `workspace/`, `input_creator/`, `output_ready/`.
- Upload default `unlisted`, `selfDeclaredMadeForKids=false`.
