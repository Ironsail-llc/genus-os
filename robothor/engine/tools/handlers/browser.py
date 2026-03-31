"""Browser automation tool handler — Playwright CDP browser control."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re as re_mod
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.async_api import Browser, BrowserContext, Locator, Page

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

# Active browser sessions keyed by agent_id
_sessions: dict[str, BrowserSession] = {}
_playwright_instance: Any = None
_session_lock = asyncio.Lock()

# Auto-cleanup after 10 minutes of inactivity
SESSION_TIMEOUT_SECONDS = 600

# ARIA roles that represent interactive elements worth indexing for the LLM
INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "link",
        "listbox",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "textbox",
        "treeitem",
    }
)

# Max indexed elements returned to the LLM to keep context manageable
_MAX_INDEXED_ELEMENTS = 100

# Regex to parse ARIA snapshot lines like: textbox "First Name" [required]
_ARIA_LINE_RE = re_mod.compile(
    r"^(?P<indent>\s*)-\s+"
    r"(?P<role>\w+)"
    r'(?:\s+"(?P<name>[^"]*)")?'
    r"(?:\s+\[(?P<attrs>[^\]]*)\])?"
    r"(?::\s*(?P<text>.*))?$"
)


@dataclass
class ElementRef:
    """A distilled interactive element from the ARIA tree."""

    index: int
    role: str
    name: str
    text: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class BrowserSession:
    """Tracks a persistent browser session for an agent."""

    browser: Browser
    context: BrowserContext
    page: Page
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    element_registry: dict[int, ElementRef] = field(default_factory=dict)

    def touch(self) -> None:
        self.last_used = time.time()

    @property
    def expired(self) -> bool:
        return (time.time() - self.last_used) > SESSION_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# ARIA snapshot distillation
# ---------------------------------------------------------------------------


def _parse_attrs(attrs_str: str) -> dict[str, str]:
    """Parse attribute string like 'required, checked' into a dict."""
    result: dict[str, str] = {}
    if not attrs_str:
        return result
    for part in attrs_str.split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
        elif part:
            result[part] = "true"
    return result


def _distill_snapshot(raw: str) -> tuple[str, dict[int, ElementRef]]:
    """Parse an ARIA snapshot YAML string into a compact indexed element list.

    Returns (distilled_text, element_registry) where distilled_text is a
    compact representation with @N refs for interactive elements, and
    element_registry maps index -> ElementRef for locator resolution.
    """
    if not raw or not raw.strip():
        return "(empty page)", {}

    registry: dict[int, ElementRef] = {}
    output_lines: list[str] = []
    next_index = 1

    for line in raw.splitlines():
        m = _ARIA_LINE_RE.match(line)
        if not m:
            # Preserve non-matching lines (plain text content) with indent
            stripped = line.strip()
            if stripped and stripped != "-":
                output_lines.append(line)
            continue

        indent = m.group("indent") or ""
        role = m.group("role")
        name = m.group("name") or ""
        attrs_str = m.group("attrs") or ""
        text = m.group("text") or ""
        attrs = _parse_attrs(attrs_str)

        # Build display parts
        display_name = f' "{name}"' if name else ""
        display_attrs = f" [{attrs_str}]" if attrs_str else ""
        display_text = f": {text}" if text else ""

        if role in INTERACTIVE_ROLES and next_index <= _MAX_INDEXED_ELEMENTS:
            # Indexed interactive element
            ref_tag = f"@{next_index}"
            registry[next_index] = ElementRef(
                index=next_index,
                role=role,
                name=name,
                text=text.strip() if text else "",
                attributes=attrs,
            )
            output_lines.append(
                f"{indent}  {ref_tag} {role}{display_name}{display_attrs}{display_text}"
            )
            next_index += 1
        elif role in (
            "heading",
            "navigation",
            "main",
            "banner",
            "contentinfo",
            "complementary",
            "region",
            "form",
            "dialog",
            "alertdialog",
            "alert",
            "status",
            "img",
        ):
            # Structural/landmark elements — keep for context, no index
            output_lines.append(f"{indent}  {role}{display_name}{display_attrs}{display_text}")
        elif role == "paragraph" and text:
            # Keep paragraph text for context
            output_lines.append(f"{indent}  {text}")
        # else: skip generic/group/list wrappers to reduce noise

    if next_index > _MAX_INDEXED_ELEMENTS + 1:
        overflow = next_index - _MAX_INDEXED_ELEMENTS - 1
        output_lines.append(
            f"\n... and {overflow} more interactive elements (scroll down or refine scope)"
        )

    distilled = "\n".join(output_lines)
    return distilled, registry


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
        # Phase 1: Try networkidle (catches SPAs like Workday that settle)
        response = None
        try:
            response = await session.page.goto(url, wait_until="networkidle", timeout=20000)
        except Exception:
            # Phase 2: Fallback for pages that never go idle (streaming, websockets)
            response = await session.page.goto(url, wait_until="domcontentloaded", timeout=15000)

        # Phase 3: Extra wait for JS framework rendering
        import contextlib

        with contextlib.suppress(Exception):
            await session.page.wait_for_selector(
                "button, input, a, select, textarea, "
                "[role='button'], [role='link'], [role='textbox']",
                timeout=3000,
            )

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
    """Get a distilled accessibility snapshot with indexed @N element refs.

    Returns interactive elements with @1, @2, ... refs for use with the
    'act' action.  Structural context (headings, landmarks) is preserved
    without indexing.  When few interactive elements are detected, a
    screenshot is auto-included as a vision fallback.
    """
    agent_id = ctx.agent_id or "default"
    session = await _get_session(agent_id)
    if session is None:
        return {"error": "Browser not started."}

    try:
        raw = await session.page.locator(":root").aria_snapshot(timeout=15000)
        distilled, registry = _distill_snapshot(raw)
        session.element_registry = registry

        result: dict[str, Any] = {
            "snapshot": distilled,
            "element_count": len(registry),
            "url": session.page.url,
            "title": await session.page.title(),
        }

        # Vision fallback: auto-include screenshot when few interactive elements
        if len(registry) <= 2:
            try:
                data = await session.page.screenshot()
                result["screenshot_base64"] = base64.b64encode(data).decode("ascii")
                result["vision_fallback"] = True
                result["note"] = (
                    "Few interactive elements detected in ARIA tree. "
                    "Screenshot included for visual inspection. "
                    "Use act(kind='click', x=..., y=...) for coordinate-based interaction."
                )
            except Exception:
                pass  # Screenshot failure is non-fatal

        return result

    except Exception as e:
        # ARIA snapshot failed entirely — fall back to screenshot-only
        logger.warning("ARIA snapshot failed for %s: %s", agent_id, e)
        try:
            data = await session.page.screenshot()
            return {
                "snapshot": "(ARIA tree unavailable — use screenshot for visual inspection)",
                "element_count": 0,
                "screenshot_base64": base64.b64encode(data).decode("ascii"),
                "vision_fallback": True,
                "url": session.page.url,
                "title": await session.page.title(),
                "error_detail": str(e),
            }
        except Exception as e2:
            return {"error": f"Snapshot failed: {e}; screenshot also failed: {e2}"}


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


async def _resolve_ref(page: Page, registry: dict[int, ElementRef], ref_str: str) -> Locator | None:
    """Resolve an @N ref to a Playwright Locator using semantic methods.

    Tries a cascade of strategies from most-specific to least-specific:
    1. get_by_role(role, name=exact_name)
    2. get_by_role(role) when only one match exists
    3. get_by_label / get_by_placeholder for textbox-like roles
    4. get_by_text for links/buttons
    5. get_by_role with case-insensitive partial name match
    """
    match = re_mod.match(r"@(\d+)", ref_str)
    if not match:
        return None
    index = int(match.group(1))
    elem = registry.get(index)
    if elem is None:
        return None

    # Strategy 1: role + exact name (most reliable)
    if elem.name:
        locator = page.get_by_role(elem.role, name=elem.name)
        if await locator.count() >= 1:
            return locator.first

    # Strategy 2: role only, if unique on page
    locator = page.get_by_role(elem.role)
    if await locator.count() == 1:
        return locator.first

    # Strategy 3: label/placeholder for input-like roles
    if elem.role in ("textbox", "searchbox", "combobox", "spinbutton") and elem.name:
        for method in (page.get_by_label, page.get_by_placeholder):
            locator = method(elem.name)
            if await locator.count() >= 1:
                return locator.first

    # Strategy 4: text match for links/buttons
    if elem.role in ("link", "button", "tab", "menuitem") and elem.name:
        locator = page.get_by_text(elem.name, exact=True)
        if await locator.count() >= 1:
            return locator.first

    # Strategy 5: case-insensitive partial name match
    if elem.name:
        locator = page.get_by_role(
            elem.role,
            name=re_mod.compile(re_mod.escape(elem.name), re_mod.IGNORECASE),
        )
        if await locator.count() >= 1:
            return locator.first

    return None


async def _resolve_field_ref(
    page: Page, registry: dict[int, ElementRef], ref_str: str
) -> Locator | None:
    """Resolve a ref from a batch fill field — supports @N and CSS selectors."""
    if ref_str.startswith("@"):
        return await _resolve_ref(page, registry, ref_str)
    if ref_str:
        return page.locator(ref_str).first
    return None


async def _action_act(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Perform an interaction on the page.

    request.kind: click, fill, type, press, scroll, select
    request.ref: @N element ref from most recent snapshot (e.g. "@3")
    request.selector: CSS selector (fallback)
    request.x, request.y: pixel coordinates (for click)
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
    page = session.page

    # Resolve target locator from @ref or CSS selector
    locator: Locator | None = None
    target_desc = ""

    if ref and ref.startswith("@"):
        locator = await _resolve_ref(page, session.element_registry, ref)
        if locator is None:
            return {
                "error": f"Could not resolve {ref}. Run snapshot again to refresh element list."
            }
        elem = session.element_registry.get(int(ref[1:]))
        target_desc = f'{ref} ({elem.role} "{elem.name}")' if elem else ref
    elif selector:
        locator = page.locator(selector).first
        target_desc = selector

    try:
        if kind == "click":
            if request.get("x") is not None and request.get("y") is not None:
                await page.mouse.click(int(request["x"]), int(request["y"]))
                return {"acted": "click", "target": f"({request['x']}, {request['y']})"}
            elif locator:
                await locator.click(timeout=10000)
                return {"acted": "click", "target": target_desc}
            else:
                return {"error": "click requires @ref, (x,y) coordinates, or a selector"}

        elif kind == "fill":
            # Batch fill: [{ref: "@3", value: "Philip"}, {ref: "@4", value: "Doe"}]
            fields = request.get("fields", [])
            if fields:
                filled = 0
                errors: list[str] = []
                for f in fields:
                    f_ref = f.get("ref", "")
                    f_sel = f.get("selector", "")
                    val = f.get("value", "")
                    f_locator = await _resolve_field_ref(
                        page, session.element_registry, f_ref or f_sel
                    )
                    if f_locator:
                        await f_locator.fill(val, timeout=10000)
                        filled += 1
                    else:
                        errors.append(f"Could not resolve {f_ref or f_sel}")
                result: dict[str, Any] = {
                    "acted": "fill",
                    "fields_filled": filled,
                    "fields_requested": len(fields),
                }
                if errors:
                    result["errors"] = errors
                return result
            elif locator:
                value = request.get("value", "")
                await locator.fill(value, timeout=10000)
                return {"acted": "fill", "target": target_desc}
            else:
                return {"error": "fill requires @ref or selector and value"}

        elif kind == "type":
            text = request.get("text", "")
            if locator:
                await locator.type(text, timeout=10000)
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
            if locator:
                await locator.select_option(value, timeout=10000)
                return {"acted": "select", "value": value, "target": target_desc}
            return {"error": "select requires @ref or selector"}

        else:
            return {
                "error": f"Unknown act kind: {kind}. Use: click, fill, type, press, scroll, select"
            }

    except Exception as e:
        return {"error": f"Act '{kind}' failed: {e}"}
