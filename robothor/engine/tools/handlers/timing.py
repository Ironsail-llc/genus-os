"""Timing tool handlers — wait/sleep for polling patterns."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

_MAX_WAIT = 300  # 5 minutes


async def _wait_seconds(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Pause execution for the requested number of seconds (capped at 300)."""
    raw = args.get("seconds", 0)
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return {"error": f"seconds must be an integer, got: {raw!r}"}

    if seconds < 1:
        return {"error": "seconds must be at least 1"}

    seconds = min(seconds, _MAX_WAIT)
    await asyncio.sleep(seconds)
    return {"waited": seconds}


async def _register_user_cron(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Schedule a future/recurring run of THIS agent with a custom prompt.

    Translates a natural-language schedule ("every 30m", "in 2 hours",
    "0 9 * * 1-5") into a stored cron job. Sub-minute schedules and prompts
    that trip the injection scanner are rejected.
    """
    from robothor.engine.cron_parse import parse_schedule
    from robothor.engine.cron_safety import scan_assembled_cron_prompt
    from robothor.engine.user_cron import create_user_cronjob

    schedule_text = str(args.get("schedule", "")).strip()
    prompt = str(args.get("prompt", "")).strip()
    if not schedule_text or not prompt:
        return {"error": "schedule and prompt are required"}
    try:
        schedule = parse_schedule(schedule_text)
    except ValueError as e:
        return {"error": f"invalid schedule: {e}"}
    finding = scan_assembled_cron_prompt(prompt)
    if finding is not None:
        return {"error": f"prompt rejected (injection signal): {finding}"}

    raw_max = args.get("max_fires")
    max_fires = int(raw_max) if raw_max not in (None, "") else None
    result = await asyncio.to_thread(
        create_user_cronjob,
        agent_id=ctx.agent_id,
        prompt=prompt,
        schedule=schedule,
        tenant_id=ctx.tenant_id,
        created_by_session=getattr(ctx, "run_id", None),
        max_fires=max_fires,
    )
    return {"registered": True, "schedule_kind": schedule["kind"], **result}


HANDLERS["wait_seconds"] = _wait_seconds
HANDLERS["register_user_cron"] = _register_user_cron
