"""Scan an assembled unattended prompt before the model sees it.

Extracted from `execute`. Cron, hook and workflow runs are unattended, and the
recalled memory, skills and context files folded into their prompt could carry
an injection. Interactive runs are excluded: a human typed the message and is
watching the result.

The ORDER of what happens on a block is the subtle part, and both steps encode
a live defect:

1. mark the run FAILED, then INSERT it. `_finish_run`'s persistence is a
   *background* task, and a short-lived caller (the CLI) exits before it lands
   — stranding the row in 'pending'. Inserting the already-terminal state is
   the one write guaranteed to survive.
2. log the guardrail event only AFTER the row exists.
   `agent_guardrail_events.run_id` is an FK to `agent_runs`, so logging first
   violates it and the audit event is lost.

Both were live: enforce-mode blocks were invisible to the soak report and left
'pending' runs behind. A security control that fires and leaves no trace is
indistinguishable from one that never fired.

Teardown stays with the caller. The watchdog token and `_finish_run` belong to
the runner, and a screening function has no business ending a run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from robothor.engine.sanitize import sanitize_log as _sanitize

logger = logging.getLogger(__name__)

GUARDRAIL_NAME = "injection_scan"


@dataclass
class ScreenVerdict:
    """What the caller must do next."""

    blocked: bool = False
    #: The already-persisted, already-FAILED run. The caller finishes it.
    blocked_run: Any = None
    #: An observe-mode finding, already audited. Informational.
    finding: str = ""


async def screen_run_prompt(
    session: Any,
    *,
    agent_id: str,
    trigger_type: Any,
    system_prompt: str,
    message: str,
) -> ScreenVerdict:
    """Screen an unattended run's prompt. Never raises."""
    from robothor.engine.models import TriggerType

    unattended = (TriggerType.CRON, TriggerType.HOOK, TriggerType.WORKFLOW)
    if trigger_type not in unattended:
        return ScreenVerdict()

    from robothor.engine.cron_safety import (
        CronPromptInjectionBlockedError,
        screen_cron_prompt,
    )

    try:
        finding = screen_cron_prompt(
            f"{system_prompt}\n{message}", context=f"{trigger_type.value}:{agent_id}"
        )
    except CronPromptInjectionBlockedError as exc:
        return await _block(session, exc)

    if finding:
        _audit(session.run.id, action="observed", reason=finding, mode="observe")
    return ScreenVerdict(finding=finding or "")


async def _block(session: Any, exc: Exception) -> ScreenVerdict:
    from robothor.engine.tracking import create_run

    # 1. Terminal state first, then the INSERT — see the module docstring.
    blocked_run = session.fail(f"Blocked by injection scan: {exc}")
    with contextlib.suppress(Exception):
        await asyncio.to_thread(create_run, blocked_run)

    # 2. Only now, because run_id is an FK to the row written above.
    _audit(blocked_run.id, action="blocked", reason=str(exc), mode="enforce")
    return ScreenVerdict(blocked=True, blocked_run=blocked_run)


def _audit(run_id: Any, *, action: str, reason: str, mode: str) -> None:
    """A control fired; losing its audit trail is itself an incident."""
    try:
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(
            run_id=run_id,
            guardrail_name=GUARDRAIL_NAME,
            action=action,
            tool_name=None,
            reason=reason,
            mode=mode,
            step_number=0,
        )
    except Exception as exc:  # noqa: BLE001 - never silent
        logger.error(
            "injection_scan %s on run %s but the guardrail event could not be recorded: %s",
            action,
            _sanitize(str(run_id)),
            _sanitize(exc),
        )
