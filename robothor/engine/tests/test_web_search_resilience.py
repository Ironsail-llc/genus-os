"""web_search resilience: quality gate, browser fallback, API provider, places.

Measured on the production box (2026-09-11): from this egress IP SearXNG's
general engines answer with "access denied"/"CAPTCHA"/"too many requests"
except bing, and a local query came back with generic definition pages — i.e.
a search that "succeeds" and tells the agent nothing. The engine's browser
tool loads the same results page fine, so web_search now grades its own
SearXNG answer and drives the browser when the answer is useless.

Every network boundary here is mocked: no SearXNG, no Playwright, no Bing,
no Nominatim, no Brave.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import web

FIXTURES = Path(__file__).parent / "fixtures"

LOCAL_QUERY = "coworking private office near Springfield"
REMOTE_QUERY = "asyncio cancellation semantics python"


# ──────────────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("GET", "https://example.invalid/"),
                response=httpx.Response(self.status_code),
            )


class FakeHttp:
    """Routes GETs by URL substring; records every call."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    def client_factory(self, *args: Any, **kwargs: Any) -> FakeHttp:
        return self

    async def __aenter__(self) -> FakeHttp:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> FakeResponse:
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                if isinstance(value, FakeResponse):
                    return value
                return FakeResponse(value)
        raise AssertionError(f"unexpected outbound request: {url}")

    def hit(self, needle: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if needle in c["url"]]


class FakeBrowser:
    """Stand-in for the browser tool handler: records actions, serves rows."""

    def __init__(
        self,
        *,
        running: bool = False,
        pages: dict[str, Any] | None = None,
        html_pages: dict[str, str] | None = None,
    ) -> None:
        self.running = running
        self.pages = pages or {}
        self.html_pages = html_pages or {}
        self.actions: list[str] = []
        self.navigated: list[str] = []
        self.js: list[str] = []
        self.current = ""

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        action = args.get("action", "")
        self.actions.append(action)
        if action == "status":
            return {"status": "running" if self.running else "not_running"}
        if action == "start":
            self.running = True
            return {"status": "started"}
        if action == "stop":
            self.running = False
            return {"status": "stopped"}
        if action == "navigate":
            self.current = args.get("url") or args.get("targetUrl", "")
            self.navigated.append(self.current)
            return {"url": self.current, "title": "results"}
        if action == "evaluate":
            js = args.get("js", "")
            self.js.append(js)
            source = self.html_pages if "outerHTML" in js else self.pages
            for key, payload in source.items():
                if key in self.current:
                    return {"result": payload}
            return {"result": []}
        raise AssertionError(f"unexpected browser action: {action}")


def _rows(n: int, *, relevant: bool) -> list[dict[str, str]]:
    if relevant:
        return [
            {
                "title": f"Springfield coworking space #{i}",
                "url": f"https://example.com/springfield/{i}",
                "content": "Private office suites in Springfield.",
            }
            for i in range(n)
        ]
    return [
        {
            "title": f"Word of the day #{i}",
            "url": f"https://dictionary.example.com/{i}",
            "content": "A daily definition, unrelated to anything asked.",
        }
        for i in range(n)
    ]


BROWSER_ROWS = [
    {
        "title": "Springfield Coworking — Private Offices",
        "url": "https://springfield-cowork.example.com/",
        "snippet": "Private offices in Springfield from $400/month.",
    },
    {
        "title": "Desks & Suites of Springfield",
        "url": "https://desks.example.org/springfield",
        "snippet": "Coworking memberships near Springfield station.",
    },
    {
        "title": "Office space listings in Springfield",
        "url": "https://example.net/listings/springfield-office",
        "snippet": "Private office suites across Springfield.",
    },
]

NOMINATIM_PAYLOAD = [
    {
        "display_name": "Springfield Coworking, Main Street, Springfield",
        "osm_type": "node",
        "osm_id": 12345,
        "type": "coworking_space",
        "category": "office",
    },
    {
        "display_name": "Springfield Business Centre, Springfield",
        "osm_type": "way",
        "osm_id": 999,
        "type": "office",
        "category": "office",
    },
]


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(agent_id="test-agent", tenant_id="test-tenant")


