"""Evidence-based completion contracts (PR-3a).

Hermes Agent shipped "judges against evidence, not the model's say-so" as its
headline feature. This module grafts that pattern onto machinery this engine
already has: ``session_goal.py`` is already an evidence engine (typed,
validated evidence; a completion guard requiring a valid ``test_run`` AND a
valid ``commit``). What was missing is a check at run end that catches an
agent *claiming* a goal is done in prose without ever satisfying that guard.

Design:
  - Pure, deterministic core (``check_completion_contract``,
    ``_claims_completion``) — no I/O beyond one DAL read
    (``session_goal.get_active_goal``), unit-testable without a live LLM.
  - Flag-gated at the call site (``runner._after_response_delivered``), not
    here — this module always computes the verdict; the caller decides
    whether to log it (observe) or act on it (enforce). See
    ``feature_flags.completion_contract_mode``.
  - Scoping decision (documented, not a TODO): this only considers *session
    goals* (``session_goal.get_active_goal``). A run's originating crm_task
    (``run.task_id``) having a non-empty ``objective`` is a plausible second
    trigger, but "was this crm_task's completion claimed" is not cheaply
    detectable from the run alone (it would require reading task history /
    status transitions). Session-goal completion attempts are the
    unambiguous, cheaply-detectable case, so the check is scoped to those.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from robothor.engine.session_goal import get_active_goal, missing_completion_requirements

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.models import AgentRun

logger = logging.getLogger(__name__)

ContractStatus = Literal["satisfied", "missing"]


@dataclass(frozen=True)
class ContractVerdict:
    """The outcome of checking a completion claim against recorded evidence."""

    status: ContractStatus
    goal_id: str
    missing: list[str] = field(default_factory=list)


# Phrasings that plausibly claim a task/goal/objective is finished. Kept
# deliberately small and readable — this is a cheap heuristic gate, not a
# classifier; false negatives (a missed claim) are safe (no-op), false
# positives just trigger an evidence check that a genuinely-done run should
# pass anyway.
_COMPLETION_CLAIM_PATTERNS = [
    re.compile(
        r"\b(task|goal|objective)\s+is\s+(now\s+)?(complete|done|finished)\b", re.IGNORECASE
    ),
    re.compile(
        r"\bi(?:'ve| have)\s+(completed|finished)\s+the\s+(task|goal|objective)\b", re.IGNORECASE
    ),
    re.compile(r"\bmarking\s+(this|the)\s+(task|goal)\s+(as\s+)?complete\b", re.IGNORECASE),
    re.compile(r"\bthis\s+completes\s+the\s+(task|goal|objective)\b", re.IGNORECASE),
    re.compile(r"\b(task|goal|objective)\s+complete\b", re.IGNORECASE),
]
# A "not" (or similar) shortly before a match means the phrase is being
# negated ("the task is NOT complete") rather than claimed.
_NEGATION_RE = re.compile(r"\b(not|isn't|isnt|hasn't|hasnt|never)\b", re.IGNORECASE)
_NEGATION_WINDOW = 20


def _claims_completion(text: str | None) -> bool:
    """Return True iff ``text`` plausibly claims a task/goal is finished."""
    if not text:
        return False
    for pattern in _COMPLETION_CLAIM_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        window_start = max(0, match.start() - _NEGATION_WINDOW)
        preceding = text[window_start : match.start()]
        if _NEGATION_RE.search(preceding):
            continue
        return True
    return False


def check_completion_contract(run: AgentRun, config: EngineConfig) -> ContractVerdict | None:
    """Verify a run's completion claim against recorded, validated evidence.

    Applies only when the run has an active session goal (scoping decision
    above). Returns ``None`` when there's nothing to check: no active goal,
    or the run's output doesn't plausibly claim completion. Otherwise
    returns a ``ContractVerdict`` — ``satisfied`` when
    ``missing_completion_requirements`` finds nothing missing, ``missing``
    (with the reasons) otherwise.
    """
    goal = get_active_goal(tenant_id=run.tenant_id, agent_id=run.agent_id)
    if goal is None:
        return None
    if not _claims_completion(run.output_text):
        return None

    workspace = getattr(config, "workspace", None)
    missing = missing_completion_requirements(goal, workspace=workspace)
    if missing:
        return ContractVerdict(status="missing", goal_id=goal.id, missing=missing)
    return ContractVerdict(status="satisfied", goal_id=goal.id, missing=[])
