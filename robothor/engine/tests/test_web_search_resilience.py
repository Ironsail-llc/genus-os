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

import asyncio
import contextlib
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import quote_plus, urlparse

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


class ByParams:
    """A route that answers the geocode (limit=1) and the free-text lookup differently."""

    def __init__(self, *, geocode: Any, free_text: Any) -> None:
        self.geocode = geocode
        self.free_text = free_text

    def pick(self, params: dict[str, Any]) -> Any:
        return self.geocode if params.get("limit") == 1 else self.free_text


class FakeHttp:
    """Routes GETs and POSTs by URL substring; records every call."""

    by_params = ByParams

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
        return self._route("GET", url, params=params, headers=headers)

    async def post(
        self,
        url: str,
        content: str | None = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> FakeResponse:
        return self._route("POST", url, headers=headers, body=content or data)

    def _route(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params or {},
                "headers": headers or {},
                "body": body,
            }
        )
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                if isinstance(value, FakeResponse):
                    return value
                if isinstance(value, ByParams):
                    return FakeResponse(value.pick(params or {}))
                return FakeResponse(value)
        raise AssertionError(f"unexpected outbound request: {url}")

    def hit(self, needle: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if needle in c["url"]]


class FakeBrowser:
    """Stand-in for browser.py's session helpers.

    Carries the state a real session carries — the page the agent is on and the
    @N element registry bound to it — so a test can prove a background search
    left both alone.
    """

    AGENT_PAGE = "https://intranet.example.com/form"

    def __init__(
        self,
        *,
        running: bool = False,
        pages: dict[str, Any] | None = None,
        html_pages: dict[str, str] | None = None,
        statuses: dict[str, int] | None = None,
        start_error: str = "",
        fetch_error: str = "",
        navigate_delay: float = 0.0,
    ) -> None:
        self.fetch_error = fetch_error
        self.running = running
        self.pages = pages or {}
        self.html_pages = html_pages or {}
        self.statuses = statuses or {}
        self.start_error = start_error
        self.navigate_delay = navigate_delay
        # State the agent owns; a background fetch must not touch either.
        self.agent_page_url = self.AGENT_PAGE
        self.element_registry: dict[int, str] = {1: "textbox 'Name'"}
        self.actions: list[str] = []
        self.navigated: list[str] = []
        self.js: list[str] = []
        self.waits: list[tuple[str, str]] = []
        self.open_tabs = 0
        self.max_open_tabs = 0

    def install(self, monkeypatch: pytest.MonkeyPatch | None = None) -> tuple[Any, ...]:
        from robothor.engine.tools.handlers import browser as browser_mod

        return (
            patch.object(browser_mod, "ensure_session", self.ensure_session),
            patch.object(browser_mod, "close_session", self.close_session),
            patch.object(browser_mod, "isolated_fetch", self.isolated_fetch),
        )

    async def ensure_session(self, ctx: ToolContext) -> tuple[bool, str]:
        self.actions.append("ensure_session")
        if self.running:
            return False, ""
        if self.start_error:
            return False, self.start_error
        self.running = True
        self.actions.append("start")
        return True, ""

    async def close_session(self, ctx: ToolContext) -> None:
        self.actions.append("stop")
        self.running = False

    async def isolated_fetch(
        self,
        ctx: ToolContext,
        url: str,
        js: str,
        html_js: str = "",
        timeout_ms: int = 20000,
        wait_selector: str = "",
    ) -> dict[str, Any]:
        self.actions.append("isolated_fetch")
        self.navigated.append(url)
        self.js.append(js)
        self.waits.append((url, wait_selector))
        self.open_tabs += 1
        self.max_open_tabs = max(self.max_open_tabs, self.open_tabs)
        try:
            if self.navigate_delay:
                await asyncio.sleep(self.navigate_delay)
            if self.fetch_error:
                return {
                    "status": None,
                    "url": url,
                    "result": None,
                    "html": None,
                    "error": self.fetch_error,
                }
            status = 200
            for key, code in self.statuses.items():
                if key in url:
                    status = code
            result: Any = []
            for key, payload in self.pages.items():
                if key in url:
                    result = payload
            html = None
            if html_js and not result:
                self.js.append(html_js)
                for key, payload in self.html_pages.items():
                    if key in url:
                        html = payload
            return {"status": status, "url": url, "result": result, "html": html}
        finally:
            self.open_tabs -= 1


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

# What the browser actually brings back for REMOTE_QUERY — rows that answer it.
ASYNCIO_BROWSER_ROWS = [
    {
        "title": "Cancellation semantics in asyncio",
        "url": "https://docs.example.org/asyncio/cancellation",
        "snippet": "How asyncio delivers cancellation to awaited tasks in Python.",
    },
    {
        "title": "Python asyncio task cancellation",
        "url": "https://example.com/python-asyncio-cancel",
        "snippet": "Cancellation arrives as CancelledError at the next await point.",
    },
    {
        "title": "Structured concurrency and cancel scopes",
        "url": "https://example.net/structured-cancellation",
        "snippet": "Cancellation semantics of nested scopes in Python asyncio.",
    },
]

