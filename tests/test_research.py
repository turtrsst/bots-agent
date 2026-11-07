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
