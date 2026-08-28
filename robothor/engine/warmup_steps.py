"""Record setup milestones as steps, so a stall is visible where runs are read.

Extracted from `execute`. Warmup happens before the first iteration, so a run
that stalls there shows nothing in `agent_run_steps` — only watchdog touch
logs, which nobody reads until something has already gone wrong. Recording
each phase as a `warmup_phase` step puts the stall in the same place as the
rest of the run.

Per-section timings matter more than the total: knowing warmup took 40s says
nothing, knowing `memory_blocks` took 39 of them says everything. That
granularity is what the fleet-wide warmup-stall investigation needed.

Every step is best-effort and independent. Losing one timing to a bad value
must not cost the others, and none of it is worth failing a run over.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from typing import Any

logger = logging.getLogger(__name__)

#: A section slower than this is flagged in its step metadata. Half a second
#: of setup is unremarkable on its own and unmistakable in aggregate — it is a
#: hint for whoever reads the run, not a threshold anything enforces.
SLOW_SECTION_SECONDS = 0.5


def record_warmup_steps(
    session: Any,
    *,
    prompt_ms: int,
    prompt_cached: bool,
    warmup_ms: int,
    warmup_kind: str,
    warmup_chars: int,
    section_timings: dict[str, float],
) -> None:
    """Append one `warmup_phase` step per setup milestone."""
    steps: list[tuple[str, int, dict[str, Any]]] = [
        ("system_prompt_build", prompt_ms, {"cached": "hit" if prompt_cached else "miss"}),
        (
            "warmup_preamble_build",
            warmup_ms,
            {"kind": warmup_kind or "none", "chars": warmup_chars},
        ),
    ]
    # Section granularity: "warmup took 40s" says nothing, "memory_blocks took
    # 39 of them" says everything.
    for name, elapsed in (section_timings or {}).items():
        steps.append(
            (
                f"warmup_section:{name}",
                int(elapsed * 1000),
                {"section": name, "slow": elapsed > SLOW_SECTION_SECONDS},
            )
        )

    for name, duration_ms, meta in steps:
        _append(session, name, duration_ms, meta)


def _append(session: Any, name: str, duration_ms: int, meta: dict[str, Any]) -> None:
    """One step, independently. A bad value must not cost the others."""
    try:
        from robothor.engine.models import RunStep, StepType

        session.run.steps.append(
            RunStep(
                id=str(_uuid.uuid4()),
                run_id=session.run.id,
                # Pre-iteration; the grader ignores step_number for
                # warmup_phase, so 0 is the marker rather than an index.
                step_number=0,
                step_type=StepType.WARMUP_PHASE,
                tool_name=name,
                tool_input={},
                tool_output=meta,
                duration_ms=duration_ms,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("warmup_phase step record failed (%s): %s", name, exc)