# The exact query the box probed: eight on-topic-looking pages that explain what
# coworking is, and not one of them mentions the place the operator asked about.
JAMAICA_QUERY = "coworking private office Jamaica Queens"
GENERIC_COWORKING_ROWS = [
    {
        "title": f"What is coworking? A definition ({i})",
        "url": f"https://dictionary.example.com/coworking/{i}",
        "content": "Coworking spaces explained: private office vs hot desk vs dedicated desk.",
    }
    for i in range(8)
]

# What the browser brings back for it: listings that name the place.
JAMAICA_BROWSER_ROWS = [
    {
        "title": "Private offices in Jamaica, Queens",
        "url": "https://example.com/jamaica-queens-offices",
        "snippet": "Coworking and private office suites on Jamaica Avenue, Queens.",
    },
    {
        "title": "Queens coworking directory",
        "url": "https://example.org/queens-coworking",
        "snippet": "Desks and private offices across Queens, including Jamaica.",
    },
]

# What Bing hands this box back for JAMAICA_QUERY: seven rows about the city the
# egress IP sits in, not one of them about the place that was typed.
GEOLOCATED_BING_ROWS = [
    {
        "title": f"Coworking space in Riverport #{i}",
        "url": f"https://riverport-cowork.example.com/{i}",
        "snippet": "Private offices and hot desks in downtown Riverport.",
    }
    for i in range(7)
]

# Enough place-naming rows that the count rule alone would stop the chain.
JAMAICA_BING_ROWS = JAMAICA_BROWSER_ROWS + [
    {
        "title": "Jamaica Avenue office suites",
        "url": "https://example.net/jamaica-ave-suites",
        "snippet": "Private offices to rent in Jamaica, Queens.",
    },
]

GEOCODE_PAYLOAD = [
    {
        "lat": "42.1015",
        "lon": "-72.5898",
        "display_name": "Springfield, Example County",
        "osm_type": "relation",
        "osm_id": 1,
    }
]

OVERPASS_PAYLOAD = {
    "elements": [
        {
            "type": "node",
            "id": 111,
            "tags": {
                "name": "Springfield Coworking",
                "addr:housenumber": "12",
                "addr:street": "Main Street",
                "addr:city": "Springfield",
            },
        },
        {
            "type": "way",
            "id": 222,
            "center": {"lat": 42.1, "lon": -72.6},
            "tags": {
                "name": "Desks & Suites",
                "addr:street": "Station Road",
                "addr:city": "Springfield",
            },
        },
    ]
}

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
def _reset_places_throttle(monkeypatch: pytest.MonkeyPatch):
    """No courtesy sleeping in unit tests — the throttle has its own test,
    which sets its own interval after this fixture has run."""
    monkeypatch.setattr(web, "_PLACES_MIN_INTERVAL", 0.0)
    web._places_last = 0.0
    yield
    web._places_last = 0.0


@pytest.fixture(autouse=True)
def _browser_tool_allowed():
    """Default: the browser tool is permitted. Denial has its own test."""
    with patch.object(web, "_browser_permission_error", _allow_browser):
        yield


async def _allow_browser(ctx: ToolContext) -> str:
    return ""


