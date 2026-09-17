"""What the loop does about a tool that just failed, before escalating.

Extracted from `_run_loop`. Four remedies with very different costs — sleeping,
nudging, spawning a whole helper agent, injecting guidance — chosen by
`get_recovery_action` from the error's type and how often it has repeated.

The conditions are the interesting part, and none of them was reachable by a
test while this lived inline:

* plan mode recovers nothing: a read-only run has nothing to retry.
* an unclassified error is skipped, because the recommender keys on the type
  and guessing one applies the wrong remedy.
* spawning is gated on the agent being permitted to spawn at all, and the
  budget only counts a helper that actually came back — charging for a failed
  spawn would exhaust it on nothing.
* `applied` suppresses the error-feedback prompt that follows in the loop.
  Doing both tells the agent to analyse a failure the platform just handled.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from robothor.engine.session import ENGINE_CONTEXT_ROLE

logger = logging.getLogger(__name__)


@dataclass
class RecoveryOutcome:
    applied: bool
    helper_spawns_used: int


async def apply_error_recovery(
    session: Any,
    agent_config: Any,
    *,
    iteration_errors: list[tuple[str, str, Any]],
    escalation: Any,
    readonly_mode: bool,
    helper_spawns_used: int,
    spawn_helper: Any,
) -> RecoveryOutcome:
    """Try to recover from this iteration's tool failures."""
    applied = False
    if not iteration_errors or readonly_mode:
        return RecoveryOutcome(applied=False, helper_spawns_used=helper_spawns_used)

    from robothor.engine.error_recovery import get_recovery_action

    for err_tool, err_msg, err_type in iteration_errors:
        if err_type is None:
            continue
        consecutive = escalation.consecutive_errors if escalation else 1
        logger.debug(
            "Error recovery: tool=%s type=%s consecutive=%d spawns_used=%d",
            err_tool,
            err_type,
            consecutive,
            helper_spawns_used,
        )
        action = get_recovery_action(
            error_type=err_type,
            consecutive_count=consecutive,
            agent_config=agent_config,
            tool_name=err_tool,
            error_msg=err_msg,
            helper_spawns_used=helper_spawns_used,
        )
        if action is None:
            continue
        logger.debug("Error recovery: action=%s for %s", action.action, err_tool)

        if action.action == "backoff":
            await asyncio.sleep(action.delay_seconds)
            _say(session, f"[SYSTEM] {action.message} Retrying now.")
            applied = True

        elif action.action == "retry":
            _say(session, f"[SYSTEM] {action.message}")
            applied = True

        elif action.action == "spawn" and agent_config.can_spawn_agents:
            logger.debug("Error recovery: spawning helper for %s", err_tool)
            helper_result = await spawn_helper(action)
            if helper_result:
                helper_spawns_used += 1
                _say(
                    session,
                    f"[ERROR RECOVERY — Helper agent result]\n{helper_result}\n\n"
                    "Use this information to adjust your approach.",
                )
                applied = True

        elif action.action == "inject":
            _say(session, f"[SYSTEM — Recovery guidance] {action.message}")
            applied = True

    return RecoveryOutcome(applied=applied, helper_spawns_used=helper_spawns_used)


def _say(session: Any, content: str) -> None:
    session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": content})


def inject_error_feedback(
    session: Any,
    agent_config: Any,
    *,
    iteration_errors: list[tuple[str, str, Any]],
    escalation: Any,
    recovery_applied: bool,
) -> None:
    """Tell the agent WHY this iteration's tool calls failed, or say nothing.

    Lifted out of ``_run_loop`` where it was inline: it belongs beside the
    recovery above, because the two are mutually exclusive by design —
    ``recovery_applied`` suppresses it, since doing both asks the agent to
    analyse a failure the platform has just handled.
    """
    if not (iteration_errors and agent_config.error_feedback) or recovery_applied:
        return
    listed = "\n".join(f"- {name}: {msg}" for name, msg, _etype in iteration_errors)
    # STOP RETRYING hints for error types repeated >= 2 times.
    hints = escalation.get_repeated_error_hints(threshold=2) if escalation else []
    suffix = ("\n\n" + "\n".join(hints)) if hints else ""
    _say(
        session,
        f"[SYSTEM] The following tool calls failed:\n{listed}\n\n"
        "Analyze why these failed. Consider:\n"
        "1. Were the arguments correct?\n"
        "2. Is there an alternative approach or different tool?\n"
        "3. Should you skip this step and continue?\n"
        "Do NOT retry the exact same call with the same arguments."
        f"{suffix}",
    )
