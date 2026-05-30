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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

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
    meta_loader: Callable[[str], dict[str, Any] | None] | None = None,
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
    last_pass_at: datetime | None,
    *,
    now: datetime | None = None,
    interval_days: int = CURATOR_DEFAULT_INTERVAL_DAYS,
) -> bool:
    """Cadence gate: True iff at least ``interval_days`` have elapsed."""
    now = now or datetime.now(UTC)
    if last_pass_at is None:
        return True
    return bool((now - last_pass_at) >= timedelta(days=interval_days))


_CURATOR_STATE_BLOCK = "curator_state"


def _curator_tool_whitelist() -> frozenset[str]:
    """Review-fork tools PLUS skill_archive (the per-turn fork never archives)."""
    from robothor.engine.background_review import REVIEW_TOOL_WHITELIST

    return REVIEW_TOOL_WHITELIST | frozenset({"skill_archive"})


async def spawn_curator(
    scheduler: Any,
    *,
    curator_agent_id: str = "curator",
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Run one LLM consolidation pass as a top-level ``curator`` agent run.

    Drives a real top-level run via ``scheduler._run_agent`` (which installs a
    fresh spawn context) rather than ``spawn_background_review`` (which needs an
    active spawn context and would fail from a daemon tick). The CURATOR write
    origin tags its skill writes agent-created; the tool whitelist hard-limits it
    to memory+skill tools plus skill_archive. Never raises — a failed pass must
    not take down the daemon loop.
    """
    from robothor.engine.skill_provenance import (
        CURATOR,
        reset_current_write_origin,
        set_current_write_origin,
    )
    from robothor.engine.tools.dispatch import clear_tool_whitelist, set_tool_whitelist

    candidates, pinned, human = list_curator_candidates()
    if not candidates:
        logger.info("spawn_curator: no agent-created candidates; skipping LLM pass")
        return {"status": "skipped", "reason": "no_candidates"}

    origin_token = set_current_write_origin(CURATOR)
    whitelist_token = set_tool_whitelist(_curator_tool_whitelist())
    try:
        await scheduler._run_agent(curator_agent_id)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 — background, never propagate
        logger.warning("spawn_curator run failed: %s", exc)
        return None
    finally:
        clear_tool_whitelist(whitelist_token)
        reset_current_write_origin(origin_token)

    logger.info(
        "curator pass complete: candidates=%d pinned_skipped=%d human_skipped=%d",
        len(candidates),
        len(pinned),
        len(human),
    )
    return {
        "status": "completed",
        "candidates": len(candidates),
        "skipped_pinned": len(pinned),
        "skipped_human": len(human),
    }


def load_curator_last_pass(tenant_id: str | None = None) -> datetime | None:
    """Read the last curator-pass timestamp from the curator_state block."""
    from robothor.constants import DEFAULT_TENANT
    from robothor.memory.blocks import read_block

    tid = tenant_id or DEFAULT_TENANT
    try:
        block = read_block(_CURATOR_STATE_BLOCK, tenant_id=tid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("load_curator_last_pass read failed: %s", exc)
        return None
    if block.get("error"):
        return None
    raw = (block.get("content") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


def store_curator_last_pass(when: datetime, tenant_id: str | None = None) -> None:
    """Persist the curator-pass timestamp to the curator_state block."""
    from robothor.constants import DEFAULT_TENANT
    from robothor.memory.blocks import write_block

    tid = tenant_id or DEFAULT_TENANT
    try:
        write_block(_CURATOR_STATE_BLOCK, when.astimezone(UTC).isoformat(), tenant_id=tid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("store_curator_last_pass write failed: %s", exc)
