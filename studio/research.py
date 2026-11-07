"""Riset tren nyata untuk Vito — bukan ingatan LLM.

Urutan provider (yang pertama tersedia & berhasil dipakai):

1. **YouTube Data API v3** — `videos.list?chart=mostPopular` (tren hari ini di ID)
   atau `search.list?order=viewCount&publishedAfter=7 hari` untuk topik tertentu.
   Hanya butuh API key, tanpa OAuth.
2. **Tavily** — `POST https://api.tavily.com/search`.
3. **SerpAPI** — `engine=youtube&search_query=...`.

Kalau tidak ada kredensial atau semua gagal, hasilnya **kosong dan jujur**
(`provider="none"`, ada `warning`). Bot diminta tidak mengarang angka tren.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import Settings

Transport = Callable[..., dict[str, Any]]
DEFAULT_TIMEOUT = 20.0


def default_transport(
    method: str, url: str, payload: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json", "User-Agent": "kantor-rama-ai/studio"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - host tetap milik provider
        return json.loads(response.read().decode("utf-8"))


@dataclass
class TrendItem:
    title: str
    channel: str = ""
    views: int | None = None
    published: str = ""
    url: str = ""
    source: str = ""


@dataclass
class ResearchResult:
    provider: str = "none"
    query: str = ""
    items: list[TrendItem] = field(default_factory=list)
    fetched_at: str = ""
    warning: str = ""
    attempts: list[str] = field(default_factory=list)

    @property
    def real(self) -> bool:
        return bool(self.items) and self.provider != "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "real_data": self.real,
            "query": self.query,
            "fetched_at": self.fetched_at,
            "warning": self.warning,
            "attempts": self.attempts,
            "items": [item.__dict__ for item in self.items],
        }

    def as_text(self, limit: int = 10) -> str:
        lines = []
        for item in self.items[:limit]:
            detail = ", ".join(part for part in (item.channel, human_views(item.views), item.published) if part)
            lines.append(f"- {item.title}" + (f" ({detail})" if detail else ""))
        return "\n".join(lines)


def human_views(views: int | None) -> str:
    if views is None:
        return ""
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f}jt views"
    if views >= 1_000:
        return f"{views / 1_000:.1f}rb views"
    return f"{views} views"


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# =====================================================================
# Provider
# =====================================================================
def youtube_trending(
    api_key: str, *, region: str = "ID", limit: int = 10, transport: Transport | None = None
) -> list[TrendItem]:
    url = "https://www.googleapis.com/youtube/v3/videos?" + "&".join(
        [
            "part=snippet,statistics",
            "chart=mostPopular",
            f"regionCode={region}",
            f"maxResults={limit}",
            "videoCategoryId=27",  # Education
            f"key={api_key}",
        ]
    )
    data = (transport or default_transport)("GET", url, None)
    return [
        TrendItem(
            title=entry.get("snippet", {}).get("title", ""),
            channel=entry.get("snippet", {}).get("channelTitle", ""),
            views=_int(entry.get("statistics", {}).get("viewCount")),
            published=(entry.get("snippet", {}).get("publishedAt") or "")[:10],
            url=f"https://youtu.be/{entry.get('id', '')}",
            source="youtube:trending",
        )
        for entry in data.get("items", [])
    ]


def youtube_search(
    api_key: str,
    query: str,
    *,
    region: str = "ID",
    limit: int = 10,
    days: int = 7,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> list[TrendItem]:
    published_after = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    url = "https://www.googleapis.com/youtube/v3/search?" + "&".join(
        [
            "part=snippet",
            f"q={query}",
            "order=viewCount",
            "type=video",
            f"publishedAfter={published_after}",
            f"regionCode={region}",
            f"maxResults={limit}",
            f"key={api_key}",
        ]
    )
    data = (transport or default_transport)("GET", url, None)
    items: list[TrendItem] = []
    for entry in data.get("items", []):
        snippet = entry.get("snippet", {})
        video_id = (entry.get("id") or {}).get("videoId", "")
        items.append(
            TrendItem(
                title=snippet.get("title", ""),
                channel=snippet.get("channelTitle", ""),
                published=(snippet.get("publishedAt") or "")[:10],
                url=f"https://youtu.be/{video_id}",
                source="youtube:search",
            )
        )
    return items


def tavily_search(
    api_key: str, query: str, *, limit: int = 10, transport: Transport | None = None
) -> list[TrendItem]:
    payload = {"api_key": api_key, "query": query, "search_depth": "basic", "max_results": limit}
    data = (transport or default_transport)("POST", "https://api.tavily.com/search", payload)
    return [
        TrendItem(title=entry.get("title", ""), url=entry.get("url", "")[:300], source="tavily")
        for entry in data.get("results", [])
    ]


def serpapi_search(
    api_key: str, query: str, *, limit: int = 10, transport: Transport | None = None
) -> list[TrendItem]:
    url = "https://serpapi.com/search.json?" + "&".join(
        ["engine=youtube", f"search_query={query}", f"api_key={api_key}"]
    )
    data = (transport or default_transport)("GET", url, None)
    return [
        TrendItem(
            title=entry.get("title", ""),
            channel=(entry.get("channel") or {}).get("name", "") if isinstance(entry.get("channel"), dict) else "",
            views=_int(entry.get("views")),
            published=str(entry.get("published_date") or ""),
            url=entry.get("link", ""),
            source="serpapi",
        )
        for entry in data.get("video_results", [])[:limit]
    ]


# =====================================================================
# Orkestrasi
# =====================================================================
def fetch_trends(
    settings: Settings,
    topic: str = "",
    *,
    limit: int = 8,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> ResearchResult:
    """Coba setiap provider yang punya kredensial sampai ada yang berhasil."""
    query = topic.strip() or "tren konten YouTube Indonesia hari ini"
    result = ResearchResult(query=query, fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"))

    plan: list[tuple[str, Callable[[], list[TrendItem]]]] = []
    if settings.youtube_api_key:
        key = settings.youtube_api_key
        if topic.strip():
            plan.append(("youtube:search", lambda: youtube_search(key, topic.strip(), limit=limit, transport=transport, now=now)))
        else:
            plan.append(("youtube:trending", lambda: youtube_trending(key, limit=limit, transport=transport)))
            plan.append(("youtube:search", lambda: youtube_search(key, query, limit=limit, transport=transport, now=now)))
    if settings.tavily_api_key:
        key = settings.tavily_api_key
        plan.append(("tavily", lambda: tavily_search(key, query, limit=limit, transport=transport)))
    if settings.serpapi_api_key:
        key = settings.serpapi_api_key
        plan.append(("serpapi", lambda: serpapi_search(key, query, limit=limit, transport=transport)))

    if not plan:
        result.warning = (
            "Tidak ada kredensial riset (STUDIO_YOUTUBE_API_KEY / TAVILY_API_KEY / SERPAPI_API_KEY). "
            "Data tren real-time tidak tersedia."
        )
        return result

    for name, fetch in plan:
        try:
            items = [item for item in fetch() if item.title]
        except Exception as exc:  # noqa: BLE001 - provider apa pun boleh gagal
            result.attempts.append(f"{name}: GAGAL ({type(exc).__name__}: {str(exc)[:120]})")
            continue
        if items:
            result.provider = name
            result.items = items[:limit]
            result.attempts.append(f"{name}: ok ({len(items)} hasil)")
            return result
        result.attempts.append(f"{name}: kosong")

    result.warning = "Semua provider riset gagal atau kosong: " + "; ".join(result.attempts)
    return result


# =====================================================================
# Radar real-time Vito: Google Trends RSS + Outlier YouTube (+ yt-dlp)
# =====================================================================
TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"
NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=id&gl=ID&ceid=ID:id"
USER_AGENT = "Mozilla/5.0 (compatible; kantor-rama-ai/studio)"

FetchText = Callable[[str], str]
Runner = Callable[..., Any]


def default_fetch_text(url: str, *, timeout: float = 15.0) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL feed publik
        return response.read().decode("utf-8", errors="replace")


def _rss_items(xml_text: str) -> list[ET.Element]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    return root.findall(".//item")


def google_trends_rss(
    geo: str = "ID", *, limit: int = 8, fetch: FetchText | None = None
) -> list[dict[str, Any]]:
    """Kueri yang sedang melonjak di Google Trends hari ini (tanpa API key)."""
    xml_text = (fetch or default_fetch_text)(TRENDS_RSS.format(geo=geo))
    trends: list[dict[str, Any]] = []
    ns = "{https://trends.google.com/trending/rss}"
    for item in _rss_items(xml_text)[:limit]:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        news = item.findtext(f"{ns}news_item/{ns}news_item_title") or ""
        trends.append(
            {
                "source": f"Google Trends ({geo})",
                "keyword": title,
                "search_volume_velocity": item.findtext(f"{ns}approx_traffic") or "High",
                "context_headline": news.strip(),
            }
        )
    return trends


def google_news_rss(
    keyword: str, *, limit: int = 6, fetch: FetchText | None = None
) -> list[dict[str, Any]]:
    """Berita 48 jam terakhir untuk niche tertentu (Google News RSS)."""
    keyword = keyword.strip()
    if not keyword:
        return []
    query = quote(f"{keyword} when:2d")
    xml_text = (fetch or default_fetch_text)(NEWS_RSS.format(query=query))
    news: list[dict[str, Any]] = []
    for item in _rss_items(xml_text)[:limit]:
        headline = (item.findtext("title") or "").strip()
        if not headline:
            continue
        news.append(
            {
                "source": "Google News 48h Spike",
                "keyword": keyword,
                "context_headline": headline,
                "published": (item.findtext("pubDate") or "").strip(),
                "url": (item.findtext("link") or "").strip()[:300],
            }
        )
    return news


def youtube_outliers(
    api_key: str,
    query: str,
    *,
    days: int = 14,
    limit: int = 8,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Video terbaru + Outlier Score (views ÷ subscriber channel)."""
    published_after = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    send = transport or default_transport
    search_url = "https://www.googleapis.com/youtube/v3/search?" + "&".join(
        [
            "part=snippet",
            f"q={quote(query)}",
            "type=video",
            "order=viewCount",
            f"publishedAfter={published_after}",
            f"maxResults={limit}",
            f"key={api_key}",
        ]
    )
    search = send("GET", search_url, None)
    entries = search.get("items") or []
    video_ids = [str((entry.get("id") or {}).get("videoId") or "") for entry in entries]
    video_ids = [video_id for video_id in video_ids if video_id]
    if not video_ids:
        return []

    channel_ids = sorted({str((entry.get("snippet") or {}).get("channelId") or "") for entry in entries})
    channel_ids = [channel_id for channel_id in channel_ids if channel_id]

    stats = send(
        "GET",
        "https://www.googleapis.com/youtube/v3/videos?part=statistics,snippet&id="
        + ",".join(video_ids)
        + f"&key={api_key}",
        None,
    )
    channels = send(
        "GET",
        "https://www.googleapis.com/youtube/v3/channels?part=statistics&id="
        + ",".join(channel_ids)
        + f"&key={api_key}",
        None,
    )
    subs = {
        str(channel.get("id")): max(int((channel.get("statistics") or {}).get("subscriberCount") or 0), 1)
        for channel in channels.get("items") or []
    }

    outliers: list[dict[str, Any]] = []
    for entry in stats.get("items") or []:
        statistics = entry.get("statistics") or {}
        snippet = entry.get("snippet") or {}
        views = int(statistics.get("viewCount") or 0)
        likes = int(statistics.get("likeCount") or 0)
        comments = int(statistics.get("commentCount") or 0)
        channel_subs = subs.get(str(snippet.get("channelId") or ""), 1000)
        outliers.append(
            {
                "title": snippet.get("title"),
                "channel": snippet.get("channelTitle"),
                "published_at": (snippet.get("publishedAt") or "")[:10],
                "views": views,
                "subscribers": channel_subs,
                "outlier_score": round(views / channel_subs, 2),
                "engagement_rate": round(((likes + comments) / max(views, 1)) * 100, 2),
                "url": f"https://youtu.be/{entry.get('id', '')}",
            }
        )
    outliers.sort(key=lambda item: item["outlier_score"], reverse=True)
    return outliers


