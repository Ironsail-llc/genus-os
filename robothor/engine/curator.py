"""Skill curator — Rip 5.

Idle-triggered consolidation pass over agent-created skills. The
per-turn background-review fork (Rip 1) adds skills and patches;
the curator runs on a slower cadence (default every 7 days, only
when no session has been active for the configured min-idle window)
and proposes higher-level structural cleanups: consolidate near-
duplicate umbrellas, archive cold ones, demote one-off entries to
references under an umbrella.

The curator only touches skills with ``is_agent_created=True``
(stamped by Rip 4's provenance ContextVar). Human-authored skills
are never archived or consolidated.

For the first 2 weeks of any tenant's rollout the curator runs in
**dry-run** mode — it writes proposed actions to ``crm_curator_state``
without applying any. The operator inspects the proposals, then
flips ``dry_run=False`` to enable real consolidation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robothor.engine.skills import SkillDefinition

logger = logging.getLogger(__name__)


# Cadence defaults — overridable per-tenant via engine config / env.
CURATOR_DEFAULT_INTERVAL_DAYS = 7
CURATOR_DEFAULT_MIN_IDLE_HOURS = 2


# Ported from Hermes ``agent/curator.py:330-460``. Paths adjusted
# for Genus's repo layout. The do-not-capture guardrail from the
# per-turn review (Rip 1) is carried into the curator prompt
# because consolidation is also a write surface — bad merges can
# also harden environment-dependent failures into durable rules.
CURATOR_REVIEW_PROMPT = (
    "You are the skill-library CURATOR. Your job is structural cleanup over the "
    "agent-created skills below, not per-session learning. Be DELIBERATE — most "
    "passes should produce one to three changes; a pass that proposes nothing is "
    "fine when the library is already tidy.\n\n"
    "Inputs you have:\n"
    "  • list_skills + skill_view to inspect candidates.\n"
    "  • update_skill to patch; create_skill to make a new umbrella; skill_archive "
    "to move a skill to agents/skills/.archive/.\n\n"
    "Look for:\n"
    "  1. Near-duplicates: two or more skills covering the same class of work. "
    "Merge into the strongest umbrella; archive the rest.\n"
    "  2. One-offs that should be references: skills that captured a session-"
    "specific recipe rather than a class. Demote to "
    "`references/<topic>.md` under an existing umbrella; archive the standalone.\n"
    "  3. Cold skills with usage_count = 0 and creation > 30 days ago: archive.\n"
    "  4. Naming drift: skills that violate the class-level convention (Rip 2). "
    "Rename via update_skill, then archive the old name.\n\n"
    "Hard rules:\n"
    "  • Only touch skills where is_agent_created=True. Human-authored skills "
    "(is_agent_created=False) are off-limits — list_skills shows the flag.\n"
    "  • Pinned skills (meta.pinned=true) get content updates but NEVER archive "
    "or consolidation — pin is the operator's explicit 'keep this'.\n"
    "  • Do NOT capture environment-dependent failures or negative tool claims as "
    "new rules during consolidation. Same do-not-capture list as the per-turn "
    "review prompt.\n\n"
    "If the library is clean, say 'No consolidation needed.' and stop. Otherwise "
    "act, then summarise the actions taken in your final reply."
)


@dataclass
class CuratorResult:
    """Structured outcome of one curator pass."""

    tenant_id: str
    dry_run: bool
    candidates_inspected: int
    proposed_archive: list[str] = field(default_factory=list)
    proposed_merge: list[tuple[str, str]] = field(default_factory=list)  # (from, into)
    proposed_demote: list[str] = field(default_factory=list)
    skipped_pinned: list[str] = field(default_factory=list)
    skipped_human_authored: list[str] = field(default_factory=list)
    summary: str = ""

    def total_actions(self) -> int:
        return len(self.proposed_archive) + len(self.proposed_merge) + len(self.proposed_demote)


def list_curator_candidates(
    skills: dict[str, SkillDefinition] | None = None,
    meta_loader=None,  # type: ignore[no-untyped-def]
) -> tuple[list[SkillDefinition], list[str], list[str]]:
    """Return (candidates, skipped_pinned, skipped_human) for one pass.

    Candidates are skills with ``meta.is_agent_created=True`` and NOT
    ``meta.pinned=True``. Human-authored and pinned skills are
    returned in separate lists so the curator report can call them
    out without touching them.

    ``meta_loader`` lets tests inject a fake meta lookup; defaults
    to ``robothor.engine.skills.read_skill_meta``.
    """
    from robothor.engine.skills import load_skills, read_skill_meta

    if skills is None:
        skills = load_skills()
    if meta_loader is None:
        meta_loader = read_skill_meta

    candidates: list[SkillDefinition] = []
    pinned: list[str] = []
    human: list[str] = []

    for name, defn in skills.items():
        meta = meta_loader(name) or {}
        if meta.get("pinned"):
            pinned.append(name)
            continue
        if not meta.get("is_agent_created"):
            human.append(name)
            continue
        candidates.append(defn)

    return candidates, pinned, human


def should_run_curator(
    last_pass_at,  # type: ignore[no-untyped-def]
    *,
    now=None,  # type: ignore[no-untyped-def]
    interval_days: int = CURATOR_DEFAULT_INTERVAL_DAYS,
) -> bool:
    """Cadence gate: True iff at least ``interval_days`` have elapsed."""
    from datetime import UTC, datetime, timedelta

    now = now or datetime.now(UTC)
    if last_pass_at is None:
        return True
    return (now - last_pass_at) >= timedelta(days=interval_days)


# NOTE: the actual fork spawn re-uses Rip 1's spawn_background_review
# infrastructure — the curator is just "the same fork pattern with a
# different prompt + a candidate filter". The next commit wires the
# scheduler tick that calls into spawn_background_review with
# decision.prompt = CURATOR_REVIEW_PROMPT and a candidate list
# rendered into the transcript tail.
