"""Read-only capability checks executed inside the running engine process."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any


async def probe(workspace: str) -> dict[str, Any]:
    from robothor.engine import host_execution
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import browser, desktop

    ctx = ToolContext(agent_id="capability-probe", run_id=str(uuid.uuid4()), workspace=workspace)
    evidence: dict[str, Any] = {
        "loaded_module": str(Path(browser.__file__).resolve()),
        "host_execution": await host_execution.health(),
    }
    try:
        start = await browser._action_start({}, ctx)
        evidence["browser_start"] = start
        if "error" not in start:
            session = await browser._get_session(browser._session_key(ctx))
            assert session is not None, "Browser reported started without a session"
            await session.page.goto(
                "data:text/html,<title>Genus capability probe</title>"
                '<h1>Computer tools work</h1><input aria-label="Probe">'
            )
            await session.page.get_by_role("textbox", name="Probe").fill("verified")
            snapshot = await session.page.locator(":root").aria_snapshot()
            screenshot = await session.page.screenshot()
            evidence["browser"] = {
                "ready": "Computer tools work" in snapshot,
                "input": await session.page.get_by_role("textbox").input_value(),
                "screenshot_bytes": len(screenshot),
            }
        shot = await desktop._screenshot({}, ctx)
        evidence["desktop"] = {
            "ready": bool(shot.get("screenshot_base64")),
            "width": shot.get("width"),
            "height": shot.get("height"),
            "error": shot.get("error"),
        }
    finally:
        await browser._action_stop({}, ctx)
    evidence["ready"] = bool(
        evidence.get("browser", {}).get("ready")
        and evidence.get("desktop", {}).get("ready")
        and evidence["host_execution"].get("available")
    )
    return evidence