def ytdlp_search(
    query: str, *, limit: int = 6, run: Runner | None = None
) -> list[dict[str, Any]]:
    """Fallback tanpa kuota API: `yt-dlp ytsearchN:... --dump-json`."""
    cmd = [
        "yt-dlp",
        f"ytsearch{limit}:{query}",
        "--dump-json",
        "--flat-playlist",
        "--no-warnings",
    ]
    try:
        completed = (run or _default_run)(cmd, timeout=60)
    except Exception as exc:  # noqa: BLE001 - yt-dlp belum tentu terpasang
        print(f"⚠️  [YT-DLP FALLBACK] {type(exc).__name__}: {exc}", flush=True)
        return []
    videos: list[dict[str, Any]] = []
    for line in str(getattr(completed, "stdout", "") or "").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        videos.append(
            {
                "title": data.get("title"),
                "channel": data.get("uploader"),
                "views": data.get("view_count"),
                "duration_sec": data.get("duration"),
                "url": data.get("url") or f"https://youtu.be/{data.get('id')}",
            }
        )
    return videos


def _default_run(cmd: list[str], *, timeout: float = 60) -> Any:
    import subprocess

    return subprocess.run(  # noqa: S603 - argumen dibangun sendiri, tanpa shell
        cmd, check=True, capture_output=True, text=True, timeout=timeout
    )


