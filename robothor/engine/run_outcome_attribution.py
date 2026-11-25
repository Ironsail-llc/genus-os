"""Run-end outcome attribution for the memories a run recalled.

Extracted from delivery.py (held by the module-size ratchet): this cluster
decides what a finished run's outcome says about the facts its recalls
returned, and hands the write to ``robothor.memory.outcomes``. Its only inputs
are the run and its agent config; delivery calls it once, first thing, and
nothing else in delivery depends on it.

A failed run blames every fact it recalled (``bump_failure_for_run``). A
successful run credits them and records which ones its answer used
(``credit_success_for_run``). Any other ending — timeout, cancellation, skip,
a run still pending or awaiting approval — is neither. Benchmark runs earn no
credit. The writes run in a worker thread; a failure is logged, never raised,
so it cannot look like the run itself failed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.models import AgentRun

logger = logging.getLogger(__name__)


def _status(run: AgentRun) -> str:
    return str(getattr(run.status, "value", run.status) or "").lower()


def run_failed(run: AgentRun) -> bool:
    """Whether a finished run counts against what it recalled."""
    return bool(run.error_message) or _status(run) == "failed"


def run_succeeded(run: AgentRun) -> bool:
    """Whether a finished run is evidence that what it recalled helped.

    Only a run that COMPLETED, raised no error, did not stop on its budget and
    produced output. Timeouts, cancellations, skips and runs still pending or
    awaiting approval are neither success nor failure.
    """
    return (
        _status(run) == "completed"
        and not run.error_message
        and not getattr(run, "budget_exhausted", False)
        and bool((run.output_text or "").strip())
    )


async def attribute_run_outcome(config: Any, run: AgentRun) -> None:
    """Credit or blame the facts a finished run recalled. Never raises."""
    try:
        if not run.id:
            return
        from robothor.memory import outcomes

        tenant_id = getattr(run, "tenant_id", None) or getattr(config, "tenant_id", None)
        if run_failed(run):
            await asyncio.to_thread(outcomes.bump_failure_for_run, str(run.id), tenant_id)
        elif run_succeeded(run) and not getattr(run, "is_benchmark", False):
            await asyncio.to_thread(
                outcomes.credit_success_for_run,
                str(run.id),
                tenant_id,
                output_text=run.output_text,
                task_text=getattr(run, "task_text", None),
            )
    except Exception as e:  # noqa: BLE001 - attribution is best-effort
        logger.debug("Outcome attribution failed (non-fatal): %s", e)
