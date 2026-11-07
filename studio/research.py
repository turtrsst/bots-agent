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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
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
