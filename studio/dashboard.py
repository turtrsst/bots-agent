"""KRA Control Center — dashboard web ringan di atas FastAPI.

Padanan `kra_dashboard_server.py` (spec Creator): tiga panel real-time.

1. **Live group chat** — `workspace/chat_logs.json` (ditulis `studio/chatlog.py`).
2. **Memori SQLite** — katalog episode dari `MemoryStore` (`GET /api/history`).
3. **Pipeline & render queue** — `workspace/pipeline_state.json` + antrean daemon.

Catatan desain: halaman HTML **tidak** memuat CDN Tailwind/Alpine. Semua CSS
dan JS ditanam di halaman sehingga dashboard tetap tampil rapi saat mesin n100n
offline (dan tidak ada request keluar dari browser). Palet, radius, dan
komponennya meniru Tailwind (slate + cyan), jadi tampilan spec tetap konsisten.

Endpoint (dipasang lewat `register_dashboard`):

* ``GET /`` dan ``GET /dashboard``            — antarmuka web
* ``GET /api/state``                          — status pipeline + runtime
* ``GET /api/logs``                           — chat log terakhir
* ``GET /api/history``                        — katalog episode SQLite
* ``GET /api/thumbnails``                     — variasi thumbnail A/B
* ``POST /api/thumbnail/select?index=2``      — pilih thumbnail (dipakai /thumb)
"""

from __future__ import annotations

from typing import Any