@pytest.fixture(autouse=True)
def _searxng_cfg():
    class _Cfg:
        searxng_url = "http://searxng.test:8888"

    with patch.object(web, "_cfg", lambda: _Cfg()):
        yield


@pytest.fixture(autouse=True)
def _no_brave_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _reset_nominatim_throttle():
    web._nominatim_last = 0.0
    yield
    web._nominatim_last = 0.0


def _install(http: FakeHttp, browser: FakeBrowser):
    from robothor.engine.tools.handlers import browser as browser_mod

    return (
        patch.object(web.httpx, "AsyncClient", http.client_factory),
        patch.object(browser_mod, "_browser", browser),
    )


async def _search(
    http: FakeHttp, browser: FakeBrowser, args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    http_patch, browser_patch = _install(http, browser)
    with http_patch, browser_patch:
        return await web._web_search(args, ctx)


# ──────────────────────────────────────────────────────────────────────
# Quality gate → browser fallback
# ──────────────────────────────────────────────────────────────────────


async def test_generic_searxng_results_fall_back_to_browser(ctx: ToolContext) -> None:
    """Five results that mention nothing the operator asked for is not an answer."""
    http = FakeHttp({"searxng.test": {"results": _rows(5, relevant=False)}})
    browser = FakeBrowser(pages={"bing.com": BROWSER_ROWS})

    out = await _search(http, browser, {"query": REMOTE_QUERY}, ctx)

    assert out["provider"] == "browser"
    assert out["fallback_from"] == "searxng"
    assert [r["url"] for r in out["results"]] == [r["url"] for r in BROWSER_ROWS]
    assert browser.navigated and "bing.com/search" in browser.navigated[0]


async def test_searxng_error_falls_back_to_browser(ctx: ToolContext) -> None:
    http = FakeHttp({"searxng.test": httpx.ConnectError("connection refused")})
    browser = FakeBrowser(pages={"bing.com": BROWSER_ROWS})

    out = await _search(http, browser, {"query": REMOTE_QUERY}, ctx)

    assert out["provider"] == "browser"
    assert out["fallback_from"] == "searxng"
    assert out["fallback_reason"] == "error"


async def test_unresponsive_engines_are_reported_with_the_fallback(ctx: ToolContext) -> None:
    http = FakeHttp(
        {
            "searxng.test": {
                "results": [],
                "unresponsive_engines": [
                    ["google", "access denied"],
                    ["duckduckgo", "CAPTCHA"],
                ],
            }
        }
    )
    browser = FakeBrowser(pages={"bing.com": BROWSER_ROWS})

    out = await _search(http, browser, {"query": REMOTE_QUERY}, ctx)

    assert out["provider"] == "browser"
    assert out["fallback_reason"] == "engines_unresponsive"
    assert "google" in str(out["unresponsive_engines"])


async def test_relevant_searxng_results_are_kept(ctx: ToolContext) -> None:
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": NOMINATIM_PAYLOAD,
        }
    )
    browser = FakeBrowser(pages={"bing.com": BROWSER_ROWS})

    out = await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)

    assert out["provider"] == "searxng"
    assert "fallback_from" not in out
    assert browser.actions == []


# ──────────────────────────────────────────────────────────────────────
# Browser provider
# ──────────────────────────────────────────────────────────────────────


async def test_browser_session_is_stopped_when_the_search_started_it(ctx: ToolContext) -> None:
    http = FakeHttp({})
    browser = FakeBrowser(running=False, pages={"bing.com": BROWSER_ROWS})

    out = await _search(http, browser, {"query": REMOTE_QUERY, "provider": "browser"}, ctx)

    assert out["provider"] == "browser"
    assert "start" in browser.actions
    assert "stop" in browser.actions
    assert browser.running is False