async def _search(
    http: FakeHttp, browser: FakeBrowser, args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    with patch.object(web.httpx, "AsyncClient", http.client_factory):
        with contextlib.ExitStack() as stack:
            for p in browser.install():
                stack.enter_context(p)
            return await web._web_search(args, ctx)


# ──────────────────────────────────────────────────────────────────────
# Quality gate → browser fallback
# ──────────────────────────────────────────────────────────────────────


async def test_generic_searxng_results_fall_back_to_browser(ctx: ToolContext) -> None:
    """Five results that mention nothing the operator asked for is not an answer."""
    http = FakeHttp({"searxng.test": {"results": _rows(5, relevant=False)}})
    browser = FakeBrowser(pages={"bing.com": ASYNCIO_BROWSER_ROWS})

    out = await _search(http, browser, {"query": REMOTE_QUERY}, ctx)

    assert out["provider"] == "browser"
    assert out["fallback_from"] == "searxng"
    assert [r["url"] for r in out["results"]] == [r["url"] for r in ASYNCIO_BROWSER_ROWS]
    assert browser.navigated and "bing.com/search" in browser.navigated[0]


async def test_generic_pages_that_share_the_common_term_still_fall_back(
    ctx: ToolContext,
) -> None:
    """Eight "what is coworking" pages are not an answer about Jamaica, Queens.

    Every row contains the query's commonest word, which is exactly why a
    "does any term appear" gate passed this on the box. The place terms appear
    in none of them, and that is the signal that matters.
    """
    http = FakeHttp(
        {
            "searxng.test": {"results": GENERIC_COWORKING_ROWS},
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser(pages={"bing.com": JAMAICA_BROWSER_ROWS})

    out = await _search(http, browser, {"query": JAMAICA_QUERY, "limit": 8}, ctx)

    assert out["provider"] == "browser"
    assert out["fallback_from"] == "searxng"
    assert out["fallback_reason"] == "missing_place_terms"


def test_saturated_common_term_carries_no_signal() -> None:
    rows = [
        web._row(r["title"], r["url"], r["content"])  # type: ignore[index]
        for r in GENERIC_COWORKING_ROWS
    ]
    assert web._grade_results(rows, JAMAICA_QUERY) == "missing_place_terms"


def test_results_that_mention_every_term_are_accepted() -> None:
    rows = [web._row(r["title"], r["url"], r["content"]) for r in _rows(4, relevant=True)]
    assert web._grade_results(rows, LOCAL_QUERY) == ""


async def test_a_single_result_can_answer_a_limit_one_search(ctx: ToolContext) -> None:
    """Demanding a place term in two rows is unmeetable when only one was asked for."""
    http = FakeHttp(
        {
            "searxng.test": {
                "results": [
                    {
                        "title": "Springfield coworking",
                        "url": "https://example.com/springfield",
                        "content": "Private office suites in Springfield.",
                    }
                ]
            },
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser(pages={"bing.com": BROWSER_ROWS})

    out = await _search(http, browser, {"query": LOCAL_QUERY, "limit": 1}, ctx)

    assert out["provider"] == "searxng"
    assert browser.actions == []  # no needless fallback


LONG_LOCAL_QUERY = "coworking private office with meeting rooms and parking Jamaica Queens"


def test_grading_does_not_log_the_whole_query(caplog: pytest.LogCaptureFixture) -> None:
    """A query is operator data; the log gets a truncated echo, not the lot."""
    rows = [web._row(r["title"], r["url"], r["content"]) for r in GENERIC_COWORKING_ROWS]

    with caplog.at_level("INFO", logger="robothor.engine.tools.handlers.web"):
        assert web._grade_results(rows, LONG_LOCAL_QUERY) == "missing_place_terms"

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert logged  # it does explain itself
    assert LONG_LOCAL_QUERY not in logged
    assert "…" in logged


async def test_browser_failures_do_not_log_the_whole_query(
    ctx: ToolContext, caplog: pytest.LogCaptureFixture
) -> None:
    """The search URL carries the query — redact it before it reaches a log."""
    http = FakeHttp({"searxng.test": httpx.ConnectError("down")})
    browser = FakeBrowser(fetch_error="Navigation failed: net::ERR_ABORTED")

    with caplog.at_level("WARNING", logger="robothor.engine.tools.handlers.web"):
        await _search(http, browser, {"query": LONG_LOCAL_QUERY}, ctx)

    logged = " ".join(r.getMessage() for r in caplog.records)
    logged_hosts = {urlparse(u).hostname for u in re.findall(r"https?://[^\s'\"]+", logged)}
    assert {"www.bing.com"} <= logged_hosts  # which source failed is still legible
    assert quote_plus(LONG_LOCAL_QUERY) not in logged
    assert LONG_LOCAL_QUERY not in logged


def test_redacted_url_keeps_the_target_and_trims_the_query() -> None:
    redacted = web._redact_query(f"https://www.bing.com/search?q={quote_plus(LONG_LOCAL_QUERY)}")

    assert redacted.startswith("https://www.bing.com/search?q=coworking private office")
    assert redacted.endswith("…")
    assert len(redacted) < len(LONG_LOCAL_QUERY) + 50


def test_results_about_something_else_are_rejected() -> None:
    rows = [web._row(r["title"], r["url"], r["content"]) for r in _rows(5, relevant=False)]
    assert web._grade_results(rows, REMOTE_QUERY) == "low_relevance"


async def test_searxng_error_falls_back_to_browser(ctx: ToolContext) -> None:
    http = FakeHttp({"searxng.test": httpx.ConnectError("connection refused")})
    browser = FakeBrowser(pages={"bing.com": ASYNCIO_BROWSER_ROWS})

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
    browser = FakeBrowser(pages={"bing.com": ASYNCIO_BROWSER_ROWS})

    out = await _search(http, browser, {"query": REMOTE_QUERY}, ctx)

    assert out["provider"] == "browser"
    assert out["fallback_reason"] == "engines_unresponsive"
    assert "google" in str(out["unresponsive_engines"])


async def test_relevant_searxng_results_are_kept(ctx: ToolContext) -> None:
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
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
    assert out["sources"] == ["bing", "startpage:empty", "duckduckgo"]


async def test_duckduckgo_challenge_page_is_reported_as_blocked(ctx: ToolContext) -> None:
    """DDG answers this box's browser with HTTP 202 and a challenge form.

    Contributing zero rows silently is how "DuckDuckGo had nothing" gets
    mistaken for "there is nothing"; say the source was blocked instead.
    """
    browser = FakeBrowser(
        pages={"bing.com": BROWSER_ROWS[:1]},
        html_pages={"duckduckgo.com": (FIXTURES / "ddg_challenge.html").read_text()},
        statuses={"duckduckgo.com": 202},
    )

    out = await _search(FakeHttp({}), browser, {"query": REMOTE_QUERY, "provider": "browser"}, ctx)

    assert "duckduckgo:blocked" in out["sources"]
    assert "bing" in out["sources"]
    assert len(out["results"]) == 1


async def test_bing_page_without_parsable_rows_reports_selector_drift(
    ctx: ToolContext, caplog: pytest.LogCaptureFixture
) -> None:
    """A 200 with nothing parsable means our selectors moved — say so loudly."""
    browser = FakeBrowser(
        pages={"bing.com": []},
        html_pages={"bing.com": "<html><body><div id='b_results'></div></body></html>"},
        statuses={"bing.com": 200, "duckduckgo.com": 200},
    )

    with caplog.at_level("WARNING", logger="robothor.engine.tools.handlers.web"):
        out = await _search(
            FakeHttp({}), browser, {"query": REMOTE_QUERY, "provider": "browser"}, ctx
        )

    assert out["results"] == []
    assert out["fallback_reason"] == "browser_parse_empty"
    assert any("parse" in r.message.lower() for r in caplog.records)


@pytest.mark.parametrize("limit,expected", [(2, 3), (5, 5), (25, 10)])
async def test_browser_honours_limit_up_to_ten(ctx: ToolContext, limit: int, expected: int) -> None:
    """Honour what was asked for, floor at the two-source threshold, cap at 10."""
    many = [
        {"title": f"Result {i}", "url": f"https://example.com/{i}", "snippet": "x"}
        for i in range(25)
    ]
    http = FakeHttp({})
    browser = FakeBrowser(pages={"bing.com": many})

    out = await _search(
        http, browser, {"query": REMOTE_QUERY, "provider": "browser", "limit": limit}, ctx
    )

    assert len(out["results"]) == expected


async def test_fallback_leaves_the_agents_own_page_and_refs_alone(ctx: ToolContext) -> None:
    """A session holds ONE page. Navigating it would move the agent's document
    out from under the @N refs it is mid-way through using."""
    http = FakeHttp({"searxng.test": {"results": _rows(5, relevant=False)}})
    browser = FakeBrowser(running=True, pages={"bing.com": ASYNCIO_BROWSER_ROWS})

    out = await _search(http, browser, {"query": REMOTE_QUERY}, ctx)

    assert out["provider"] == "browser"
    assert browser.agent_page_url == FakeBrowser.AGENT_PAGE
    assert browser.element_registry == {1: "textbox 'Name'"}
    assert browser.open_tabs == 0  # every tab the search opened was closed
    assert browser.max_open_tabs == 1


async def test_fallback_is_skipped_when_the_browser_tool_is_denied(
    ctx: ToolContext,
) -> None:
    """web_search must not become a way around the browser tool's permissions."""
    http = FakeHttp({"searxng.test": {"results": _rows(5, relevant=True)}})
    browser = FakeBrowser(pages={"bing.com": ASYNCIO_BROWSER_ROWS})
    audits: list[dict[str, Any]] = []

    async def _deny(_ctx: ToolContext) -> str:
        return "Role 'service' is not permitted to use tool 'browser'"

    with patch.object(web, "_browser_permission_error", _deny):
        with patch.object(
            web,
            "_audit_tool_call",
            lambda *a, **k: audits.append({"args": a, "kwargs": k}),
        ):
            out = await _search(http, browser, {"query": REMOTE_QUERY}, ctx)

    assert browser.actions == []  # the browser was never touched
    assert out["provider"] == "searxng"
    assert out["fallback_reason"] == "browser_denied"
    assert out["degraded"] == "low_relevance"
    assert audits and audits[0]["kwargs"]["status"] == "denied"


async def test_allowed_browser_fallback_is_audited(ctx: ToolContext) -> None:
    http = FakeHttp({"searxng.test": {"results": _rows(5, relevant=False)}})
    browser = FakeBrowser(pages={"bing.com": ASYNCIO_BROWSER_ROWS})
    audits: list[dict[str, Any]] = []

    with patch.object(
        web, "_audit_tool_call", lambda *a, **k: audits.append({"args": a, "kwargs": k})
    ):
        out = await _search(http, browser, {"query": REMOTE_QUERY}, ctx)

    assert out["provider"] == "browser"
    assert audits and audits[0]["args"][0] == "browser"
    assert audits[0]["kwargs"].get("status", "ok") == "ok"


async def test_a_wedged_browser_does_not_hang_the_search(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web, "_BROWSER_SEARCH_TIMEOUT", 0.05)
    http = FakeHttp({"searxng.test": {"results": _rows(5, relevant=True)}})
    browser = FakeBrowser(pages={"bing.com": ASYNCIO_BROWSER_ROWS}, navigate_delay=5.0)

    out = await _search(http, browser, {"query": REMOTE_QUERY, "limit": 5}, ctx)

    assert out["fallback_reason"] == "browser_timeout"
    assert out["provider"] == "searxng"  # the weak rows still come back
    assert "stop" in browser.actions  # and the session we started is stopped


async def test_operator_can_switch_the_implicit_fallback_off(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It drives a headed Chromium on the operator's display — give them a switch."""
    monkeypatch.setenv("ROBOTHOR_WEB_SEARCH_BROWSER_FALLBACK", "off")
    http = FakeHttp({"searxng.test": {"results": _rows(5, relevant=False)}})
    browser = FakeBrowser(pages={"bing.com": ASYNCIO_BROWSER_ROWS})

    out = await _search(http, browser, {"query": REMOTE_QUERY}, ctx)

    assert browser.actions == []
    assert out["fallback_reason"] == "browser_fallback_disabled"

    # …but an explicit provider="browser" is still honoured.
    explicit = await _search(http, browser, {"query": REMOTE_QUERY, "provider": "browser"}, ctx)
    assert explicit["provider"] == "browser"


async def test_two_runs_of_one_agent_do_not_race_the_session(ctx: ToolContext) -> None:
    """Both runs share one browser session: one must not stop it under the other."""
    http = FakeHttp({"searxng.test": {"results": _rows(5, relevant=False)}})
    browser = FakeBrowser(pages={"bing.com": ASYNCIO_BROWSER_ROWS}, navigate_delay=0.02)

    with patch.object(web.httpx, "AsyncClient", http.client_factory):
        with contextlib.ExitStack() as stack:
            for p in browser.install():
                stack.enter_context(p)
            first, second = await asyncio.gather(
                web._web_search({"query": REMOTE_QUERY}, ctx),
                web._web_search({"query": REMOTE_QUERY}, ctx),
            )

    assert first["provider"] == second["provider"] == "browser"
    assert browser.max_open_tabs == 1
    assert browser.open_tabs == 0


async def test_browser_answer_is_graded_too(ctx: ToolContext) -> None:
    """Falling back is not the same as answering the question."""
    http = FakeHttp({"searxng.test": {"results": _rows(5, relevant=False)}})
    browser = FakeBrowser(pages={"bing.com": BROWSER_ROWS})  # Springfield, not asyncio

    out = await _search(http, browser, {"query": REMOTE_QUERY}, ctx)

    assert out["provider"] == "browser"
    assert out["fallback_reason"] == "browser_low_relevance"
    assert out["degraded"] == "low_relevance"


async def test_unknown_provider_is_an_error(ctx: ToolContext) -> None:
    out = await _search(
        FakeHttp({}), FakeBrowser(), {"query": REMOTE_QUERY, "provider": "kagi"}, ctx
    )

    assert "error" in out
    assert "kagi" in out["error"]


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
        "https://springfield-cowork.example.com/private-offices",
    ]
    assert rows[0]["title"] == "Springfield Coworking — Private Offices"
    assert "downtown Springfield" in rows[0]["snippet"]
    assert rows[1]["title"] == "Desks & Suites of Springfield"
    # the li.b_ad block is not an organic result
    assert all("ads.example.com" not in r["url"] for r in rows)


def test_bing_click_tracking_links_are_decoded() -> None:
    """Handing the agent bing.com/ck/a?...&u=a1<base64> is handing it nothing."""
    rows = web._parse_bing_html((FIXTURES / "bing_results.html").read_text())

    assert rows[-1]["url"] == "https://springfield-cowork.example.com/private-offices"
    assert all("/ck/a" not in r["url"] for r in rows)


@pytest.mark.parametrize(
    "href,expected",
    [
        (
            "https://www.bing.com/ck/a?!&&p=1&u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9h&ntb=1",
            "https://example.com/a",
        ),
        # undecodable payload → keep the wrapper rather than invent a URL
        (
            "https://www.bing.com/ck/a?!&&p=1&u=a1%%%notbase64%%%",
            "https://www.bing.com/ck/a?!&&p=1&u=a1%%%notbase64%%%",
        ),
        ("https://example.com/plain", "https://example.com/plain"),
    ],
)
def test_bing_href_unwrapping(href: str, expected: str) -> None:
    assert web._unwrap_bing_href(href) == expected


def test_bing_titles_survive_nested_and_adjacent_markup() -> None:
    """Measured on the box: every fallback row came back titled "-".

    The anchor's own text node is empty in several of Bing's title shapes, so
    the title has to come off the <h2>, then the anchor's label, and never be
    blank — a row the agent cannot name is a row it cannot use.
    """
    rows = web._parse_bing_html((FIXTURES / "bing_nested_titles.html").read_text())

    assert [r["title"] for r in rows] == [
        "Coworking spaces in Springfield",
        "Private offices — Springfield",
        "Desks and suites of Springfield",
        "no-title.example.com",  # last resort: the host, never blank
    ]
    assert all(r["title"] for r in rows)


def test_a_row_is_never_titleless() -> None:
    assert web._row("", "https://www.example.com/page", "s")["title"] == "example.com"
    assert web._row("   ", "https://sub.example.org/x", "s")["title"] == "sub.example.org"


def test_bing_extraction_js_reads_the_heading_not_just_the_anchor() -> None:
    assert "textContent" in web._BING_EXTRACT_JS
    assert "aria-label" in web._BING_EXTRACT_JS


def test_bing_extraction_js_decodes_click_tracking() -> None:
    assert "ck/a" in web._BING_EXTRACT_JS
    assert "atob" in web._BING_EXTRACT_JS


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
    assert "a.result-link" in web._STARTPAGE_EXTRACT_JS
    assert "a.w-gl__result-title" in web._STARTPAGE_EXTRACT_JS
    assert "p.description" in web._STARTPAGE_EXTRACT_JS


# ──────────────────────────────────────────────────────────────────────
# Startpage: the source that honours the place the query named
# ──────────────────────────────────────────────────────────────────────


async def test_bing_that_drops_the_place_advances_to_startpage(ctx: ToolContext) -> None:
    """Seven rows about the box's own city are not an answer about Queens.

    Measured on the box: Bing pins a local-intent query to the egress IP and
    ignores the typed place, and it returns *enough* rows that a count-only
    advance rule never reaches for another source. Startpage honours the words.
    """
    http = FakeHttp(
        {
            "searxng.test": {"results": GENERIC_COWORKING_ROWS},
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser(
        pages={"bing.com": GEOLOCATED_BING_ROWS, "startpage.com": JAMAICA_BROWSER_ROWS}
    )

    out = await _search(http, browser, {"query": JAMAICA_QUERY, "limit": 8}, ctx)

    assert out["sources"] == ["bing", "startpage"]
    assert out["place_source"] == "startpage"
    urls = [r["url"] for r in out["results"]]
    assert urls[:2] == [r["url"] for r in JAMAICA_BROWSER_ROWS]  # the place-naming rows lead
    assert urls[2:] == [r["url"] for r in GEOLOCATED_BING_ROWS][: len(urls) - 2]
    assert out.get("degraded") != "missing_place_terms"


async def test_bing_that_honours_the_place_is_the_whole_chain(ctx: ToolContext) -> None:
    """One page is expensive: rows that name the place end the chain."""
    http = FakeHttp(
        {
            "searxng.test": {"results": GENERIC_COWORKING_ROWS},
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser(
        pages={"bing.com": JAMAICA_BING_ROWS, "startpage.com": JAMAICA_BROWSER_ROWS}
    )

    out = await _search(http, browser, {"query": JAMAICA_QUERY, "limit": 8}, ctx)

    assert len(browser.navigated) == 1
    assert out["sources"] == ["bing"]
    assert "place_source" not in out


async def test_a_place_named_by_one_word_still_advances(ctx: ToolContext) -> None:
    """The rule must not be inert for "in Springfield" — one word is a place too."""
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(5, relevant=False)},
            "nominatim.openstreetmap.org": FakeHttp.by_params(
                geocode=GEOCODE_PAYLOAD, free_text=[]
            ),
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser(pages={"bing.com": GEOLOCATED_BING_ROWS, "startpage.com": BROWSER_ROWS})

    out = await _search(http, browser, {"query": "coworking private office in Springfield"}, ctx)

    assert out["sources"] == ["bing", "startpage"]
    assert out["place_source"] == "startpage"
    assert [r["url"] for r in out["results"]][:3] == [r["url"] for r in BROWSER_ROWS]


async def test_every_source_blocked_keeps_bings_rows_and_says_so(ctx: ToolContext) -> None:
    """An honest trace: which sources were tried, and that the answer is weak."""
    http = FakeHttp(
        {
            "searxng.test": {"results": GENERIC_COWORKING_ROWS},
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser(
        pages={"bing.com": GEOLOCATED_BING_ROWS},
        html_pages={
            "startpage.com": (FIXTURES / "startpage_challenge.html").read_text(),
            "duckduckgo.com": (FIXTURES / "ddg_challenge.html").read_text(),
        },
        statuses={"duckduckgo.com": 202},
    )

    out = await _search(http, browser, {"query": JAMAICA_QUERY, "limit": 8}, ctx)

    assert out["sources"] == ["bing", "startpage:blocked", "duckduckgo:blocked"]
    assert [r["url"] for r in out["results"]] == [r["url"] for r in GEOLOCATED_BING_ROWS]
    assert out["degraded"] == "missing_place_terms"
    assert "place_source" not in out


async def test_thin_bing_page_reaches_startpage_before_duckduckgo(ctx: ToolContext) -> None:
    """The count rule still works, and Startpage sits between the two."""
    http = FakeHttp({})
    browser = FakeBrowser(
        pages={"bing.com": ASYNCIO_BROWSER_ROWS[:1], "startpage.com": ASYNCIO_BROWSER_ROWS[1:]}
    )

    out = await _search(http, browser, {"query": REMOTE_QUERY, "provider": "browser"}, ctx)

    hosts = [urlparse(u).hostname for u in browser.navigated]
    assert hosts == ["www.bing.com", "www.startpage.com"]
    assert out["sources"] == ["bing", "startpage"]
    assert [r["url"] for r in out["results"]] == [r["url"] for r in ASYNCIO_BROWSER_ROWS]
    assert "place_source" not in out  # nothing was asked about a place


async def test_startpage_failures_do_not_log_the_whole_query(
    ctx: ToolContext, caplog: pytest.LogCaptureFixture
) -> None:
    """Startpage's URL carries the query too — redact it before it reaches a log."""
    http = FakeHttp({"searxng.test": httpx.ConnectError("down")})
    browser = FakeBrowser(fetch_error="Navigation failed: net::ERR_ABORTED")

    with caplog.at_level("WARNING", logger="robothor.engine.tools.handlers.web"):
        await _search(http, browser, {"query": LONG_LOCAL_QUERY}, ctx)

    logged = " ".join(r.getMessage() for r in caplog.records)
    logged_hosts = {urlparse(u).hostname for u in re.findall(r"https?://[^\s'\"]+", logged)}
    assert {"www.bing.com", "www.startpage.com"} <= logged_hosts
    assert quote_plus(LONG_LOCAL_QUERY) not in logged
    assert LONG_LOCAL_QUERY not in logged


async def test_startpage_is_given_time_to_clear_its_interstitial(ctx: ToolContext) -> None:
    """The wait is the whole reason Startpage answers at all — ask for it.

    Measured on the box: Startpage serves a proof-of-work interstitial first and
    replaces it with the results about a second later. Without waiting for the
    result anchors the source reads as empty every single time.
    """
    browser = FakeBrowser(pages={"bing.com": ASYNCIO_BROWSER_ROWS[:1]})

    await _search(FakeHttp({}), browser, {"query": REMOTE_QUERY, "provider": "browser"}, ctx)

    waits = {urlparse(u).hostname: selector for u, selector in browser.waits}
    assert waits["www.startpage.com"] == "a.result-link, a.w-gl__result-title"
    assert waits["www.bing.com"] == ""  # Bing answers straight away; do not pay for a wait


def test_startpage_html_fixture_parses_into_rows() -> None:
    rows = web._parse_startpage_html((FIXTURES / "startpage_results.html").read_text())

    assert len(rows) == 3
    assert rows[0]["title"] == "Springfield Coworking — Private Offices"
    assert rows[0]["url"] == "https://springfield-cowork.example.com/"
    assert "meeting rooms" in rows[0]["snippet"]
    assert [r["url"] for r in rows[1:]] == [
        "https://desks.example.org/springfield",
        "https://example.net/listings/springfield-office",
    ]
    assert all(r["title"] for r in rows)


def test_startpage_parser_reads_either_result_class() -> None:
    """Startpage serves both the old and the new result-anchor class."""
    html = (
        "<html><body>"
        "<div class='w-gl__result'><a class='result-link' href='https://a.example.com/'>A</a>"
        "<p class='description'>First.</p></div>"
        "<div class='w-gl__result'>"
        "<a class='w-gl__result-title' href='https://b.example.com/'>B</a>"
        "<p class='w-gl__description'>Second.</p></div>"
        "</body></html>"
    )

    rows = web._parse_startpage_html(html)

    assert [(r["title"], r["url"], r["snippet"]) for r in rows] == [
        ("A", "https://a.example.com/", "First."),
        ("B", "https://b.example.com/", "Second."),
    ]


def test_startpage_captcha_page_is_a_challenge_not_an_empty_page() -> None:
    challenge = (FIXTURES / "startpage_challenge.html").read_text()
    results = (FIXTURES / "startpage_results.html").read_text()

    assert web._looks_like_challenge(challenge, web._STARTPAGE_RESULT_MARKERS) is True
    assert web._looks_like_challenge(results, web._STARTPAGE_RESULT_MARKERS) is False


def test_the_grader_and_the_chain_ask_the_same_question() -> None:
    """One implementation, so "Bing dropped the place" cannot drift from the grade."""
    generic = [web._row(r["title"], r["url"], r["content"]) for r in GENERIC_COWORKING_ROWS]
    naming = [web._row(r["title"], r["url"], r["snippet"]) for r in JAMAICA_BROWSER_ROWS]

    assert web._grade_results(generic, JAMAICA_QUERY) == "missing_place_terms"
    assert web._rows_mention_place(generic, JAMAICA_QUERY) is False
    assert web._rows_mention_place(naming, JAMAICA_QUERY) is True


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


async def test_explicit_searxng_is_not_quietly_answered_by_brave(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that named a provider gets that provider."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser()

    out = await _search(
        http, browser, {"query": LOCAL_QUERY, "provider": "searxng", "limit": 4}, ctx
    )

    assert out["provider"] == "searxng"
    assert "degraded" not in out  # the SearXNG answer stood on its own
    assert http.hit("api.search.brave.com") == []
    assert browser.actions == []
    # nothing refused to answer, so nothing is reported as unresponsive
    assert "unresponsive_engines" not in out


async def test_brave_is_absent_without_a_key(ctx: ToolContext) -> None:
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
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
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)

    assert out["provider"] == "searxng"
    assert "error" not in out


# ──────────────────────────────────────────────────────────────────────
# Places, via Nominatim + Overpass
# ──────────────────────────────────────────────────────────────────────


async def test_known_category_geocodes_then_queries_overpass(ctx: ToolContext) -> None:
    """ "coworking near X" is an OSM tag query, not a free-text geocode."""
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)

    geocode = http.hit("nominatim.openstreetmap.org")
    assert len(geocode) == 1
    assert geocode[0]["params"]["q"] == "Springfield"
    assert "genus" in geocode[0]["headers"]["User-Agent"].lower()

    overpass = http.hit("overpass-api.de")
    assert len(overpass) == 1
    assert overpass[0]["method"] == "POST"
    body = overpass[0]["body"]
    assert '["office"="coworking"]' in body
    assert "around:5000,42.1015,-72.5898" in body

    assert [p["title"] for p in out["places"]] == ["Springfield Coworking", "Desks & Suites"]
    assert out["places"][0]["url"] == "https://www.openstreetmap.org/node/111"
    assert out["places"][1]["url"] == "https://www.openstreetmap.org/way/222"
    assert "Main Street" in out["places"][0]["snippet"]


@pytest.mark.parametrize(
    "query,tag",
    [
        ("coworking near Springfield", '["office"="coworking"]'),
        ("coffee near Springfield", '["amenity"="cafe"]'),
        ("a cafe in Springfield", '["amenity"="cafe"]'),
        ("gym near Springfield", '["leisure"="fitness_centre"]'),
    ],
)
def test_category_to_osm_tag_map(query: str, tag: str) -> None:
    category = web._place_category(query)
    assert category is not None
    assert tag in web._overpass_query(category, 42.0, -72.0)


async def test_unknown_category_skips_overpass(ctx: ToolContext) -> None:
    """No tag map for "hardware store" — geocode it free-text instead."""
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": NOMINATIM_PAYLOAD,
        }
    )
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": "hardware store in Springfield", "limit": 4}, ctx)

    assert http.hit("overpass-api.de") == []
    assert len(out["places"]) == 2
    assert out["places"][0]["url"] == "https://www.openstreetmap.org/node/12345"
    assert "coworking_space" in out["places"][0]["snippet"]


@pytest.mark.parametrize(
    "query,expected",
    [
        ("coworking private office Jamaica Queens", "Jamaica Queens"),
        ("coworking near Jamaica Queens NY", "Jamaica Queens NY"),
        ("coworking near Springfield", "Springfield"),
    ],
)
def test_place_text_is_taken_from_the_query_and_nothing_is_invented(
    query: str, expected: str
) -> None:
    """ "Jamaica Queens", not "Queens" — and no state the operator never typed."""
    assert web._place_text(query) == expected


async def test_geocode_query_is_exactly_what_the_operator_named(ctx: ToolContext) -> None:
    http = FakeHttp(
        {
            "searxng.test": {"results": GENERIC_COWORKING_ROWS},
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser(pages={"bing.com": JAMAICA_BROWSER_ROWS})

    await _search(http, browser, {"query": JAMAICA_QUERY, "limit": 8}, ctx)

    assert http.hit("nominatim.openstreetmap.org")[0]["params"]["q"] == "Jamaica Queens"


async def test_places_say_why_they_are_missing_when_geocoding_fails(
    ctx: ToolContext,
) -> None:
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": [],  # geocode finds nothing, free text neither
        }
    )
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)

    assert "places" not in out
    assert out["places_reason"] == "geocode_failed:Springfield"


async def test_places_say_why_they_are_missing_when_overpass_is_empty(
    ctx: ToolContext,
) -> None:
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": FakeHttp.by_params(
                geocode=GEOCODE_PAYLOAD, free_text=[]
            ),
            "overpass-api.de": {"elements": []},
        }
    )
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)

    assert "places" not in out
    assert out["places_reason"] == "overpass_empty"


async def test_places_say_why_they_are_missing_when_overpass_errors(
    ctx: ToolContext,
) -> None:
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": FakeHttp.by_params(
                geocode=GEOCODE_PAYLOAD, free_text=[]
            ),
            "overpass-api.de": httpx.ConnectError("overpass unreachable"),
        }
    )
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)

    assert out["places_reason"] == "overpass_error"


