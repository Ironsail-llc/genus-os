"""Desktop control tool handlers — screenshot, click, type, key, scroll, window management."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path as _Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

# Display dimensions (set on first screenshot)
_display_width: int = 1280
_display_height: int = 1024


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _display() -> str:
    """Get the virtual display identifier."""
    from robothor.engine.tools.dispatch import _cfg

    return _cfg().desktop_display


def _env() -> dict[str, str]:
    """Build subprocess env with DISPLAY injected."""
    env = os.environ.copy()
    env["DISPLAY"] = _display()
    return env


def _run_xdotool(*args: str, timeout: int = 10) -> dict[str, Any]:
    """Run an xdotool command and return result."""
    cmd = ["xdotool", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env(),
        )
        if proc.returncode != 0:
            return {"error": f"xdotool failed: {proc.stderr.strip()}"}
        return {"stdout": proc.stdout.strip(), "exit_code": 0}
    except subprocess.TimeoutExpired:
        return {"error": f"xdotool timed out ({timeout}s)"}
    except Exception as e:
        return {"error": f"xdotool error: {e}"}


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


@_handler("desktop_screenshot")
async def _screenshot(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Capture the virtual display and return base64 PNG."""

    def _run() -> dict[str, Any]:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            proc = subprocess.run(
                ["scrot", "-o", path],
                capture_output=True,
                text=True,
                timeout=10,
                env=_env(),
            )
            if proc.returncode != 0:
                return {"error": f"scrot failed: {proc.stderr.strip()}"}

            data = base64.b64encode(_Path(path).read_bytes()).decode("ascii")

            # Get actual dimensions via identify
            id_proc = subprocess.run(
                ["identify", "-format", "%w %h", path],
                capture_output=True,
                text=True,
                timeout=5,
            )
            width, height = _display_width, _display_height
            if id_proc.returncode == 0:
                parts = id_proc.stdout.strip().split()
                if len(parts) == 2:
                    width, height = int(parts[0]), int(parts[1])

            return {
                "screenshot_base64": data,
                "width": width,
                "height": height,
                "format": "png",
            }
        finally:
            with contextlib.suppress(OSError):
                _Path(path).unlink()

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Mouse actions
# ---------------------------------------------------------------------------


def _validate_coords(x: Any, y: Any) -> tuple[int, int] | dict[str, str]:
    """Validate and convert coordinates. Returns (x, y) or error dict."""
    try:
        xi, yi = int(x), int(y)
    except (TypeError, ValueError):
        return {"error": f"Invalid coordinates: x={x}, y={y}"}
    if xi < 0 or yi < 0 or xi > _display_width or yi > _display_height:
        return {
            "error": f"Coordinates ({xi}, {yi}) outside display bounds (0-{_display_width}, 0-{_display_height})"
        }
    return (xi, yi)