async def test_browser_session_is_left_running_when_the_agent_started_it(
    ctx: ToolContext,
) -> None:
    http = FakeHttp({})
    browser = FakeBrowser(running=True, pages={"bing.com": BROWSER_ROWS})

    await _search(http, browser, {"query": REMOTE_QUERY, "provider": "browser"}, ctx)

    assert "start" not in browser.actions
    assert "stop" not in browser.actions
    assert browser.running is True


async def test_thin_bing_page_pulls_in_duckduckgo(ctx: ToolContext) -> None:
    http = FakeHttp({})
    browser = FakeBrowser(
        pages={
            "bing.com": BROWSER_ROWS[:1],
            "duckduckgo.com": BROWSER_ROWS[1:],
        }
    )

    out = await _search(http, browser, {"query": REMOTE_QUERY, "provider": "browser"}, ctx)

    assert any("duckduckgo.com/html" in u for u in browser.navigated)
    urls = [r["url"] for r in out["results"]]
    assert urls == [r["url"] for r in BROWSER_ROWS]  # bing first, DDG appended, deduped
    assert out["sources"] == ["bing", "duckduckgo"]


async def test_browser_results_are_capped_at_ten(ctx: ToolContext) -> None:
    many = [
        {"title": f"Result {i}", "url": f"https://example.com/{i}", "snippet": "x"}
        for i in range(25)
    ]
    http = FakeHttp({})
    browser = FakeBrowser(pages={"bing.com": many})

    out = await _search(http, browser, {"query": REMOTE_QUERY, "provider": "browser"}, ctx)

    assert len(out["results"]) == 10


# ──────────────────────────────────────────────────────────────────────
# HTML parsing (the selectors the injected JS uses, exercised on fixtures)
# ──────────────────────────────────────────────────────────────────────


def test_bing_html_fixture_parses_into_rows() -> None:
    rows = web._parse_bing_html((FIXTURES / "bing_results.html").read_text())

    assert [r["url"] for r in rows] == [
        "https://springfield-cowork.example.com/",
        "https://desks.example.org/springfield",
        "https://example.net/listings/springfield-office",
        "https://example.com/coworking-guide",
    ]
    assert rows[0]["title"] == "Springfield Coworking — Private Offices"
    assert "downtown Springfield" in rows[0]["snippet"]
    assert rows[1]["title"] == "Desks & Suites of Springfield"
    # the li.b_ad block is not an organic result
    assert all("ads.example.com" not in r["url"] for r in rows)


def test_ddg_html_fixture_parses_into_rows() -> None:
    rows = web._parse_ddg_html((FIXTURES / "ddg_results.html").read_text())

    assert [r["url"] for r in rows] == [
        "https://springfield-cowork.example.com/",  # unwrapped from /l/?uddg=
        "https://desks.example.org/springfield",
        "https://example.net/listings/springfield-office",
    ]
    assert rows[0]["title"] == "Springfield Coworking — Private Offices"
    assert "meeting rooms" in rows[0]["snippet"]
    assert all("ads.example.com" not in r["url"] for r in rows)


async def test_page_html_is_parsed_when_the_injected_js_returns_nothing(
    ctx: ToolContext,
) -> None:
    """A blocked/errored evaluate must not read as "no results on the page"."""
    browser = FakeBrowser(
        pages={"bing.com": []},
        html_pages={"bing.com": (FIXTURES / "bing_results.html").read_text()},
    )

    out = await _search(FakeHttp({}), browser, {"query": REMOTE_QUERY, "provider": "browser"}, ctx)

    assert [r["url"] for r in out["results"]][:2] == [
        "https://springfield-cowork.example.com/",
        "https://desks.example.org/springfield",
    ]
    assert any("outerHTML" in js for js in browser.js)


def test_extraction_js_uses_the_documented_selectors() -> None:
    assert "li.b_algo" in web._BING_EXTRACT_JS
    assert ".b_caption p" in web._BING_EXTRACT_JS
    assert ".result__a" in web._DDG_EXTRACT_JS
    assert ".result__snippet" in web._DDG_EXTRACT_JS