@dataclass
class RadarResult:
    niche: str = ""
    geo: str = "ID"
    engine: str = "none"
    trends: list[dict[str, Any]] = field(default_factory=list)
    news: list[dict[str, Any]] = field(default_factory=list)
    outliers: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)
    timestamp: str = ""

    @property
    def real(self) -> bool:
        return bool(self.trends or self.news or self.outliers) and self.engine != "none"

    def top_topics(self, limit: int = 5) -> list[str]:
        topics = [str(item.get("keyword") or "") for item in self.trends]
        topics += [str(item.get("title") or "") for item in self.outliers]
        topics += [str(item.get("context_headline") or "") for item in self.news]
        return [topic for topic in topics if topic][:limit]

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp_local": self.timestamp,
            "niche_queried": self.niche,
            "geo": self.geo,
            "youtube_scanner_engine": self.engine,
            "real_data": self.real,
            "google_trends_and_news_spikes": self.trends + self.news,
            "youtube_top_performing_videos": self.outliers,
            "attempts": self.attempts,
            "warnings": self.warnings,
        }


def scan_radar(
    settings: Settings,
    niche: str,
    *,
    geo: str = "ID",
    limit: int = 6,
    days: int = 14,
    transport: Transport | None = None,
    fetch: FetchText | None = None,
    run: Runner | None = None,
    now: datetime | None = None,
) -> RadarResult:
    """Gabungkan Google Trends + Google News + outlier YouTube (API → yt-dlp)."""
    niche = niche.strip()
    geo = (geo or "ID").strip().upper()[:2] or "ID"
    result = RadarResult(
        niche=niche,
        geo=geo,
        timestamp=(now or datetime.now()).astimezone().isoformat(timespec="seconds"),
    )

    try:
        result.trends = google_trends_rss(geo, limit=8, fetch=fetch)
        result.attempts.append(f"google trends: ok ({len(result.trends)} kueri)")
    except Exception as exc:  # noqa: BLE001
        result.attempts.append(f"google trends: GAGAL ({type(exc).__name__}: {str(exc)[:100]})")

    if niche:
        try:
            result.news = google_news_rss(niche, limit=6, fetch=fetch)
            result.attempts.append(f"google news: ok ({len(result.news)} berita)")
        except Exception as exc:  # noqa: BLE001
            result.attempts.append(f"google news: GAGAL ({type(exc).__name__}: {str(exc)[:100]})")

    if settings.youtube_api_key and niche:
        try:
            result.outliers = youtube_outliers(
                settings.youtube_api_key, niche, days=days, limit=limit, transport=transport, now=now
            )
            if result.outliers:
                result.engine = "YouTube Data API v3 (Outlier Ratio Analysis)"
            result.attempts.append(f"youtube api: ok ({len(result.outliers)} video)")
        except Exception as exc:  # noqa: BLE001
            result.attempts.append(f"youtube api: GAGAL ({type(exc).__name__}: {str(exc)[:100]})")
    elif not settings.youtube_api_key:
        result.warnings.append("YOUTUBE_API_KEY belum diset; memakai yt-dlp bila tersedia")

    if not result.outliers and niche:
        fallback = ytdlp_search(niche, limit=limit, run=run)
        if fallback:
            result.outliers = fallback
            result.engine = "yt-dlp Search Scraper (Fallback)"
            result.attempts.append(f"yt-dlp: ok ({len(fallback)} video)")
        else:
            result.attempts.append("yt-dlp: kosong / tidak tersedia")
    elif not niche:
        result.warnings.append("niche kosong: pencarian YouTube dilewati")

    if not result.real:
        result.warnings.append(
            "Radar tidak menemukan data real-time (jaringan/kuota). Jangan mengarang tren."
        )
    return result
