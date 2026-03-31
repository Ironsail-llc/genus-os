"""Browser automation tool handler — Playwright CDP browser control."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.async_api import Browser, BrowserContext, Page

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

# Active browser sessions keyed by agent_id
_sessions: dict[str, BrowserSession] = {}
_playwright_instance: Any = None
_session_lock = asyncio.Lock()

# Auto-cleanup after 10 minutes of inactivity
SESSION_TIMEOUT_SECONDS = 600


@dataclass
class BrowserSession:
    """Tracks a persistent browser session for an agent."""

    browser: Browser
    context: BrowserContext
    page: Page
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used = time.time()

    @property
    def expired(self) -> bool:
        return (time.time() - self.last_used) > SESSION_TIMEOUT_SECONDS


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _display() -> str:
    """Get the virtual display identifier."""
    from robothor.engine.tools.dispatch import _cfg

    return _cfg().desktop_display


async def _get_playwright() -> Any:
    """Get or create the global Playwright instance."""
    global _playwright_instance
    if _playwright_instance is None:
        from playwright.async_api import async_playwright

        _playwright_instance = await async_playwright().start()
    return _playwright_instance


async def _get_session(agent_id: str) -> BrowserSession | None:
    """Get an active session, returning None if not found or expired."""
    session = _sessions.get(agent_id)
    if session is None:
        return None
    if session.expired:
        await _close_session(agent_id)
        return None
    session.touch()
    return session


async def _close_session(agent_id: str) -> None:
    """Close and remove a browser session."""
    session = _sessions.pop(agent_id, None)
    if session is None:
        return
    try:
        await session.browser.close()
    except Exception as e:
        logger.warning("Error closing browser session for %s: %s", agent_id, e)


async def _cleanup_expired() -> None:
    """Clean up all expired sessions."""
    expired = [k for k, v in _sessions.items() if v.expired]
    for key in expired:
        await _close_session(key)


# ---------------------------------------------------------------------------
# Main browser tool — dispatches by action
# ---------------------------------------------------------------------------


@_handler("browser")
async def _browser(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Full browser automation via Playwright. Dispatches by 'action' parameter."""
    action = args.get("action", "")
    if not action:
        return {
            "error": "No action provided. Use: start, stop, navigate, screenshot, snapshot, click, fill, type, press, scroll, evaluate, tabs, pdf, console, status"
        }

    # Clean up expired sessions periodically
    await _cleanup_expired()

    dispatch: dict[str, Any] = {
        "start": _action_start,
        "stop": _action_stop,
        "status": _action_status,
        "navigate": _action_navigate,
        "screenshot": _action_screenshot,
        "snapshot": _action_snapshot,
        "act": _action_act,
        "tabs": _action_tabs,
        "pdf": _action_pdf,
        "console": _action_console,
        "evaluate": _action_evaluate,
    }

    handler = dispatch.get(action)
    if handler is None:
        return {
            "error": f"Unknown browser action: {action}. Available: {', '.join(sorted(dispatch.keys()))}"
        }

    result: dict[str, Any] = await handler(args, ctx)
    return result


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