# ──────────────────────────────────────────────────────────────────────
# Brave
# ──────────────────────────────────────────────────────────────────────


BRAVE_PAYLOAD = {
    "web": {
        "results": [
            {
                "title": "Springfield Coworking",
                "url": "https://springfield-cowork.example.com/",
                "description": "Private offices in Springfield.",
            },
            {
                "title": "Desks & Suites",
                "url": "https://desks.example.org/springfield",
                "description": "Coworking near the station.",
            },
        ]
    }
}


async def test_brave_is_preferred_when_the_key_is_set(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    http = FakeHttp({"api.search.brave.com": BRAVE_PAYLOAD})
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": REMOTE_QUERY}, ctx)

    assert out["provider"] == "brave"
    assert out["results"][0]["url"] == "https://springfield-cowork.example.com/"
    assert out["results"][0]["snippet"] == "Private offices in Springfield."
    assert http.hit("api.search.brave.com")[0]["headers"]["X-Subscription-Token"] == "test-key"
    assert http.hit("searxng.test") == []


async def test_brave_is_absent_without_a_key(ctx: ToolContext) -> None:
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": NOMINATIM_PAYLOAD,
        }
    )
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)

    assert out["provider"] == "searxng"
    assert http.hit("brave.com") == []
    assert "error" not in out


async def test_brave_failure_falls_through_to_searxng(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    http = FakeHttp(
        {
            "api.search.brave.com": httpx.ConnectError("brave down"),
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": NOMINATIM_PAYLOAD,
        }
    )
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)

    assert out["provider"] == "searxng"
    assert "error" not in out


# ──────────────────────────────────────────────────────────────────────
# Places, via Nominatim
# ──────────────────────────────────────────────────────────────────────


async def test_local_query_appends_places(ctx: ToolContext) -> None:
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": NOMINATIM_PAYLOAD,
        }
    )
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)

    assert len(out["places"]) == 2
    assert out["places"][0]["title"].startswith("Springfield Coworking")
    assert out["places"][0]["url"] == "https://www.openstreetmap.org/node/12345"
    assert "coworking_space" in out["places"][0]["snippet"]
    ua = http.hit("nominatim.openstreetmap.org")[0]["headers"]["User-Agent"]
    assert "genus" in ua.lower()


async def test_non_local_query_never_touches_nominatim(ctx: ToolContext) -> None:
    http = FakeHttp({"searxng.test": {"results": _rows(4, relevant=True)}})
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": REMOTE_QUERY, "limit": 4}, ctx)

    assert http.hit("nominatim") == []
    assert "places" not in out


@pytest.mark.parametrize(
    "query,local",
    [
        ("coworking near Springfield", True),
        ("dentist nearby", True),
        ("hardware store in Springfield", True),
        ("pizza 02134", True),
        ("asyncio cancellation semantics python", False),
        ("what is a private office", False),
    ],
)
def test_local_query_detection(query: str, local: bool) -> None:
    assert web._looks_local(query) is local


async def test_nominatim_is_rate_limited(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nominatim's usage policy is one request per second — honour it."""
    monkeypatch.setattr(web, "_NOMINATIM_MIN_INTERVAL", 0.3)
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": NOMINATIM_PAYLOAD,
        }
    )
    browser = FakeBrowser()

    started = time.monotonic()
    await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)
    await _search(http, browser, {"query": "bakery near Springfield", "limit": 4}, ctx)
    elapsed = time.monotonic() - started

    assert len(http.hit("nominatim.openstreetmap.org")) == 2
    assert elapsed >= 0.3


# ──────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────


def test_schema_documents_providers_and_fallback() -> None:
    from robothor.engine.tools.schemas import get_engine_schemas

    schema = get_engine_schemas()["web_search"]["function"]
    desc = schema["description"].lower()
    assert "browser" in desc and "fallback" in desc
    props = schema["parameters"]["properties"]
    assert {"query", "limit", "provider"} <= set(props)
    assert {"searxng", "perplexity", "browser", "brave"} <= set(props["provider"]["enum"])