@_handler("desktop_click")
async def _click(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Left click at (x, y) coordinates."""
    result = _validate_coords(args.get("x"), args.get("y"))
    if isinstance(result, dict):
        return result
    x, y = result

    def _run() -> dict[str, Any]:
        return _run_xdotool("mousemove", str(x), str(y), "click", "1")

    return await asyncio.to_thread(_run)


@_handler("desktop_double_click")
async def _double_click(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Double click at (x, y) coordinates."""
    result = _validate_coords(args.get("x"), args.get("y"))
    if isinstance(result, dict):
        return result
    x, y = result

    def _run() -> dict[str, Any]:
        return _run_xdotool("mousemove", str(x), str(y), "click", "--repeat", "2", "1")

    return await asyncio.to_thread(_run)


@_handler("desktop_right_click")
async def _right_click(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Right click at (x, y) coordinates."""
    result = _validate_coords(args.get("x"), args.get("y"))
    if isinstance(result, dict):
        return result
    x, y = result

    def _run() -> dict[str, Any]:
        return _run_xdotool("mousemove", str(x), str(y), "click", "3")

    return await asyncio.to_thread(_run)


@_handler("desktop_mouse_move")
async def _mouse_move(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Move cursor to (x, y) without clicking."""
    result = _validate_coords(args.get("x"), args.get("y"))
    if isinstance(result, dict):
        return result
    x, y = result

    def _run() -> dict[str, Any]:
        return _run_xdotool("mousemove", str(x), str(y))

    return await asyncio.to_thread(_run)


@_handler("desktop_drag")
async def _drag(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Drag from (start_x, start_y) to (end_x, end_y)."""
    start = _validate_coords(args.get("start_x"), args.get("start_y"))
    if isinstance(start, dict):
        return start
    end = _validate_coords(args.get("end_x"), args.get("end_y"))
    if isinstance(end, dict):
        return end
    sx, sy = start
    ex, ey = end

    def _run() -> dict[str, Any]:
        return _run_xdotool(
            "mousemove",
            str(sx),
            str(sy),
            "mousedown",
            "1",
            "mousemove",
            str(ex),
            str(ey),
            "mouseup",
            "1",
        )

    return await asyncio.to_thread(_run)


@_handler("desktop_scroll")
async def _scroll(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Scroll up or down. direction='up' or 'down', clicks=number of scroll steps."""
    direction = args.get("direction", "down")
    clicks = max(1, min(int(args.get("clicks", 3)), 20))
    # xdotool: button 4 = scroll up, button 5 = scroll down
    button = "4" if direction == "up" else "5"

    def _run() -> dict[str, Any]:
        return _run_xdotool("click", "--repeat", str(clicks), button)

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Keyboard actions
# ---------------------------------------------------------------------------


@_handler("desktop_type")
async def _type_text(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Type a text string at the current cursor position."""
    text = args.get("text", "")
    if not text:
        return {"error": "No text provided"}

    def _run() -> dict[str, Any]:
        return _run_xdotool("type", "--delay", "50", "--", text)

    return await asyncio.to_thread(_run)


@_handler("desktop_key")
async def _key(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Press a key combination (e.g. 'ctrl+a', 'Return', 'alt+F4')."""
    combo = args.get("key", "")
    if not combo:
        return {"error": "No key combination provided"}

    def _run() -> dict[str, Any]:
        return _run_xdotool("key", "--", combo)

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Window management
# ---------------------------------------------------------------------------


@_handler("desktop_window_list")
async def _window_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List all open windows with IDs, titles, and geometry."""

    def _run() -> dict[str, Any]:
        try:
            proc = subprocess.run(
                ["wmctrl", "-lG"],
                capture_output=True,
                text=True,
                timeout=5,
                env=_env(),
            )
            if proc.returncode != 0:
                return {"error": f"wmctrl failed: {proc.stderr.strip()}"}

            windows = []
            for line in proc.stdout.strip().splitlines():
                parts = line.split(None, 8)
                if len(parts) >= 8:
                    windows.append(
                        {
                            "id": parts[0],
                            "desktop": parts[1],
                            "x": int(parts[2]),
                            "y": int(parts[3]),
                            "width": int(parts[4]),
                            "height": int(parts[5]),
                            "host": parts[6],
                            "title": parts[7] if len(parts) > 7 else "",
                        }
                    )
            return {"windows": windows, "count": len(windows)}
        except Exception as e:
            return {"error": f"Failed to list windows: {e}"}

    return await asyncio.to_thread(_run)


@_handler("desktop_window_focus")
async def _window_focus(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Activate/focus a window by ID (from desktop_window_list)."""
    window_id = args.get("window_id", "")
    if not window_id:
        return {"error": "No window_id provided"}

    def _run() -> dict[str, Any]:
        try:
            proc = subprocess.run(
                ["wmctrl", "-ia", window_id],
                capture_output=True,
                text=True,
                timeout=5,
                env=_env(),
            )
            if proc.returncode != 0:
                return {"error": f"wmctrl focus failed: {proc.stderr.strip()}"}
            return {"focused": window_id}
        except Exception as e:
            return {"error": f"Failed to focus window: {e}"}

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Application launch
# ---------------------------------------------------------------------------


@_handler("desktop_launch")
async def _launch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Launch an application on the virtual display."""
    app = args.get("app", "")
    if not app:
        return {"error": "No app provided"}

    app_args = args.get("args", [])
    if isinstance(app_args, str):
        app_args = app_args.split()

    def _run() -> dict[str, Any]:
        try:
            cmd = [app, *app_args]
            proc = subprocess.Popen(
                cmd,
                env=_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Give the app a moment to start
            time.sleep(1)
            if proc.poll() is not None:
                return {"error": f"App '{app}' exited immediately with code {proc.returncode}"}
            return {"launched": app, "pid": proc.pid}
        except FileNotFoundError:
            return {"error": f"App not found: {app}"}
        except Exception as e:
            return {"error": f"Failed to launch app: {e}"}

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# VLM-powered screen description
# ---------------------------------------------------------------------------


@_handler("desktop_describe")
async def _describe(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Take a screenshot and describe the screen contents using a vision model."""
    prompt = args.get(
        "prompt",
        "Describe what you see on this screen in detail. List all visible windows, UI elements, text, buttons, and their approximate positions.",
    )

    # First, take the screenshot
    screenshot_result: dict[str, Any] = await _screenshot(args, ctx)
    if "error" in screenshot_result:
        return screenshot_result

    image_b64 = screenshot_result["screenshot_base64"]

    from robothor.engine.tools.dispatch import _cfg

    cfg = _cfg()

    # Try local Ollama vision model first
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"http://{cfg.ollama.host}:{cfg.ollama.port}/api/generate",
                json={
                    "model": cfg.ollama.vision_model,
                    "prompt": prompt,
                    "images": [image_b64],
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "description": data.get("response", ""),
                "width": screenshot_result["width"],
                "height": screenshot_result["height"],
                "model": cfg.ollama.vision_model,
            }
    except Exception as e:
        logger.warning("Local VLM failed for desktop_describe, returning screenshot only: %s", e)
        return {
            "description": f"[VLM unavailable: {e}] Screenshot captured but could not be analyzed.",
            "screenshot_base64": image_b64,
            "width": screenshot_result["width"],
            "height": screenshot_result["height"],
        }
