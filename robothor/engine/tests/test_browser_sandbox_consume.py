"""Browser drives the sandbox's Chromium over CDP when present (PR-9).

A per-run Docker sandbox exposes a CDP endpoint; the browser connects to it
instead of launching a host Chromium with --no-sandbox. With no sandbox
(default), it launches on the host display as before.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import robothor.engine.sandbox as sandbox_mod
from robothor.engine.tools.handlers import browser as browser_mod


def _fake_playwright():
    page = MagicMock()
    ctx = SimpleNamespace(pages=[page], new_page=AsyncMock(return_value=page))
    fake_browser = SimpleNamespace(
        contexts=[ctx],
        new_context=AsyncMock(return_value=ctx),
    )
    chromium = SimpleNamespace(
        connect_over_cdp=AsyncMock(return_value=fake_browser),
        launch=AsyncMock(return_value=fake_browser),
    )
    return SimpleNamespace(chromium=chromium), chromium


async def _run_start(monkeypatch, sandbox):
    pw, chromium = _fake_playwright()
    monkeypatch.setattr(browser_mod, "_get_playwright", AsyncMock(return_value=pw))
    monkeypatch.setattr(sandbox_mod, "get_current_sandbox", lambda: sandbox)
    browser_mod._sessions.pop("default", None)
    ctx = SimpleNamespace(agent_id="default")
    result = await browser_mod._action_start({}, ctx)
    browser_mod._sessions.pop("default", None)
    return result, chromium


async def test_connects_over_cdp_with_sandbox(monkeypatch):
    sandbox = SimpleNamespace(browser_endpoint=lambda: "http://localhost:9222")
    result, chromium = await _run_start(monkeypatch, sandbox)
    assert result.get("status") == "started"
    chromium.connect_over_cdp.assert_awaited_once_with("http://localhost:9222")
    chromium.launch.assert_not_awaited()


async def test_launches_on_host_without_sandbox(monkeypatch):
    result, chromium = await _run_start(monkeypatch, None)
    assert result.get("status") == "started"
    chromium.launch.assert_awaited_once()
    chromium.connect_over_cdp.assert_not_awaited()


def _fake_playwright_closable():
    page = MagicMock()
    ctx = SimpleNamespace(pages=[page], new_page=AsyncMock(return_value=page))
    fake_browser = SimpleNamespace(
        contexts=[ctx],
        new_context=AsyncMock(return_value=ctx),
        close=AsyncMock(),
    )
    chromium = SimpleNamespace(
        connect_over_cdp=AsyncMock(return_value=fake_browser),
        launch=AsyncMock(return_value=fake_browser),
    )
    return SimpleNamespace(chromium=chromium), chromium, fake_browser


async def test_same_sandbox_endpoint_reuses_session(monkeypatch):
    pw, chromium, _ = _fake_playwright_closable()
    monkeypatch.setattr(browser_mod, "_get_playwright", AsyncMock(return_value=pw))
    sandbox = SimpleNamespace(browser_endpoint=lambda: "http://localhost:9222")
    monkeypatch.setattr(sandbox_mod, "get_current_sandbox", lambda: sandbox)
    browser_mod._sessions.pop("default", None)
    ctx = SimpleNamespace(agent_id="default")
    try:
        first = await browser_mod._action_start({}, ctx)
        second = await browser_mod._action_start({}, ctx)
        assert first.get("status") == "started"
        assert second.get("status") == "already_running"
        chromium.connect_over_cdp.assert_awaited_once()  # not reconnected
    finally:
        browser_mod._sessions.pop("default", None)


async def test_stale_session_from_destroyed_sandbox_is_replaced(monkeypatch):
    """A session cached against a prior run's sandbox endpoint must not be
    reused when the current run has a different sandbox — it is closed and a
    fresh CDP connection is made."""
    pw, chromium, first_browser = _fake_playwright_closable()
    monkeypatch.setattr(browser_mod, "_get_playwright", AsyncMock(return_value=pw))
    endpoints = ["http://localhost:9222", "http://localhost:9333"]
    monkeypatch.setattr(
        sandbox_mod,
        "get_current_sandbox",
        lambda: SimpleNamespace(browser_endpoint=lambda: endpoints[0]),
    )
    browser_mod._sessions.pop("default", None)
    ctx = SimpleNamespace(agent_id="default")
    try:
        await browser_mod._action_start({}, ctx)  # binds to :9222
        # The next run's sandbox exposes a different endpoint (old one destroyed).
        endpoints[0] = endpoints[1]
        result = await browser_mod._action_start({}, ctx)
        assert result.get("status") == "started"  # replaced, not reused
        first_browser.close.assert_awaited()  # stale session torn down
        assert chromium.connect_over_cdp.await_count == 2
        assert browser_mod._sessions["default"].sandbox_endpoint == "http://localhost:9333"
    finally:
        browser_mod._sessions.pop("default", None)
