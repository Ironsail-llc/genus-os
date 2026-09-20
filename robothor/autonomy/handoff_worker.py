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


#: How often an owner with the switch ON is rescanned.
ACTIVE_POLL_SECONDS = 5
#: How often the daemon looks for an owner who has turned the switch on. With
#: nobody enabled it touches the handoff queue not at all.
IDLE_POLL_SECONDS = 60


async def recover_once(queue: HandoffChecks) -> int:
    """One pass, bound to the owners who have the feature enabled.

    Returns the number of owners scanned, which is zero when the feature is
    off everywhere -- and zero handoffs are read, claimed or expired in that
    case, because "off" has to mean the daemon does nothing.
    """
    scopes = await asyncio.to_thread(queue.enabled_scopes)
    for scope in scopes:
        await asyncio.to_thread(queue.release_expired, scope)
        for handoff_id in await asyncio.to_thread(queue.candidates, scope):
            await check_one(queue, scope, handoff_id)
    return len(scopes)


async def recover_checks() -> None:
    """All bridge workers may scan; a database lease admits only one checker."""
    while True:
        active = False
        try:
            active = await recover_once(HandoffChecks(AutonomyStore())) > 0
        except Exception:
            logger.warning("External verification recovery temporarily unavailable")
        await asyncio.sleep(ACTIVE_POLL_SECONDS if active else IDLE_POLL_SECONDS)


#: Six hours. The retention window is measured in months, so the sweep only
#: has to be regular, not prompt, and a missed pass costs nothing but a few
#: more hours of retention. Running it oftener would mean every bridge worker
#: issuing the same no-op DELETE against a growing table for no gain.
PURGE_PERIOD_SECONDS = 6 * 60 * 60


async def purge_expired_observations(period: float = PURGE_PERIOD_SECONDS) -> None:
    """Delete observations past their retention window, forever.

    The archive of rendered review pages had no expiry at all: an operation
    the owner ran once kept their name, date of birth, address and the
    answers they gave a website indefinitely, sealed with a key derived from
    the vault master key. ``AutonomyStore.purge_expired`` is the sweep and
    ``autonomy.terms_retention_days`` is the window; this only runs it.

    Every bridge worker may call it — the DELETE is idempotent and the rows
    it removes are gone for everybody — so no lease is taken. A failure is a
    warning and never a crash: a retention sweep is not worth taking the
    bridge down for, and the next pass picks up whatever this one missed.
    """
    while True:
        await asyncio.sleep(period)
        try:
            removed = await asyncio.to_thread(AutonomyStore().purge_expired)
            if removed["terms_snapshots"]:
                logger.info(
                    "Retention sweep removed %s expired observation(s)",
                    removed["terms_snapshots"],
                )
        except Exception:
            logger.warning("Observation retention sweep temporarily unavailable")
