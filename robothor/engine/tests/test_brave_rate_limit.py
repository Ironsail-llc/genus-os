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
        assert url == web.BRAVE_API_URL
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


# ── The month runs out (2026-09-14: free plan = 2,000 queries per 30 days) ──


_MONTH_GONE = {
    "x-ratelimit-limit": "1, 2000",
    "x-ratelimit-remaining": "0, 0",
    "x-ratelimit-reset": "1, 1398406",
}
_MONTH_LOW = {
    "x-ratelimit-limit": "1, 2000",
    "x-ratelimit-remaining": "1, 150",
    "x-ratelimit-reset": "1, 1398406",
}
_MONTH_FINE = {
    "x-ratelimit-limit": "1, 2000",
    "x-ratelimit-remaining": "1, 1965",
    "x-ratelimit-reset": "1, 1398406",
}


@pytest.fixture(autouse=True)
def _fresh_quota():
    web.QUOTA.reset()
    yield
    web.QUOTA.reset()


@pytest.mark.asyncio
async def test_a_monthly_quota_429_is_not_retried_and_the_next_call_skips_brave(
    monkeypatch, slept
) -> None:
    client = _Sequence([_Reply(429, headers=_MONTH_GONE)])
    monkeypatch.setattr(web.httpx, "AsyncClient", client)

    first = await web._brave_search("rockaway beach bakery", 3)
    second = await web._brave_search("rockaway beach bakery", 3)

    assert first is None and second is None
    # One dial, no backoff: a month-long 429 is not a one-second one.
    assert client.calls == 1
    assert slept == []


@pytest.mark.asyncio
async def test_the_search_tool_says_why_brave_was_skipped(monkeypatch, slept) -> None:
    client = _Sequence([_Reply(429, headers=_MONTH_GONE)])
    monkeypatch.setattr(web.httpx, "AsyncClient", client)

    async def _searxng_down(query: str, limit: int) -> dict[str, Any]:
        raise RuntimeError("down")

    monkeypatch.setattr(web, "_searxng_search", _searxng_down)
    monkeypatch.setattr(web, "_browser_fallback_enabled", lambda: False)
    ctx = ToolContext(agent_id="main", workspace="/tmp/ws")

    await web._web_search({"query": "rockaway beach bakery", "limit": 3}, ctx)
    out = await web._web_search({"query": "rockaway beach bakery", "limit": 3}, ctx)

    assert out["brave_skipped"] == "brave_monthly_quota_exhausted"
    assert out["brave_quota"]["exhausted"] is True
    assert "error" in out  # nothing else answered either — and it says so


@pytest.mark.asyncio
async def test_a_brave_answer_carries_the_quota_once_it_runs_low(monkeypatch, slept) -> None:
    client = _Sequence([_Reply(200, _HIT, headers=_MONTH_LOW)])
    monkeypatch.setattr(web.httpx, "AsyncClient", client)
    ctx = ToolContext(agent_id="main", workspace="/tmp/ws")

    out = await web._web_search({"query": "rockaway beach bakery", "limit": 3}, ctx)

    assert out["provider"] == "brave"
    assert out["brave_quota"]["month_remaining"] == 150


@pytest.mark.asyncio
async def test_a_healthy_quota_is_not_narrated_on_every_result(monkeypatch, slept) -> None:
    client = _Sequence([_Reply(200, _HIT, headers=_MONTH_FINE)])
    monkeypatch.setattr(web.httpx, "AsyncClient", client)
    ctx = ToolContext(agent_id="main", workspace="/tmp/ws")

    out = await web._web_search({"query": "rockaway beach bakery", "limit": 3}, ctx)

    assert out["provider"] == "brave"
    assert "brave_quota" not in out
