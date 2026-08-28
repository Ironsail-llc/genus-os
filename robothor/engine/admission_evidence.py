"""How a deferral is recorded, kept apart from whether one happens.

`admission.py` answers one question -- does this run get a slot -- and that is
all it should have to hold. Persisting the verdict is a separate concern with
separate failure modes: it touches the database, it is best-effort, and it must
never be able to change the answer. Splitting it keeps the guard readable and
keeps this file free to grow the schema-shaped detail it needs.

The row matters as much as the refusal. FleetPool ran its whole existence with
no production caller and nothing distinguished "never triggered" from "never
called"; a shadow row in observe mode is what turns a promotion decision into
an evidence-based one.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def record_deferral(agent_id: str, reason: str, mode: str, priority: str) -> None:
    """Leave a row saying the gate fired. Best-effort, never raises.

    A guardrail event needs a run to point at (``run_id`` is an FK), so this
    writes a terminal SKIPPED run first -- the shape ``runner.py`` already uses
    for refusals. ``observed`` marks the shadow verdict in observe mode, which
    is what makes promotion evidence-based instead of hopeful.
    """
    from robothor.engine.models import AgentRun, RunStatus, TriggerType
    from robothor.engine.tracking import create_run, log_guardrail_event

    run_id = create_run(
        AgentRun(
            agent_id=agent_id,
            trigger_type=TriggerType.CRON,
            trigger_detail=f"admission:{mode}",
            status=RunStatus.SKIPPED,
            error_message=f"Deferred by admission control: {reason}",
        )
    )
    log_guardrail_event(
        run_id,
        "execution_mode_admission",
        "blocked" if mode == "enforce" else "observed",
        reason=f"{priority}: {reason}",
        mode=mode,
    )
