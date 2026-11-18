"""One structured log line per finished run, of any trigger.

``_finish_run`` is the one door every exit goes through, so it writes::

    event=run_end agent=<id> run_id=<id> trigger=<type> outcome=<outcome>
        duration_ms=<n> iterations=<n> input_tokens=<n> output_tokens=<n>
        total_tokens=<n>

``outcome`` is ``completed``, ``error``, ``cap`` (a token or cost budget
stopped it), ``timeout``, ``cancelled`` or the run's own status for anything
else. ``iterations`` counts the model calls that answered. ``completed`` logs
at INFO, the rest at WARNING. No prompt, output or error text.
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["log_run_end", "run_outcome"]

logger = logging.getLogger(__name__)
line_logger = logging.getLogger("robothor.engine.runs")

_BY_STATUS = {
    "completed": "completed",
    "failed": "error",
    "timeout": "timeout",
    "cancelled": "cancelled",
}


def run_outcome(run: Any) -> str:
    """The ``outcome`` field for ``run``."""
    status = str(getattr(getattr(run, "status", ""), "value", getattr(run, "status", "")))
    if getattr(run, "budget_exhausted", False):
        return "cap"
    return _BY_STATUS.get(status, status or "unknown")


def log_run_end(run: Any) -> None:
    """Write the line. Never raises."""
    try:
        from robothor.engine.log_fields import kv_line
        from robothor.engine.models import StepType

        steps = getattr(run, "steps", None) or []
        iterations = sum(
            1 for s in steps if s.step_type == StepType.LLM_CALL and not s.error_message
        )
        trigger = getattr(run, "trigger_type", "")
        input_tokens = int(getattr(run, "input_tokens", 0) or 0)
        output_tokens = int(getattr(run, "output_tokens", 0) or 0)
        outcome = run_outcome(run)
        line_logger.log(
            logging.INFO if outcome == "completed" else logging.WARNING,
            "%s",
            kv_line(
                "run_end",
                agent=getattr(run, "agent_id", ""),
                run_id=getattr(run, "id", ""),
                trigger=getattr(trigger, "value", trigger),
                outcome=outcome,
                duration_ms=getattr(run, "duration_ms", None) or 0,
                iterations=iterations,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )
    except Exception:  # noqa: BLE001 — a log line never blocks finalization
        logger.debug("run_end log line failed", exc_info=True)
