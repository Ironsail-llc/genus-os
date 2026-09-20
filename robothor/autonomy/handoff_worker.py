"""Restart-safe checks of stored confirmations; no form or payment submission."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from robothor.autonomy.broker import ExecutionPlan
from robothor.autonomy.handoff_recovery import HandoffChecks
from robothor.autonomy.runtime import run_browser
from robothor.autonomy.store import AutonomyStore

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope

logger = logging.getLogger(__name__)


async def check_one(queue: HandoffChecks, scope: Scope, handoff_id: str) -> None:
    claim = await asyncio.to_thread(queue.claim, scope, handoff_id)
    if not claim:
        return
    # This timeout is shorter than the durable lease. Runtime cancellation kills
    # the isolated browser process; a crashed service recovers after lease expiry.
    # Do not catch cancellation: it must leave a recoverable checking record.
    retry = True
    try:
        confirmation = await asyncio.to_thread(
            queue.confirmation, scope, handoff_id, claim["token"]
        )
        plan = ExecutionPlan(
            url=confirmation["url"],
            submit_selector="__unused__",
            success_selector=confirmation["selector"],
            success_text=confirmation["text"],
            session_resource_id=confirmation.get("session_resource_id"),
        )
        result = await asyncio.wait_for(
            run_browser(
                scope,
                claim["operation_id"],
                claim["agent_id"],
                plan,
                reconcile=True,
            ),
            timeout=185,
        )
        retry = result.get("reason") in {"broker_unavailable", "broker_interrupted"} or bool(
            result.get("error")
        )
    except Exception:
        # Never emit private URLs, encrypted plans, browser errors or credentials.
        retry = True
    with contextlib.suppress(Exception):
        await asyncio.to_thread(queue.finish, scope, handoff_id, claim["token"], retry=retry)


async def recover_checks() -> None:
    """All bridge workers may scan; a database lease admits only one checker."""
    while True:
        try:
            queue = HandoffChecks(AutonomyStore())
            candidates = await asyncio.to_thread(queue.candidates)
            for scope, handoff_id in candidates:
                await check_one(queue, scope, handoff_id)
        except Exception:
            logger.warning("External verification recovery temporarily unavailable")
        await asyncio.sleep(5)
