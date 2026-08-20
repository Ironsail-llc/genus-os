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

Accretion gate (PR-3b): when ``ROBOTHOR_CURATOR_APPLY=1`` (apply mode) AND
``ROBOTHOR_ACCRETION_ENABLED`` is set, ``spawn_curator`` consults the two-key
gate (``robothor.engine.accretion.accretion_gate``) before granting the
destructive ``skill_archive`` tool. SCOPING DECISION: the gate is evaluated
against the ``curator`` agent itself (its own benchmark suite + goal-judge
history), not per-skill or per-affected-agent — there is no per-skill
benchmark/judge attribution in the engine yet, and building one is out of
scope for this wiring. This is a pragmatic v1 proxy ("is the curator's own
recent behavior safe/healthy") for "is it safe to let it apply destructive
skill changes"; revisit once per-skill scoring exists. If the gate blocks,
the pass is downgraded to the dry-run whitelist (proposals only) rather than
aborted — the downgrade is logged (warning) and persisted to the
``curator_state`` summary, so a blocked pass still produces an inspectable
proposal set with the blocking reason on record.

Fail-open/-closed asymmetry of key 1 (benchmark): "no suite configured" fails
OPEN by design — there is nothing to regress against and the brief forbids
inventing a benchmark run. But once a suite EXISTS, any inability to read its
signal (DAL unavailable, query failure, fewer than 2 recorded runs, missing
case counts) fails CLOSED: signal genuinely unknown must not read as "safe",
the same principle key 2 applies to missing judge history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from robothor.engine.accretion import accretion_enabled, accretion_gate

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
    to ``robothor.engine.skills.read_skill_view`` (meta.json static
    fields merged with state.json runtime telemetry).
    """
    from robothor.engine.skills import load_skills, read_skill_view

    if skills is None:
        skills = load_skills()
    if meta_loader is None:
        meta_loader = read_skill_view

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


def curator_dry_run() -> bool:
    """Whether the curator runs in dry-run (propose-only) mode.

    Default True (safe): the documented 2-week soak must be code-enforced, not
    prompt-trust. The operator sets ``ROBOTHOR_CURATOR_APPLY=1`` to allow real
    consolidation after reviewing proposals in ``crm_curator_state``.
    """
    import os

    return os.environ.get("ROBOTHOR_CURATOR_APPLY", "").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    )


def _curator_tool_whitelist(dry_run: bool = True) -> frozenset[str]:
    """Review-fork tools, plus skill_archive ONLY when not dry-run.

    In dry-run the destructive ``skill_archive`` tool is withheld entirely, so
    the curator physically cannot archive/merge — it can only propose. This
    makes the soak real rather than trusting the prompt.
    """
    from robothor.engine.background_review import REVIEW_TOOL_WHITELIST

    if dry_run:
        return REVIEW_TOOL_WHITELIST
    return REVIEW_TOOL_WHITELIST | frozenset({"skill_archive"})


def _curator_benchmark_regression(
    agent_id: str = "curator", tenant_id: str | None = None
) -> tuple[bool, str]:
    """Key 1 of the accretion gate: has the curator's own suite regressed?

    Looks for ``docs/benchmarks/<agent_id>/suite.yaml``. Fail-open/-closed
    asymmetry (deliberate):

    - **No suite configured** — fail OPEN (``(False, note)``): there is nothing
      to regress against and the brief forbids inventing a benchmark run.
      This is the state today (the curator has no ``suite.yaml``).
    - **Suite exists but the signal is unknown** (DAL import failure, DB query
      error, or fewer than 2 recorded runs) — fail CLOSED (``(True, note)``):
      the operator configured a suite expecting it to gate, so "signal
      genuinely unknown" must not read as "safe" — the same principle the
      judge key applies to missing history.

    When both rows are available, compares the two most recent
    ``benchmark_results`` rows (same passed/total_cases access pattern as
    ``goals._get_benchmark_pass_rate``).
    """
    import os
    from pathlib import Path

    from robothor.constants import DEFAULT_TENANT

    tid = tenant_id or DEFAULT_TENANT
    workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
    suite_path = workspace / "docs" / "benchmarks" / agent_id / "suite.yaml"
    if not suite_path.exists():
        return False, f"no benchmark suite for '{agent_id}'"

    try:
        from robothor.crm.dal import get_connection
    except Exception:
        return True, "benchmark DAL unavailable with suite configured (fail closed)"

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT passed, total_cases
                FROM benchmark_results
                WHERE agent_id = %s AND tenant_id = %s
                ORDER BY run_at DESC
                LIMIT 2
                """,
                (agent_id, tid),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("curator gate: benchmark_results lookup failed: %s", exc)
        return True, "benchmark lookup failed with suite configured (fail closed)"

    if len(rows) < 2:
        return True, "fewer than 2 benchmark runs recorded with suite configured (fail closed)"

    latest_passed, latest_total = rows[0]
    prev_passed, prev_total = rows[1]
    if not latest_total or not prev_total:
        return True, "benchmark run missing case counts with suite configured (fail closed)"

    latest_rate = float(latest_passed) / float(latest_total)
    prev_rate = float(prev_passed) / float(prev_total)
    if latest_rate < prev_rate:
        return True, f"benchmark pass rate regressed {prev_rate:.2f} -> {latest_rate:.2f}"
    return False, f"benchmark pass rate stable/improved {prev_rate:.2f} -> {latest_rate:.2f}"


def _curator_judge_scores(
    agent_id: str = "curator", tenant_id: str | None = None
) -> tuple[float | None, float | None]:
    """Key 2 inputs: (current judge score, pre-change baseline judge score).

    Both come from ``goals._get_goal_achievement_judgment`` (the goal-judge's
    confidence-weighted 0-1 score), evaluated over two adjacent 7-day windows:
    the trailing week (current) and the week before that (baseline). Either
    value is ``None`` when there is no judge history for that window — the
    gate treats that as "cannot confirm", not as a passing score.
    """
    from robothor.constants import DEFAULT_TENANT
    from robothor.engine.goals import _get_goal_achievement_judgment

    tid = tenant_id or DEFAULT_TENANT
    now = datetime.now(UTC)
    current = _get_goal_achievement_judgment(agent_id, tenant_id=tid, as_of=now)
    baseline = _get_goal_achievement_judgment(
        agent_id, tenant_id=tid, as_of=now - timedelta(days=7)
    )
    return current, baseline


def evaluate_accretion_gate(
    agent_id: str = "curator", tenant_id: str | None = None
) -> tuple[bool, str]:
    """Compute the two-key accretion-gate verdict for the curator's apply pass.

    See the module docstring for the scoping decision (gates on the curator
    agent itself, not per-skill). Fails closed: if there is no judge history
    yet to compare against a baseline, the gate blocks rather than assuming
    the change is safe. The judge check runs first and short-circuits — no
    benchmark DB round-trip is made when the verdict is already blocked.
    """
    judge_score, baseline_score = _curator_judge_scores(agent_id, tenant_id)
    if judge_score is None or baseline_score is None:
        return (
            False,
            f"blocked: insufficient judge history for '{agent_id}' to establish a baseline",
        )
    has_regression, regression_note = _curator_benchmark_regression(agent_id, tenant_id)
    ok, reason = accretion_gate(
        has_safety_regression=has_regression,
        judge_score=judge_score,
        baseline_score=baseline_score,
    )
    return ok, f"{reason} ({regression_note})"


async def spawn_curator(
    scheduler: Any,
    *,
    curator_agent_id: str = "curator",
    tenant_id: str | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any] | None:
    """Run one LLM consolidation pass as a top-level ``curator`` agent run.

    Drives a real top-level run via ``scheduler._run_agent`` (which installs a
    fresh spawn context) rather than ``spawn_background_review`` (which needs an
    active spawn context and would fail from a daemon tick). The CURATOR write
    origin tags its skill writes agent-created; the tool whitelist hard-limits it
    to memory+skill tools plus skill_archive. Never raises — a failed pass must
    not take down the daemon loop.

    Accretion gate: when the caller requests apply mode (not dry-run) AND
    ``accretion_enabled()``, the two-key gate is evaluated before the
    destructive whitelist is granted. A blocked gate downgrades this pass to
    the dry-run whitelist (skill_archive withheld) rather than aborting it —
    the LLM still runs and proposes, it just cannot apply. When the flag is
    off, the apply whitelist is still granted (behavior unchanged from before
    this wiring existed) but a warning is logged and the pass summary records
    ``gate_verdict="not consulted"`` — an apply pass with no safety check
    must never look identical to a passed gate.
    """
    from robothor.engine.skill_provenance import (
        CURATOR,
        reset_current_write_origin,
        set_current_write_origin,
    )
    from robothor.engine.tools.dispatch import clear_tool_whitelist, set_tool_whitelist

    requested_dry_run = curator_dry_run() if dry_run is None else dry_run

    candidates, pinned, human = list_curator_candidates()
    if not candidates:
        logger.info("spawn_curator: no agent-created candidates; skipping LLM pass")
        return {"status": "skipped", "reason": "no_candidates"}

    effective_dry_run = requested_dry_run
    gate_verdict: bool | str | None = None
    gate_reason: str | None = None
    if not requested_dry_run:
        if accretion_enabled():
            gate_verdict, gate_reason = evaluate_accretion_gate(curator_agent_id, tenant_id)
            if gate_verdict:
                logger.info("curator accretion gate passed: %s", gate_reason)
            else:
                logger.warning(
                    "curator accretion gate blocked apply pass, downgrading to dry-run: %s",
                    gate_reason,
                )
                effective_dry_run = True
        else:
            logger.warning(
                "curator: apply pass running WITHOUT the accretion gate "
                "(ROBOTHOR_ACCRETION_ENABLED unset) — no safety check before "
                "granting the destructive whitelist"
            )
            gate_verdict = "not consulted"
            gate_reason = "accretion gate disabled (ROBOTHOR_ACCRETION_ENABLED unset)"

    if effective_dry_run:
        logger.info("curator: DRY-RUN — skill_archive withheld, proposals only")
    origin_token = set_current_write_origin(CURATOR)
    whitelist_token = set_tool_whitelist(_curator_tool_whitelist(dry_run=effective_dry_run))
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
    # NOTE: scheduler._run_agent does not hand back the agent's own final
    # reply here, so CuratorResult's proposed_archive/merge/demote fields
    # cannot be populated from this call site (would require output-parsing
    # infra, out of scope for PR-3b). Persist what IS observable instead.
    store_curator_pass_summary(
        {
            "mode": "dry_run" if effective_dry_run else "apply",
            "requested_mode": "dry_run" if requested_dry_run else "apply",
            "gate_verdict": gate_verdict,
            "gate_reason": gate_reason,
            "candidates_inspected": len(candidates),
            "skipped_pinned": list(pinned),
            "skipped_human_authored": list(human),
        },
        tenant_id=tenant_id,
    )
    return {
        "status": "completed",
        "dry_run": effective_dry_run,
        "requested_dry_run": requested_dry_run,
        "gate_verdict": gate_verdict,
        "gate_reason": gate_reason,
        "candidates": len(candidates),
        "skipped_pinned": len(pinned),
        "skipped_human": len(human),
    }


def _load_curator_state_payload(tenant_id: str | None = None) -> dict[str, Any]:
    """Read + parse the curator_state block as JSON.

    Tolerates the pre-PR-3b legacy format (a bare ISO-8601 timestamp string,
    no JSON) by lifting it into ``{"last_pass_at": <raw>}`` so existing blocks
    written before this change still load correctly.
    """
    import json

    from robothor.constants import DEFAULT_TENANT
    from robothor.memory.blocks import read_block

    tid = tenant_id or DEFAULT_TENANT
    try:
        block = read_block(_CURATOR_STATE_BLOCK, tenant_id=tid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("curator_state read failed: %s", exc)
        return {}
    if block.get("error"):
        return {}
    raw = (block.get("content") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except (json.JSONDecodeError, TypeError):
        pass
    return {"last_pass_at": raw}  # legacy bare-ISO format


def _write_curator_state_payload(payload: dict[str, Any], tenant_id: str | None = None) -> None:
    import json

    from robothor.constants import DEFAULT_TENANT
    from robothor.memory.blocks import write_block

    tid = tenant_id or DEFAULT_TENANT
    try:
        write_block(_CURATOR_STATE_BLOCK, json.dumps(payload), tenant_id=tid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("curator_state write failed: %s", exc)


def load_curator_last_pass(tenant_id: str | None = None) -> datetime | None:
    """Read the last curator-pass timestamp from the curator_state block."""
    raw = _load_curator_state_payload(tenant_id).get("last_pass_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


def store_curator_last_pass(when: datetime, tenant_id: str | None = None) -> None:
    """Persist the curator-pass timestamp to the curator_state block.

    Preserves any previously-stored pass summary (see
    ``store_curator_pass_summary``) rather than clobbering it — the two are
    written to the same block but are logically separate fields.
    """
    payload = _load_curator_state_payload(tenant_id)
    payload["last_pass_at"] = when.astimezone(UTC).isoformat()
    _write_curator_state_payload(payload, tenant_id)


_MAX_CURATOR_SUMMARY_LIST_LEN = 10


def store_curator_pass_summary(summary: dict[str, Any], tenant_id: str | None = None) -> None:
    """Persist a structured curator-pass summary alongside the last-pass timestamp.

    Keeps the ``curator_state`` block small: list-shaped fields (e.g.
    ``skipped_pinned``) are capped at ``_MAX_CURATOR_SUMMARY_LIST_LEN`` entries
    before being written. Lets an operator inspect a dry-run soak (mode, gate
    verdict/reason, candidate counts) without needing to parse the LLM's own
    reply, which is not available at this call site.
    """
    payload = _load_curator_state_payload(tenant_id)
    capped: dict[str, Any] = {}
    for key, value in summary.items():
        if isinstance(value, list):
            capped[key] = value[:_MAX_CURATOR_SUMMARY_LIST_LEN]
        else:
            capped[key] = value
    payload["last_summary"] = capped
    _write_curator_state_payload(payload, tenant_id)


def load_curator_last_summary(tenant_id: str | None = None) -> dict[str, Any] | None:
    """Read the last persisted curator-pass summary, if any."""
    summary = _load_curator_state_payload(tenant_id).get("last_summary")
    return summary if isinstance(summary, dict) else None
