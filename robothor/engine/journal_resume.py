"""Wake a scheduled agent up knowing where it left off.

Extracted from `execute`. An agent with `resume_on_start` and a `journal_file`
gets its journal rendered as a preamble to the incoming message, so a run that
picks up a long-lived experiment does not restart it.

The trigger gate is the invariant worth stating: only CRON, HOOK and WORKFLOW
runs resume. An interactive run already has a human telling it what to do, and
prepending "here is where you left off" to that would answer a question nobody
asked — and could steer the agent back to yesterday's task.

Failing to load a journal is non-fatal. A corrupt or missing journal costs the
agent its continuity, not its run.
"""

from __future__ import annotations

import logging
from typing import Any

from robothor.engine.sanitize import sanitize_log as _sanitize

logger = logging.getLogger(__name__)


def maybe_prepend_journal_resume(
    message: str,
    *,
    agent_id: str,
    agent_config: Any,
    trigger_type: Any,
    workspace: Any,
) -> str:
    """The message the agent should receive, journal preamble included."""
    from robothor.engine.models import TriggerType

    resumable = (TriggerType.CRON, TriggerType.HOOK, TriggerType.WORKFLOW)
    if trigger_type not in resumable:
        return message
    if not (agent_config.resume_on_start and agent_config.journal_file):
        return message

    try:
        from robothor.engine.journal import JournalManager

        state = JournalManager.load(agent_id, agent_config.journal_file, workspace)
        if not state:
            return message

        preamble = JournalManager.format_resume_preamble(state)
        logger.info(
            "Journal resume injected for %s: experiment=%s iteration=%d next_action=%s",
            _sanitize(agent_id),
            _sanitize(state.experiment_id),
            state.iteration,
            _sanitize(state.next_action),
        )
        return f"{preamble}\n\n{message}"
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Journal resume failed for %s (non-fatal): %s", _sanitize(agent_id), _sanitize(e)
        )
        return message