async def test_places_say_when_the_category_is_not_mapped(ctx: ToolContext) -> None:
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": [],
        }
    )
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": "hardware store in Springfield", "limit": 4}, ctx)

    assert http.hit("overpass-api.de") == []
    assert out["places_reason"] == "category_unmapped"


async def test_found_places_carry_no_reason(ctx: ToolContext) -> None:
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)

    assert out["places"]
    assert "places_reason" not in out


async def test_non_local_query_never_touches_nominatim_or_overpass(ctx: ToolContext) -> None:
    http = FakeHttp({"searxng.test": {"results": _rows(4, relevant=True)}})
    browser = FakeBrowser()

    out = await _search(http, browser, {"query": REMOTE_QUERY, "limit": 4}, ctx)

    assert http.hit("nominatim") == []
    assert http.hit("overpass") == []
    assert "places" not in out


@pytest.mark.parametrize(
    "query,local",
    [
        ("coworking near Springfield", True),
        ("dentist nearby", True),
        ("hardware store in Springfield", True),
        ("pizza 02134", True),
        ("coworking private office Jamaica Queens", True),  # category + proper nouns
        ("asyncio cancellation semantics python", False),
        ("what is a private office", False),
        ("how to mount a share in Ubuntu", False),  # "in <software>" is not a place
        ("install postgres in Docker", False),
    ],
)
def test_local_query_detection(query: str, local: bool) -> None:
    assert web._looks_local(query) is local


async def test_geocoding_is_rate_limited(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nominatim and Overpass are free, courteous-use APIs: ≤1 request/second."""
    monkeypatch.setattr(web, "_PLACES_MIN_INTERVAL", 0.2)
    http = FakeHttp(
        {
            "searxng.test": {"results": _rows(4, relevant=True)},
            "nominatim.openstreetmap.org": GEOCODE_PAYLOAD,
            "overpass-api.de": OVERPASS_PAYLOAD,
        }
    )
    browser = FakeBrowser()

    started = time.monotonic()
    await _search(http, browser, {"query": LOCAL_QUERY, "limit": 4}, ctx)
    elapsed = time.monotonic() - started

    # geocode + Overpass = two courtesy-gated calls, so one interval elapses
    assert len(http.hit("nominatim.openstreetmap.org")) == 1
    assert len(http.hit("overpass-api.de")) == 1
    assert elapsed >= 0.2


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