HTML_PAGE = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KRA Control Center — Autonomous Virtual Office</title>
<style>
  :root{
    --bg:#020617; --panel:#0f172a; --panel-2:#020617; --line:#1e293b;
    --text:#e2e8f0; --muted:#94a3b8; --dim:#64748b;
    --cyan:#22d3ee; --emerald:#34d399; --amber:#fbbf24; --rose:#fb7185;
    --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:radial-gradient(1200px 600px at 80% -10%,#0b2740 0%,var(--bg) 55%);
    color:var(--text); font-family:var(--font); min-height:100vh; padding:24px;
  }
  header{display:flex; flex-wrap:wrap; gap:16px; justify-content:space-between;
    align-items:center; border-bottom:1px solid var(--line); padding-bottom:16px; margin-bottom:20px}
  h1{margin:0; font-size:22px; letter-spacing:.14em; font-weight:900; color:var(--cyan)}
  .sub{font-size:11px; color:var(--dim); margin-top:4px; letter-spacing:.02em}
  .badges{display:flex; gap:10px; flex-wrap:wrap; align-items:center}
  .pill{display:flex; align-items:center; gap:8px; background:var(--panel); border:1px solid var(--line);
    padding:6px 12px; border-radius:10px; font-family:var(--mono); font-size:12px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--amber); box-shadow:0 0 0 4px rgba(251,191,36,.12)}
  .dot.run{background:var(--emerald); box-shadow:0 0 0 4px rgba(52,211,153,.12); animation:pulse 1.6s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .kpi{font-size:10px; color:var(--dim); text-transform:uppercase; letter-spacing:.1em}
  .grid{display:grid; grid-template-columns:repeat(1,minmax(0,1fr)); gap:18px}
  @media(min-width:1024px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
  .panel{background:linear-gradient(180deg,rgba(15,23,42,.95),rgba(2,6,23,.85));
    border:1px solid var(--line); border-radius:14px; padding:16px; display:flex; flex-direction:column;
    height:620px; box-shadow:0 20px 40px -30px #000}
  .panel h2{margin:0 0 12px; font-size:13px; letter-spacing:.06em; text-transform:uppercase; color:#cbd5e1;
    display:flex; align-items:center; justify-content:space-between; gap:8px}
  .panel h2 span.count{font-family:var(--mono); font-size:10px; color:var(--dim); font-weight:400;
    text-transform:none; letter-spacing:0}
  .scroll{flex:1; overflow-y:auto; padding-right:6px; display:flex; flex-direction:column; gap:10px}
  .scroll::-webkit-scrollbar{width:6px}
  .scroll::-webkit-scrollbar-thumb{background:#1e293b; border-radius:99px}
  .card{background:var(--panel-2); border:1px solid var(--line); border-radius:10px; padding:10px 12px; font-size:12px}
  .row{display:flex; justify-content:space-between; gap:8px; align-items:center; font-family:var(--mono);
    font-size:10.5px; color:var(--muted); margin-bottom:6px}
  .sender{color:var(--cyan); font-weight:700}
  .to{color:var(--dim)}
  .msg{color:#cbd5e1; white-space:pre-wrap; word-break:break-word; line-height:1.5; max-height:9.5em; overflow:hidden}
  .msg.tool{color:#a5b4fc; font-family:var(--mono); font-size:11px}
  .title{font-weight:700; color:#f1f5f9; margin:0 0 4px; font-size:12.5px; line-height:1.35}
  .topic{color:var(--dim); font-size:11px; margin:0 0 6px; display:-webkit-box; -webkit-line-clamp:2;
    -webkit-box-orient:vertical; overflow:hidden}
  .tag{font-family:var(--mono); font-size:9.5px; padding:2px 7px; border-radius:99px; border:1px solid var(--line);
    background:#0b1220; color:var(--muted); white-space:nowrap}
  .tag.pub{color:var(--emerald); border-color:#065f46}
  .tag.sim{color:var(--amber); border-color:#78350f}
  .tag.script{color:var(--cyan); border-color:#155e75}
  .meta{display:flex; justify-content:space-between; align-items:center; gap:8px; margin-top:8px; font-size:10.5px; color:var(--dim)}
  a{color:var(--cyan); text-decoration:none}
  a:hover{text-decoration:underline}
  .empty{margin:auto; text-align:center; color:var(--dim); font-size:11.5px; padding:40px 12px; line-height:1.7}
  .pre{background:var(--panel-2); border:1px solid var(--line); border-radius:10px; padding:12px;
    font-family:var(--mono); font-size:11px; color:#6ee7b7; white-space:pre-wrap; word-break:break-word;
    margin:0; overflow:auto; flex:1}
  .steps{display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px}
  .step{font-family:var(--mono); font-size:9.5px; padding:3px 8px; border-radius:99px; border:1px solid var(--line);
    color:var(--dim)}
  .step.on{color:#020617; background:var(--cyan); border-color:var(--cyan); font-weight:700}
  .step.done{color:var(--emerald); border-color:#065f46}
  .thumbs{display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-top:10px}
  .thumb{border:1px solid var(--line); border-radius:8px; overflow:hidden; cursor:pointer;
    background:var(--panel-2); padding:0; position:relative; aspect-ratio:16/9}
  .thumb img{width:100%; height:100%; object-fit:cover; display:block; opacity:.92}
  .thumb.sel{outline:2px solid var(--emerald); outline-offset:1px}
  .thumb span{position:absolute; left:4px; bottom:4px; font-family:var(--mono); font-size:9px;
    background:#020617cc; padding:1px 5px; border-radius:4px; color:var(--muted)}
  .footer{display:flex; justify-content:space-between; align-items:center; gap:8px; margin-top:12px;
    padding-top:10px; border-top:1px solid var(--line); font-size:10.5px; color:var(--dim)}
  button.refresh{background:var(--cyan); color:#020617; border:0; font-weight:800; font-size:11px;
    padding:7px 12px; border-radius:8px; cursor:pointer}
  button.refresh:hover{background:#67e8f9}
  .stale{color:var(--rose)}
</style>
</head>
<body>
<header>
  <div>
    <h1>KRA CONTROL CENTER</h1>
    <div class="sub">Kantor-Rama-AI · Autonomous Content Creator Office · Vito · Clara · Bima · Raka · Damon · Sora</div>
  </div>
  <div class="badges">
    <div class="pill"><span id="dot" class="dot"></span><span id="status">MEMUAT…</span></div>
    <div class="pill"><span class="kpi">Antrean</span><strong id="queue">–</strong></div>
    <div class="pill"><span class="kpi">Episode</span><strong id="episodes">–</strong></div>
    <div class="pill"><span class="kpi">Views</span><strong id="views">–</strong></div>
  </div>
</header>

<div class="grid">
  <!-- PANEL 1: LIVE GROUP CHAT -->
  <section class="panel">
    <h2>🤖 Live Agent Group Chat <span class="count" id="logs-count"></span></h2>
    <div class="scroll" id="logs"></div>
  </section>

  <!-- PANEL 2: SQLITE CONTENT MEMORY -->
  <section class="panel">
    <h2>🧠 SQLite Content Memory <span class="count" id="history-count"></span></h2>
    <div class="scroll" id="history"></div>
  </section>

  <!-- PANEL 3: PIPELINE & RENDER QUEUE -->
  <section class="panel">
    <h2>⚙️ Pipeline &amp; Remotion Queue <span class="count" id="updated"></span></h2>
    <div class="steps" id="steps"></div>
    <pre class="pre" id="details">memuat status…</pre>
    <div class="thumbs" id="thumbs"></div>
    <div class="footer">
      <span id="footnote">Auto-refresh 3 detik</span>
      <button class="refresh" onclick="fetchAll()">Refresh</button>
    </div>
  </section>
</div>

<script>
const STEPS = ["IDLE","SCRIPTING_IN_PROGRESS","WAITING_FOR_CREATOR","READY_FOR_RENDER","RENDERING","QC_AND_UPLOADING","PUBLISHED_COMPLETE"];
const LABELS = {"IDLE":"IDLE","SCRIPTING_IN_PROGRESS":"SCRIPTING","WAITING_FOR_CREATOR":"WAITING CREATOR",
  "READY_FOR_RENDER":"READY RENDER","RENDERING":"RENDERING","QC_AND_UPLOADING":"QC + UPLOAD",
  "PUBLISHED_COMPLETE":"PUBLISHED","ERROR":"ERROR"};

function esc(value){
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;");
}
function tagClass(status){
  const s = String(status || "");
  if (s.startsWith("PUBLISHED")) return "tag pub";
  if (s.startsWith("UPLOADED_SIMULATION")) return "tag sim";
  if (s.startsWith("SCRIPT")) return "tag script";
  return "tag";
}
function compact(n){
  const v = Number(n || 0);
  if (v >= 1000000) return (v/1000000).toFixed(1)+"M";
  if (v >= 1000) return (v/1000).toFixed(1)+"K";
  return String(v);
}

function renderLogs(logs){
  const box = document.getElementById("logs");
  document.getElementById("logs-count").textContent = logs.length + " pesan";
  if (!logs.length){
    box.innerHTML = '<div class="empty">Belum ada percakapan bot.<br>Jalankan rapat: <code>--topic "…"</code> atau /topic via Telegram.</div>';
    return;
  }
  box.innerHTML = logs.slice().reverse().map(log => {
    const tool = String(log.message || "").startsWith("🛠️");
    return '<div class="card"><div class="row"><span class="sender">' + esc(log.sender) + '</span>' +
      '<span>' + esc(log.timestamp) + '</span></div>' +
      (log.recipient && log.recipient !== "GROUPCHAT"
        ? '<div class="row"><span class="to">→ ' + esc(log.recipient) + '</span></div>' : '') +
      '<div class="msg' + (tool ? ' tool' : '') + '">' + esc(log.message) + '</div></div>';
  }).join("");
}

function renderHistory(items){
  const box = document.getElementById("history");
  document.getElementById("history-count").textContent = items.length + " episode";
  if (!items.length){
    box.innerHTML = '<div class="empty">Katalog episode masih kosong.<br>Setiap naskah yang lolos gerbang Damon otomatis tercatat di sini.</div>';
    return;
  }
  box.innerHTML = items.map(item => {
    const views = Number(item.views_count || 0);
    return '<div class="card">' +
      '<div class="row"><span style="color:#fbbf24">' + esc(item.project_id) + '</span>' +
      '<span class="' + tagClass(item.status) + '">' + esc(item.status) + '</span></div>' +
      '<p class="title">' + esc(item.primary_title || "(tanpa judul)") + '</p>' +
      '<p class="topic">' + esc(item.topic_summary || "") + '</p>' +
      '<div class="meta"><span>' + esc((item.created_at || "").slice(0,10)) + '</span>' +
      '<span>' + (views ? compact(views) + ' views' : 'belum ada views') + '</span></div>' +
      (item.youtube_url ? '<div class="meta"><a href="' + esc(item.youtube_url) + '" target="_blank" rel="noopener">🔗 Tonton di YouTube →</a></div>' : '') +
      '</div>';
  }).join("");
}

function renderSteps(status){
  document.getElementById("steps").innerHTML = STEPS.map(step => {
    const index = STEPS.indexOf(step);
    const current = STEPS.indexOf(status);
    let cls = "step";
    if (step === status) cls += " on";
    else if (current > index && current >= 0) cls += " done";
    const label = LABELS[step] || step;
    return '<span class="' + cls + '">' + label + '</span>';
  }).join("");
}

function renderThumbs(items){
  const box = document.getElementById("thumbs");
  if (!items || !items.length){ box.innerHTML = ""; return; }
  box.innerHTML = items.map(item =>
    '<button class="thumb' + (item.selected ? ' sel' : '') + '" title="Pilih ' + esc(item.file) + '" ' +
    'onclick="selectThumb(' + item.index + ')">' +
    '<img src="/api/thumbnail/' + item.index + '?v=' + esc(item.mtime) + '" alt="' + esc(item.file) + '">' +
    '<span>#' + item.index + (item.selected ? ' ✓' : '') + '</span></button>').join("");
}

async function selectThumb(index){
  try{
    const res = await fetch("/api/thumbnail/select?index=" + index, {method:"POST"});
    const body = await res.json();
    document.getElementById("footnote").textContent = body.reply || body.status;
    fetchAll();
  }catch(err){ console.error(err); }
}

async function fetchAll(){
  try{
    const [stateRes, logsRes, histRes, thumbRes] = await Promise.all([
      fetch("/api/state"), fetch("/api/logs?limit=100"),
      fetch("/api/history?limit=20"), fetch("/api/thumbnails")
    ]);
    const state = await stateRes.json();
    const logs = await logsRes.json();
    const history = await histRes.json();
    const thumbs = await thumbRes.json();

    document.getElementById("status").textContent = state.status || "IDLE";
    document.getElementById("dot").className = "dot" + (state.status && state.status !== "IDLE" ? " run" : "");
    document.getElementById("queue").textContent = state.runtime ? state.runtime.queue : "–";
    document.getElementById("episodes").textContent = state.memory ? state.memory.episodes : "–";
    const views = state.memory ? (state.memory.episode_views || state.memory.total_views) : 0;
    document.getElementById("views").textContent = compact(views);
    document.getElementById("updated").textContent = state.updated_at ? state.updated_at.slice(11,19) : "";
    renderSteps(state.status);
    document.getElementById("details").textContent = JSON.stringify(
      {status: state.status, details: state.details || {}, runtime: state.runtime || {}}, null, 2);

    renderLogs(logs);
    renderHistory(history);
    renderThumbs(thumbs.items || []);
    document.getElementById("footnote").textContent = "Auto-refresh 3 detik · " +
      new Date().toLocaleTimeString("id-ID");
  }catch(err){
    const footnote = document.getElementById("footnote");
    footnote.className = "stale";
    footnote.textContent = "Koneksi dashboard terputus: " + err;
  }
}
fetchAll();
setInterval(fetchAll, 3000);
</script>
</body>
</html>
"""


def render_dashboard_html() -> str:
    return HTML_PAGE


def pipeline_payload(daemon: Any) -> dict[str, Any]:
    """Isi `GET /api/state`: status pipeline + ringkasan runtime."""
    state = daemon.state.read()
    memory = daemon.memory.stats()
    running = daemon.active_job()
    return {
        "status": state.get("status", "IDLE"),
        "details": state.get("details") or {},
        "updated_at": state.get("updated_at", ""),
        "previous_status": state.get("previous_status", ""),
        "runtime": {
            "daemon_running": bool(daemon._running),
            "queue": daemon.queue.qsize(),
            "active_job": running.as_dict() if running else None,
            "jobs": [job.as_dict() for job in list(daemon.jobs.values())[-5:]],
            "capabilities": daemon.capabilities.as_dict(),
            "notifier": type(daemon.notifier).__name__,
            "root": str(daemon.settings.paths.root),
        },
        "memory": {
            "episodes": memory.get("episodes", 0),
            "published_episodes": memory.get("published_episodes", 0),
            "total_views": memory.get("total_views", 0),
            "episode_views": memory.get("episode_views", 0),
            "runs": memory.get("runs", 0),
            "videos": memory.get("videos", 0),
        },
    }


def thumbnail_variants(daemon: Any) -> dict[str, Any]:
    """Daftar `output_ready/thumbnails/thumb_*.jpg` + mana yang terpilih."""
    from .thumbnail import selected_thumbnail
    from .thumbnail import thumbnail_variants as variants

    folder = daemon.settings.paths.output_ready / "thumbnails"
    chosen = selected_thumbnail(daemon.settings)
    chosen_name = chosen.name if chosen else ""
    items = []
    for item in variants(daemon.settings):
        index = item["index"]
        path = folder / item["file"]
        items.append(
            {
                "index": index,
                "file": item["file"],
                "size_kb": round(path.stat().st_size / 1024, 1) if path.is_file() else 0,
                "mtime": int(path.stat().st_mtime) if path.is_file() else 0,
                "selected": item["file"] == chosen_name,
            }
        )
    return {"selected": chosen_name, "items": items}


def register_dashboard(app: Any, daemon: Any, *, prefix: str = "/kra/telegram-webhook") -> None:
    """Pasang route dashboard + API-nya ke aplikasi FastAPI."""
    from fastapi.responses import HTMLResponse, JSONResponse

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard_page() -> Any:
        return HTMLResponse(render_dashboard_html())

    @app.get("/api/state")
    async def api_state() -> dict[str, Any]:
        return pipeline_payload(daemon)

    @app.get("/api/logs")
    async def api_logs(limit: int = 100) -> list[dict[str, Any]]:
        return daemon.chat_log.tail(max(1, min(limit, 500)))

    @app.get("/api/history")
    async def api_history(limit: int = 20) -> list[dict[str, Any]]:
        episodes = daemon.memory.recent_episodes(max(1, min(limit, 200)))
        return [
            {
                "project_id": episode.get("project_id", ""),
                "primary_title": episode.get("primary_title", ""),
                "topic_summary": episode.get("topic_summary", ""),
                "status": episode.get("status", ""),
                "youtube_video_id": episode.get("youtube_video_id", ""),
                "youtube_url": episode.get("youtube_url", ""),
                "views_count": episode.get("views_count", 0),
                "created_at": episode.get("created_at", ""),
            }
            for episode in episodes
        ]

    @app.get("/api/thumbnails")
    async def api_thumbnails() -> dict[str, Any]:
        return thumbnail_variants(daemon)

    @app.get("/api/thumbnail/{index}")
    async def api_thumbnail_file(index: int) -> Any:
        from fastapi.responses import FileResponse

        folder = daemon.settings.paths.output_ready / "thumbnails"
        target = folder / f"thumb_{index}.jpg"
        if not target.is_file():
            return JSONResponse({"status": "TIDAK_ADA", "file": target.name}, status_code=404)
        return FileResponse(target, media_type="image/jpeg")

    @app.post("/api/thumbnail/select")
    async def api_thumbnail_select(index: int) -> dict[str, Any]:
        from .thumbnail import choose_thumbnail

        chosen = choose_thumbnail(daemon.settings, index, via="dashboard")
        if chosen is None:
            return JSONResponse(
                {"status": "TIDAK_ADA", "reply": f"Thumbnail #{index} belum ada di output_ready/thumbnails/"},
                status_code=404,
            )
        daemon.state.update(daemon.state.status(), selected_thumbnail=chosen.name)
        return {
            "status": "THUMBNAIL_DIPILIH",
            "file": chosen.name,
            "reply": f"✅ Thumbnail {chosen.name} dipakai untuk upload YouTube berikutnya.",
        }
