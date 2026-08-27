"""Record what one tool call did.

Four steps that share one subject — the call that just finished — and one
rule: none of them may take the run down. Extracted from the tool-execution
block inside `_run_loop`, the largest cluster left in the god-object the
competitive analysis puts first.

    classify -> log -> record on the scratchpad -> trip the breaker

Two details are load-bearing and were untested inline. The scratchpad is given
the RESULT and the ARGS, not just the tool name: they feed the no-progress
detector, and without them every call looks identical so it can never fire.
And the circuit breaker speaks to the agent rather than raising — an agent
that keeps calling a broken tool burns its whole budget on it, but a tool
failing three times is not a reason to end the run.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from robothor.engine.session import ENGINE_CONTEXT_ROLE

logger = logging.getLogger(__name__)

#: Failures of the same tool, in one run, before the agent is told to stop.
CIRCUIT_BREAKER_THRESHOLD = 3


def record_tool_outcome(
    session: Any,
    *,
    tool_name: str,
    tool_args: Any,
    result: Any,
    error_msg: str | None,
    elapsed_ms: int,
    scratchpad: Any,
    failures: dict[str, int],
) -> Any:
    """Classify, log, record and count one finished tool call.

    Returns the classified error type (or None on success) — consumed
    downstream by escalation, so it is part of the contract rather than a
    convenience.
    """
    error_type = _classify(tool_name, error_msg)
    _log(session, tool_name, elapsed_ms, error_msg, error_type)
    _record_on_scratchpad(scratchpad, tool_name, tool_args, result, error_msg)
    _count_failure(session, tool_name, error_msg, failures)
    return error_type


def _classify(tool_name: str, error_msg: str | None) -> Any:
    if not error_msg:
        return None
    from robothor.engine.error_recovery import classify_error

    return classify_error(tool_name, error_msg)


def _log(
    session: Any, tool_name: str, elapsed_ms: int, error_msg: str | None, error_type: Any
) -> None:
    """Observability is not worth the work the agent has already done."""
    with contextlib.suppress(Exception):
        from robothor.engine.tracking import log_tool_event

        log_tool_event(
            run_id=session.run.id,
            tool_name=tool_name,
            duration_ms=elapsed_ms,
            success=error_msg is None,
            error_type=error_type,
            error_message=error_msg,
        )


def _record_on_scratchpad(
    scratchpad: Any, tool_name: str, tool_args: Any, result: Any, error_msg: str | None
) -> None:
    if not scratchpad:
        return
    with contextlib.suppress(Exception):
        scratchpad.record_tool_call(
            tool_name,
            error=error_msg,
            result=result,
            tool_input=tool_args,
        )


def _count_failure(
    session: Any, tool_name: str, error_msg: str | None, failures: dict[str, int]
) -> None:
    if not error_msg:
        return
    failures[tool_name] = failures.get(tool_name, 0) + 1
    if failures[tool_name] < CIRCUIT_BREAKER_THRESHOLD:
        return
    # At-or-above, so the nudge repeats on every further failure. That is the
    # pre-existing behaviour and it is kept deliberately: an agent that called
    # the tool again after being told not to is exactly the agent that needs
    # telling again. Changing it here would be a behaviour change smuggled
    # into a refactor.
    session.messages.append(
        {
            "role": ENGINE_CONTEXT_ROLE,
            "content": (
                f"[SYSTEM] Tool '{tool_name}' has failed "
                f"{failures[tool_name]} times this run. "
                "Do NOT call it again. Find an alternative "
                "approach or skip this step and move on."
            ),
        }
    )