async def _action_start(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Launch a managed Chromium browser on the virtual display."""
    agent_id = ctx.agent_id or "default"

    async with _session_lock:
        existing = await _get_session(agent_id)
        if existing is not None:
            return {"status": "already_running", "agent_id": agent_id}

        try:
            pw = await _get_playwright()
            browser = await pw.chromium.launch(
                headless=False,
                args=[
                    f"--display={_display()}",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--window-size=1280,960",
                    "--window-position=0,0",
                ],
                env={**os.environ, "DISPLAY": _display()},
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 960},
                user_agent="Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Robothor/1.0",
            )
            page = await context.new_page()
            _sessions[agent_id] = BrowserSession(browser=browser, context=context, page=page)
            return {"status": "started", "agent_id": agent_id}
        except Exception as e:
            return {"error": f"Failed to start browser: {e}"}


async def _action_stop(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Close the browser session."""
    agent_id = ctx.agent_id or "default"
    await _close_session(agent_id)
    return {"status": "stopped", "agent_id": agent_id}


async def _action_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Check browser session status."""
    agent_id = ctx.agent_id or "default"
    session = await _get_session(agent_id)
    if session is None:
        return {"status": "not_running", "agent_id": agent_id}
    return {
        "status": "running",
        "agent_id": agent_id,
        "url": session.page.url,
        "title": await session.page.title(),
        "age_seconds": int(time.time() - session.created_at),
    }


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


async def _action_navigate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Navigate to a URL."""
    agent_id = ctx.agent_id or "default"
    session = await _get_session(agent_id)
    if session is None:
        return {"error": "Browser not started. Call browser(action='start') first."}

    url = args.get("targetUrl") or args.get("url", "")
    if not url:
        return {"error": "No URL provided (use 'targetUrl' or 'url' parameter)"}

    try:
        response = await session.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {
            "url": session.page.url,
            "title": await session.page.title(),
            "status": response.status if response else None,
        }
    except Exception as e:
        return {"error": f"Navigation failed: {e}"}


# ---------------------------------------------------------------------------
# Content capture
# ---------------------------------------------------------------------------


async def _action_screenshot(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Capture a screenshot of the current page."""
    agent_id = ctx.agent_id or "default"
    session = await _get_session(agent_id)
    if session is None:
        return {"error": "Browser not started."}

    try:
        full_page = args.get("fullPage", False)
        data = await session.page.screenshot(full_page=full_page)
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "screenshot_base64": b64,
            "url": session.page.url,
            "title": await session.page.title(),
            "format": "png",
        }
    except Exception as e:
        return {"error": f"Screenshot failed: {e}"}


async def _action_snapshot(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get the accessibility tree (ARIA snapshot) of the current page.

    Returns a structured tree with element refs that can be used for targeted
    interactions via the 'act' action.
    """
    agent_id = ctx.agent_id or "default"
    session = await _get_session(agent_id)
    if session is None:
        return {"error": "Browser not started."}

    try:
        tree = await session.page.locator(":root").aria_snapshot()
        return {
            "snapshot": tree,
            "url": session.page.url,
            "title": await session.page.title(),
        }
    except Exception as e:
        return {"error": f"Snapshot failed: {e}"}


async def _action_pdf(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Export the current page as PDF (base64)."""
    agent_id = ctx.agent_id or "default"
    session = await _get_session(agent_id)
    if session is None:
        return {"error": "Browser not started."}

    try:
        data = await session.page.pdf()
        b64 = base64.b64encode(data).decode("ascii")
        return {"pdf_base64": b64, "url": session.page.url}
    except Exception as e:
        return {"error": f"PDF export failed: {e}"}


async def _action_console(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Read recent console messages from the page."""
    agent_id = ctx.agent_id or "default"
    session = await _get_session(agent_id)
    if session is None:
        return {"error": "Browser not started."}

    # Note: console messages need to be captured via event listeners
    # For now, return page info
    return {
        "note": "Console logging requires pre-registered listeners. Use evaluate(js='console.log(...)') for direct JS execution.",
        "url": session.page.url,
    }


async def _action_evaluate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Execute JavaScript on the current page."""
    agent_id = ctx.agent_id or "default"
    session = await _get_session(agent_id)
    if session is None:
        return {"error": "Browser not started."}

    js = args.get("js") or args.get("expression", "")
    if not js:
        return {"error": "No JavaScript expression provided"}

    try:
        result = await session.page.evaluate(js)
        return {"result": result, "url": session.page.url}
    except Exception as e:
        return {"error": f"JS evaluation failed: {e}"}


# ---------------------------------------------------------------------------
# Tab management
# ---------------------------------------------------------------------------


async def _action_tabs(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List open browser tabs."""
    agent_id = ctx.agent_id or "default"
    session = await _get_session(agent_id)
    if session is None:
        return {"error": "Browser not started."}

    try:
        pages = session.context.pages
        tabs = []
        for i, page in enumerate(pages):
            tabs.append(
                {
                    "index": i,
                    "url": page.url,
                    "title": await page.title(),
                    "is_active": page == session.page,
                }
            )
        return {"tabs": tabs, "count": len(tabs)}
    except Exception as e:
        return {"error": f"Failed to list tabs: {e}"}


async def _action_act(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Perform an interaction on the page.

    request.kind: click, fill, type, press, scroll, select
    request.ref: element ref from accessibility snapshot
    request.selector: CSS selector (fallback)
    """
    agent_id = ctx.agent_id or "default"
    session = await _get_session(agent_id)
    if session is None:
        return {"error": "Browser not started."}

    request = args.get("request", {})
    if isinstance(request, str):
        return {"error": "request must be an object with 'kind' field"}

    kind = request.get("kind", "")
    selector = request.get("selector", "")
    ref = request.get("ref", "")

    # Build locator from selector or ref
    if ref:
        # ARIA snapshot refs can be used as [data-ref] or role-based selectors
        # For simplicity, use getByRole or text-based matching
        locator_str = selector or f'[aria-label*="{ref}"]'
    elif selector:
        locator_str = selector
    else:
        locator_str = ""

    page = session.page

    try:
        if kind == "click":
            if request.get("x") is not None and request.get("y") is not None:
                await page.mouse.click(int(request["x"]), int(request["y"]))
            elif locator_str:
                await page.locator(locator_str).first.click(timeout=10000)
            else:
                return {"error": "click requires (x, y) coordinates or a selector/ref"}
            return {
                "acted": "click",
                "target": locator_str or f"({request.get('x')}, {request.get('y')})",
            }

        elif kind == "fill":
            fields = request.get("fields", [])
            if fields:
                for f in fields:
                    sel = f.get("selector") or f.get("ref", "")
                    val = f.get("value", "")
                    if sel:
                        await page.locator(sel).first.fill(val, timeout=10000)
                return {"acted": "fill", "fields_count": len(fields)}
            elif locator_str:
                value = request.get("value", "")
                await page.locator(locator_str).first.fill(value, timeout=10000)
                return {"acted": "fill", "target": locator_str}
            else:
                return {"error": "fill requires a selector/ref and value"}

        elif kind == "type":
            text = request.get("text", "")
            if locator_str:
                await page.locator(locator_str).first.type(text, timeout=10000)
            else:
                await page.keyboard.type(text)
            return {"acted": "type", "text_length": len(text)}

        elif kind == "press":
            key = request.get("key", "")
            if not key:
                return {"error": "press requires a 'key' field"}
            await page.keyboard.press(key)
            return {"acted": "press", "key": key}

        elif kind == "scroll":
            dx = int(request.get("dx", 0))
            dy = int(request.get("dy", -300))
            await page.mouse.wheel(dx, dy)
            return {"acted": "scroll", "dx": dx, "dy": dy}

        elif kind == "select":
            value = request.get("value", "")
            if locator_str:
                await page.locator(locator_str).first.select_option(value, timeout=10000)
                return {"acted": "select", "value": value}
            return {"error": "select requires a selector/ref"}

        else:
            return {
                "error": f"Unknown act kind: {kind}. Use: click, fill, type, press, scroll, select"
            }

    except Exception as e:
        return {"error": f"Act '{kind}' failed: {e}"}
