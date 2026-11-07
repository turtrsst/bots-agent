from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from studio.config import Settings
from studio.research import fetch_trends, human_views
from studio.tools import StudioTools

YOUTUBE_TRENDING = {
    "items": [
        {
            "id": "abc123",
            "snippet": {
                "title": "5 Tools AI yang bikin kerja 10x cepat",
                "channelTitle": "Kanal Edukasi",
                "publishedAt": "2026-11-06T10:00:00Z",
            },
            "statistics": {"viewCount": "1200000"},
        }
    ]
}

TAVILY = {"results": [{"title": "Tren AI November 2026", "url": "https://example.com/tren"}]}


def fake_transport(responses: dict[str, Any], calls: list[tuple[str, str]]) -> Any:
    def transport(method: str, url: str, payload: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        calls.append((method, url))
        for needle, response in responses.items():
            if needle in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise RuntimeError(f"URL tidak ditangani: {url}")

    return transport


def test_without_credentials_is_honest(settings: Settings) -> None:
    result = fetch_trends(settings)
    assert result.provider == "none"
    assert not result.real
    assert "Tidak ada kredensial riset" in result.warning


def test_youtube_trending_parsed(settings: Settings) -> None:
    calls: list[tuple[str, str]] = []
    configured = settings.with_changes(youtube_api_key="KEY")
    result = fetch_trends(configured, transport=fake_transport({"videos?": YOUTUBE_TRENDING}, calls))

    assert result.real and result.provider == "youtube:trending"
    item = result.items[0]
    assert item.title.startswith("5 Tools AI")
    assert item.views == 1_200_000
    assert item.url == "https://youtu.be/abc123"
    assert "1.2jt views" in result.as_text()
    assert any("chart=mostPopular" in url for _, url in calls)


def test_topic_uses_keyword_search(settings: Settings) -> None:
    calls: list[tuple[str, str]] = []
    configured = settings.with_changes(youtube_api_key="KEY")
    result = fetch_trends(
        configured, "workflow editing", transport=fake_transport({"search?": {"items": []}}, calls)
    )
    assert not result.real  # search kosong → tidak ada data
    assert calls and "q=workflow editing" in calls[0][1]
    assert "kosong" in result.attempts[0]


def test_falls_back_to_tavily_when_youtube_fails(settings: Settings) -> None:
    calls: list[tuple[str, str]] = []
    configured = settings.with_changes(youtube_api_key="KEY", tavily_api_key="TVLY")
    transport = fake_transport(
        {"videos?": RuntimeError("quota habis"), "api.tavily.com": TAVILY}, calls
    )

    result = fetch_trends(configured, transport=transport)

    assert result.provider == "tavily"
    assert result.items[0].title == "Tren AI November 2026"
    assert "GAGAL" in result.attempts[0]


def test_all_providers_fail_reports_attempts(settings: Settings) -> None:
    configured = settings.with_changes(youtube_api_key="KEY", serpapi_api_key="SERP")
    transport = fake_transport(
        {"googleapis.com": RuntimeError("403"), "serpapi.com": RuntimeError("401")}, []
    )
    result = fetch_trends(configured, topic="ai", transport=transport)
    assert not result.real
    assert result.provider == "none"
    assert "403" in result.warning and "401" in result.warning


def test_human_views() -> None:
    assert human_views(None) == ""
    assert human_views(500) == "500 views"
    assert human_views(12_300) == "12.3rb views"
    assert human_views(2_000_000) == "2.0jt views"


def test_tool_saves_artifact_and_reports_real_data(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings.with_changes(youtube_api_key="KEY")
    tools = StudioTools(configured)
    monkeypatch.setattr(
        "studio.research.default_transport",
        fake_transport({"googleapis.com": YOUTUBE_TRENDING}, []),
    )

    text = tools.research_current_trends("")

    assert text.startswith("TREN NYATA")
    assert "5 Tools AI" in text
    artifacts = sorted(configured.paths.workspace.glob("trend_research_*.json"))
    assert artifacts
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["real_data"] is True
    assert payload["items"][0]["title"].startswith("5 Tools AI")


def test_tool_warns_instead_of_inventing(settings: Settings) -> None:
    tools = StudioTools(settings)
    text = tools.research_current_trends("tema apa saja")
    assert text.startswith("RISET TIDAK TERSEDIA")
    assert "jangan mengarang" in text.lower()
    assert list(Path(settings.paths.workspace).glob("trend_research_*.json"))


# ------------------------------------------------- radar real-time Vito
TRENDS_XML = """<?xml version="1.0"?><rss xmlns:ht="https://trends.google.com/trending/rss"><channel>
<item><title>kecerdasan buatan</title><ht:approx_traffic>20000+</ht:approx_traffic>
<ht:news_item><ht:news_item_title>AI dipakai pabrik</ht:news_item_title></ht:news_item></item>
<item><title>startup lokal</title><ht:approx_traffic>5000+</ht:approx_traffic></item>
</channel></rss>"""

NEWS_XML = """<?xml version="1.0"?><rss><channel>
<item><title>Pabrik AI beroperasi tanpa manusia</title><pubDate>Fri, 07 Nov 2026 01:00:00 GMT</pubDate>
<link>https://news.example/ai-pabrik</link></item>
</channel></rss>"""

SEARCH = {"items": [{"id": {"videoId": "v1"}, "snippet": {"channelId": "c1"}}]}
VIDEOS = {
    "items": [
        {
            "id": "v1",
            "snippet": {"title": "Kantor AI tanpa manusia", "channelTitle": "Kanal Kecil",
                        "channelId": "c1", "publishedAt": "2026-11-05T00:00:00Z"},
            "statistics": {"viewCount": "48000", "likeCount": "2400", "commentCount": "300"},
        }
    ]
}
CHANNELS = {"items": [{"id": "c1", "statistics": {"subscriberCount": "10000"}}]}


def test_google_trends_rss_parses_items() -> None:
    from studio.research import google_trends_rss

    trends = google_trends_rss("ID", fetch=lambda _url: TRENDS_XML)

    assert [item["keyword"] for item in trends] == ["kecerdasan buatan", "startup lokal"]
    assert trends[0]["search_volume_velocity"] == "20000+"
    assert trends[0]["context_headline"] == "AI dipakai pabrik"


def test_google_news_rss_parses_items() -> None:
    from studio.research import google_news_rss

    news = google_news_rss('AI "pabrik"', fetch=lambda _url: NEWS_XML)

    assert news[0]["context_headline"] == "Pabrik AI beroperasi tanpa manusia"
    assert news[0]["url"].startswith("https://news.example")


def test_google_trends_rss_ignores_broken_xml() -> None:
    from studio.research import google_trends_rss

    assert google_trends_rss("ID", fetch=lambda _url: "<rss><channel>") == []


def test_youtube_outliers_score(settings: Settings) -> None:
    from studio.research import youtube_outliers

    calls: list[tuple[str, str]] = []
    transport = fake_transport({"youtube/v3/search": SEARCH, "youtube/v3/videos": VIDEOS,
                                "youtube/v3/channels": CHANNELS}, calls)

    outliers = youtube_outliers("key", "AI pabrik", transport=transport)

    assert outliers[0]["outlier_score"] == 4.8  # 48.000 views / 10.000 subs
    assert outliers[0]["engagement_rate"] == 5.62
    assert outliers[0]["url"] == "https://youtu.be/v1"
    assert any("youtube/v3/channels" in url for _method, url in calls)


def test_ytdlp_fallback_parses_json_lines() -> None:
    from studio.research import ytdlp_search

    class Done:
        stdout = (
            '{"title": "Video A", "uploader": "Kanal", "view_count": 1000, "id": "aaa"}\n'
            "bukan json\n"
            '{"title": "Video B", "uploader": "Kanal 2", "view_count": 500, "id": "bbb"}\n'
        )

    videos = ytdlp_search("AI", run=lambda cmd, timeout=60: Done())

    assert [video["title"] for video in videos] == ["Video A", "Video B"]
    assert videos[0]["url"] == "https://youtu.be/aaa"


def test_scan_radar_combines_sources(settings: Settings) -> None:
    from studio.research import scan_radar

    configured = settings.with_changes(youtube_api_key="key")
    transport = fake_transport({"youtube/v3/search": SEARCH, "youtube/v3/videos": VIDEOS,
                                "youtube/v3/channels": CHANNELS}, [])

    radar = scan_radar(
        configured, "AI pabrik", transport=transport,
        fetch=lambda url: TRENDS_XML if "trends.google" in url else NEWS_XML,
    )

    assert radar.real
    assert radar.engine.startswith("YouTube Data API v3")
    assert len(radar.trends) == 2 and len(radar.news) == 1 and len(radar.outliers) == 1
    assert radar.top_topics()[0] == "kecerdasan buatan"
    payload = radar.as_dict()
    assert payload["real_data"] is True
    assert payload["youtube_top_performing_videos"][0]["outlier_score"] == 4.8


def test_scan_radar_falls_back_to_ytdlp(settings: Settings) -> None:
    from studio.research import scan_radar

    class Done:
        stdout = '{"title": "Video yt-dlp", "uploader": "K", "view_count": 10, "id": "z"}\n'

    radar = scan_radar(
        settings, "AI", fetch=lambda _url: TRENDS_XML, run=lambda cmd, timeout=60: Done()
    )

    assert radar.engine == "yt-dlp Search Scraper (Fallback)"
    assert radar.outliers[0]["title"] == "Video yt-dlp"
    assert any("YOUTUBE_API_KEY belum diset" in warning for warning in radar.warnings)


def test_scan_radar_is_honest_without_network(settings: Settings) -> None:
    from studio.research import scan_radar

    def broken(_url: str) -> str:
        raise OSError("jaringan mati")

    radar = scan_radar(settings, "AI", fetch=broken, run=lambda *a, **k: None)

    assert radar.real is False
    assert any("Jangan mengarang tren" in warning for warning in radar.warnings)


def _offline_radar(_settings, niche: str, **_kwargs):
    from studio.research import RadarResult

    return RadarResult(
        niche=niche,
        engine="none",
        attempts=["google trends: GAGAL (OSError: jaringan mati)", "yt-dlp: kosong / tidak tersedia"],
        warnings=["Radar tidak menemukan data real-time. Jangan mengarang tren."],
    )


def test_radar_tool_writes_artifact_and_reports_honestly(settings: Settings) -> None:
    tools = StudioTools(settings, radar=_offline_radar)
    result = tools.scan_viral_trends_and_youtube_outliers("AI pabrik", "ID")

    # Tanpa jaringan & tanpa API key: harus jujur, bukan mengarang tren.
    assert result.startswith("RISET TIDAK TERSEDIA")
    artifact = settings.paths.workspace / "vito_realtime_radar.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["real_data"] is False
    assert payload["niche_queried"] == "AI pabrik"


def test_radar_tool_returns_real_data_when_available(settings: Settings) -> None:
    from studio.research import RadarResult

    def radar(_settings, niche: str, **_kwargs):
        return RadarResult(
            niche=niche,
            engine="YouTube Data API v3 (Outlier Ratio Analysis)",
            trends=[{"keyword": "kecerdasan buatan", "source": "Google Trends (ID)"}],
            outliers=[{"title": "Kantor AI", "outlier_score": 4.8}],
        )

    result = StudioTools(settings, radar=radar).scan_viral_trends_and_youtube_outliers("AI", "ID")

    assert result.startswith("DATA RISET REAL-TIME DITEMUKAN")
    assert "kecerdasan buatan" in result
    assert "4.8" in result


def test_radar_tool_rejects_empty_niche(settings: Settings) -> None:
    tools = StudioTools(settings, radar=_offline_radar)
    assert tools.scan_viral_trends_and_youtube_outliers("   ").startswith("ERROR RADAR")
