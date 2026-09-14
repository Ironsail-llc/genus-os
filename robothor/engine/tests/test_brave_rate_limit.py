"""A Brave rate limit is a pause, not an outage.

Live, 2026-09-14: the Brave key went into the engine environment and the very
first pair of searches a second apart hit ``429 Too Many Requests`` (the free
tier allows one request per second). The provider "fell through" to the
scraped SearXNG rung, which was itself dead, and the operator got Wikipedia
for a bakery. A 429 from the one reliable provider must be retried after the
interval it names, never traded for garbage.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import web


class _Reply:
    def __init__(self, status: int, payload: Any = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("GET", web.BRAVE_API_URL),
                response=httpx.Response(self.status_code, headers=self.headers),
            )


class _Sequence:
    """An AsyncClient stand-in answering the Brave URL from a scripted list."""

    def __init__(self, replies: list[_Reply]) -> None:
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, *a: Any, **k: Any) -> _Sequence:
        return self

    async def __aenter__(self) -> _Sequence:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _Reply:
        assert "api.search.brave.com" in url
        self.calls += 1
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


_HIT = {
    "web": {
        "results": [
            {
                "title": "Rockaway Beach Bakery",
                "url": "https://example.com/rbb",
                "description": "Bakery in Rockaway Park, NY",
            }
        ]
    }
}


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(web.asyncio, "sleep", _sleep)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key-not-real")
    return waits


@pytest.mark.asyncio
async def test_a_429_is_retried_after_the_interval_the_server_names(monkeypatch, slept) -> None:
    client = _Sequence([_Reply(429, headers={"Retry-After": "2"}), _Reply(200, _HIT)])
    monkeypatch.setattr(web.httpx, "AsyncClient", client)

    rows = await web._brave_search("rockaway beach bakery", 3)

    assert rows and rows[0]["url"] == "https://example.com/rbb"
    assert client.calls == 2
    assert slept == [2.0]


@pytest.mark.asyncio
async def test_a_429_without_retry_after_waits_the_free_tier_second(monkeypatch, slept) -> None:
    client = _Sequence([_Reply(429), _Reply(200, _HIT)])
    monkeypatch.setattr(web.httpx, "AsyncClient", client)

    rows = await web._brave_search("rockaway beach bakery", 3)

    assert rows
    assert slept and 1.0 <= slept[0] <= 2.0


@pytest.mark.asyncio
async def test_a_persistent_429_gives_up_after_bounded_retries(monkeypatch, slept) -> None:
    client = _Sequence([_Reply(429)])
    monkeypatch.setattr(web.httpx, "AsyncClient", client)

    rows = await web._brave_search("rockaway beach bakery", 3)

    assert rows is None
    assert client.calls == web._BRAVE_MAX_ATTEMPTS
    assert len(slept) == web._BRAVE_MAX_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_other_http_errors_are_not_retried(monkeypatch, slept) -> None:
    client = _Sequence([_Reply(401)])
    monkeypatch.setattr(web.httpx, "AsyncClient", client)

    rows = await web._brave_search("rockaway beach bakery", 3)

    assert rows is None
    assert client.calls == 1
    assert slept == []


@pytest.mark.asyncio
async def test_the_search_tool_reports_brave_after_a_retried_429(monkeypatch, slept) -> None:
    client = _Sequence([_Reply(429), _Reply(200, _HIT)])
    monkeypatch.setattr(web.httpx, "AsyncClient", client)
    ctx = ToolContext(agent_id="main", workspace="/tmp/ws")

    out = await web._web_search({"query": "rockaway beach bakery", "limit": 3}, ctx)

    assert out["provider"] == "brave"
    assert out["count"] == 1
    assert "fallback_from" not in out
