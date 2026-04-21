"""Buddy Critic — evidence-grounded agent review engine.

Buddy's reviewer-side brain. Every hour (when scheduled by docs/agents/buddy.yaml)
Buddy samples recent runs per agent, pulls real traces and tool errors, writes
one `agent_reviews` row per sampled run (`reviewer_type='buddy'`). Every six
hours it aggregates those reviews plus `goals.py` breaches into **findings** —
structured critiques paired with corrective-action templates. A finding becomes
a `crm_tasks` row tagged `nightwatch+self-improve+<agent>+<metric>`, picked up
by auto-agent, verified by `buddy_grader.py`.

The LLM is used only to *phrase* evidence. It never decides which agent is
breached, which metric matters, or what the baseline is — all of that comes
from structured queries. This eliminates the hallucination surface that made
the old reflection path generic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from robothor.constants import DEFAULT_TENANT
from robothor.engine.goals import (
    EXCLUDED_FROM_SELF_IMPROVE,
    GoalBreach,
    compute_goal_metrics,
    detect_goal_breach,
    parse_goals_from_manifest,
    suggest_corrective_actions,
)

logger = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "agents"
JOURNAL_DIR = Path(__file__).resolve().parent.parent.parent / "brain" / "journals" / "buddy"

# Fallback review model if buddy.yaml can't be loaded. Sonnet 4.6 because
# mimo-v2-pro returned empty bodies on live tests and grounded critique is
# the whole point. The *real* source of truth is the `model.primary` field
# in docs/agents/buddy.yaml (root CLAUDE.md rule 6) — `_get_review_model`
# reads that and falls back here only if parsing fails.
DEFAULT_REVIEW_MODEL = "openrouter/anthropic/claude-sonnet-4.6"
REVIEW_MAX_TOKENS = 400
REVIEW_TIMEOUT_S = 25

# (mtime, model_id) cache so we don't re-parse buddy.yaml on every review.
# Invalidated on manifest edit because mtime changes.
_review_model_cache: tuple[float, str] | None = None


def _get_review_model() -> str:
    """Return buddy's `model.primary` from the manifest.

    Cached by buddy.yaml's mtime — a manifest edit invalidates the cache on
    the next call. Returns DEFAULT_REVIEW_MODEL if the manifest is missing,
    unparseable, or declares no primary model.
    """
    global _review_model_cache
    manifest_path = AGENTS_DIR / "buddy.yaml"
    try:
        mtime = manifest_path.stat().st_mtime
    except OSError:
        return DEFAULT_REVIEW_MODEL

    if _review_model_cache is not None and _review_model_cache[0] == mtime:
        return _review_model_cache[1]

    try:
        from robothor.engine.config import load_agent_config

        config = load_agent_config("buddy", AGENTS_DIR)
    except Exception as e:
        logger.warning("Could not load buddy model from manifest: %s", e)
        config = None

    if config is not None and config.model_primary:
        _review_model_cache = (mtime, config.model_primary)
        return config.model_primary

    _review_model_cache = (mtime, DEFAULT_REVIEW_MODEL)
    return DEFAULT_REVIEW_MODEL


# Findings below this severity are not escalated to a task — they stay as
# per-run reviews for human browsing. Severity = breached_goal.priority_score
# (weight × consecutive_days_breached). A w=1.0 goal breached 3 days = 3.0.
FINDING_SEVERITY_THRESHOLD = 3.0


# ─── Dataclasses ──────────────────────────────────────────────────────


@dataclass
class Evidence:
    """Raw signal pulled from a single run. Feeds into LLM phrasing."""

    run_id: str
    agent_id: str
    status: str
    started_at: datetime | None
    duration_ms: int | None
    total_cost_usd: float | None
    output_text_truncated: str
    error_message: str | None
    error_steps: list[dict[str, Any]] = field(default_factory=list)
    tool_call_count: int = 0
    tool_error_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "duration_ms": self.duration_ms,
            "total_cost_usd": self.total_cost_usd,
            "output_text_truncated": self.output_text_truncated,
            "error_message": self.error_message,
            "error_steps": self.error_steps,
            "tool_call_count": self.tool_call_count,
            "tool_error_count": self.tool_error_count,
        }


@dataclass
class Review:
    """One Buddy review of one run. Written to agent_reviews."""

    agent_id: str
    run_id: str
    rating: int  # 1-5
    dimension: str  # reach | quality | efficiency | correctness
    specific_issue: str  # <=80 chars, refers to concrete evidence
    suggested_action: str  # <=120 chars, corrective-action category
    raw_evidence: Evidence


@dataclass
class Finding:
    """Aggregated critique — one per (agent, dimension, metric)."""

    agent_id: str
    dimension: str
    metric: str
    severity: float  # priority_score from GoalBreach
    consecutive_days_breached: int
    baseline_metric: float | None  # current metric value, for the grader
    target: str
    representative_run_ids: list[str]
    representative_feedback: list[str]
    corrective_actions: list[str]
    # Rolling window the breach was measured on. The grader uses this when it
    # re-computes the metric 48h later, so a 30-day-window goal isn't silently
    # verified on a 7-day slice. Defaults to 7 for older call sites.
    window_days: int = 7

    def task_title(self) -> str:
        return f"buddy/self-improve · {self.agent_id} · {self.metric} breached"

    def task_body(self) -> str:
        lines = [
            f"**Agent**: `{self.agent_id}`",
            f"**Dimension**: {self.dimension}",
            f"**Metric**: `{self.metric}` (target `{self.target}`)",
            f"**Consecutive days breached**: {self.consecutive_days_breached}",
            f"**Current value**: {self.baseline_metric if self.baseline_metric is not None else 'null'}",
            "",
            "## Recent Buddy reviews",
        ]
        for rid, fb in zip(self.representative_run_ids, self.representative_feedback, strict=False):
            lines.append(f"- run `{rid}`: {fb}")
        lines.append("")
        lines.append("## Corrective-action template")
        for step in self.corrective_actions:
            lines.append(f"- {step}")
        lines.append("")
        lines.append(
            "## Verification contract\n"
            "`buddy-grader` re-computes the metric 48h after this task moves to DONE. "
            f"If `{self.metric}` still doesn't satisfy `{self.target}`, the task is "
            "re-opened with an incremented escalation tag. At escalation:3 the task "
            "is marked `requires_human=true` and auto-escalation stops."
        )
        lines.append("")
        lines.append(
            f'<!-- buddy-baseline: {{"metric": "{self.metric}", '
            f'"target": "{self.target}", "baseline": {json.dumps(self.baseline_metric)}, '
            f'"window_days": {self.window_days}}} -->'
        )
        return "\n".join(lines)

    def task_tags(self) -> list[str]:
        return ["nightwatch", "self-improve", self.agent_id, self.metric]


# ─── Manifest loading (lightweight cache) ────────────────────────────


_manifests_cache: list[tuple[str, dict[str, Any]]] | None = None


def _load_manifests(refresh: bool = False) -> list[tuple[str, dict[str, Any]]]:
    global _manifests_cache
    if _manifests_cache is not None and not refresh:
        return _manifests_cache
    out: list[tuple[str, dict[str, Any]]] = []
    if not AGENTS_DIR.is_dir():
        _manifests_cache = []
        return _manifests_cache
    for path in sorted(AGENTS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            logger.warning("Skipping manifest %s: %s", path.name, e)
            continue
        agent_id = data.get("id")
        if agent_id:
            out.append((str(agent_id), data))
    _manifests_cache = out
    return _manifests_cache


# ─── Sampling ────────────────────────────────────────────────────────


def sample_runs_to_review(
    agent_id: str,
    n: int = 5,
    *,
    hours: int = 24,
    tenant_id: str = DEFAULT_TENANT,
) -> list[str]:
    """Pick up to `n` recent top-level runs for review.

    Biases toward: failed/timeout runs, runs with error steps, long-duration runs.
    Excludes sub-agent runs and already-reviewed runs.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.status, r.duration_ms, r.error_message,
                   (SELECT COUNT(*) FROM agent_run_steps s
                      WHERE s.run_id = r.id AND s.step_type = 'error') AS error_steps
            FROM agent_runs r
            WHERE r.agent_id = %s
              AND r.tenant_id = %s
              AND r.parent_run_id IS NULL
              AND r.status IN ('completed', 'failed', 'timeout')
              AND r.started_at > NOW() - (%s || ' hours')::interval
              AND NOT EXISTS (
                  SELECT 1 FROM agent_reviews ar
                  WHERE ar.run_id = r.id AND ar.reviewer_type = 'buddy'
              )
            ORDER BY
                CASE WHEN r.status IN ('failed', 'timeout') THEN 0 ELSE 1 END,
                (SELECT COUNT(*) FROM agent_run_steps s
                      WHERE s.run_id = r.id AND s.step_type = 'error') DESC,
                r.duration_ms DESC NULLS LAST
            LIMIT %s
            """,
            (agent_id, tenant_id, hours, n),
        )
        return [str(row[0]) for row in cur.fetchall()]


# ─── Evidence gathering ──────────────────────────────────────────────


MAX_OUTPUT_CHARS = 1500
MAX_ERROR_STEPS = 6


def build_evidence(run_id: str, *, tenant_id: str = DEFAULT_TENANT) -> Evidence | None:
    """Pull run metadata + error steps into a structured Evidence object."""
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, agent_id, status, started_at, duration_ms,
                   total_cost_usd, output_text, error_message
            FROM agent_runs
            WHERE id = %s AND tenant_id = %s
            """,
            (run_id, tenant_id),
        )
        row = cur.fetchone()
        if row is None:
            return None

        output = row[6] or ""
        truncated = output if len(output) <= MAX_OUTPUT_CHARS else output[:MAX_OUTPUT_CHARS] + "…"

        cur.execute(
            """
            SELECT step_number, step_type, tool_name, error_message, duration_ms
            FROM agent_run_steps
            WHERE run_id = %s AND (step_type = 'error' OR error_message IS NOT NULL)
            ORDER BY step_number
            LIMIT %s
            """,
            (run_id, MAX_ERROR_STEPS),
        )
        error_steps: list[dict[str, Any]] = []
        for srow in cur.fetchall():
            error_steps.append(
                {
                    "step_number": srow[0],
                    "step_type": srow[1],
                    "tool_name": srow[2],
                    "error_message": (srow[3] or "")[:200],
                    "duration_ms": srow[4],
                }
            )

        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE step_type = 'tool_call') AS tool_calls,
                COUNT(*) FILTER (
                    WHERE step_type = 'tool_call' AND error_message IS NOT NULL
                ) AS tool_errors
            FROM agent_run_steps WHERE run_id = %s
            """,
            (run_id,),
        )
        trow = cur.fetchone() or (0, 0)
        tool_calls = int(trow[0] or 0)
        tool_errors = int(trow[1] or 0)

    return Evidence(
        run_id=str(row[0]),
        agent_id=str(row[1]),
        status=str(row[2] or ""),
        started_at=row[3],
        duration_ms=int(row[4]) if row[4] is not None else None,
        total_cost_usd=float(row[5]) if row[5] is not None else None,
        output_text_truncated=truncated,
        error_message=row[7],
        error_steps=error_steps,
        tool_call_count=tool_calls,
        tool_error_count=tool_errors,
    )


# ─── LLM phrasing ────────────────────────────────────────────────────


_REVIEW_PROMPT = """You are Buddy — Robothor's fleet reviewer. Rate ONE agent run based on the structured evidence below.

Rules:
- Output JSON only. No prose outside the JSON.
- `rating` must be an integer 1-5 (1 = broken, 5 = excellent).
- `dimension` must be exactly one of: reach, quality, efficiency, correctness.
- `specific_issue` MUST quote or reference concrete evidence from the input. No generic filler.
- `specific_issue` max 80 chars. `suggested_action` max 120 chars.
- If the run looks fine, rate 4 or 5 and say what worked well.

Evidence:
{evidence_json}

Respond with JSON matching this shape:
{{"rating": <int>, "dimension": "<category>", "specific_issue": "<text>", "suggested_action": "<text>"}}"""


async def review_run(
    evidence: Evidence,
    *,
    model: str | None = None,
    timeout_s: int = REVIEW_TIMEOUT_S,
) -> Review | None:
    """Call the LLM to phrase a review of this run. Returns None on failure.

    ``model`` defaults to whatever ``buddy.yaml`` declares as
    ``model.primary`` (resolved lazily so manifest edits take effect without
    restarting the process). Pass an explicit value only when overriding for
    tests or ad-hoc scripts.
    """
    if model is None:
        model = _get_review_model()

    try:
        from robothor.engine.llm_client import llm_call
    except Exception as e:
        logger.warning("llm_client unavailable: %s", e)
        return None

    prompt = _REVIEW_PROMPT.format(evidence_json=json.dumps(evidence.to_dict(), default=str))

    try:
        response = await llm_call(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.2,
            max_tokens=REVIEW_MAX_TOKENS,
            timeout=timeout_s,
        )
        raw = str(response.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("Buddy review LLM call failed for run %s: %s", evidence.run_id, e)
        return None

    parsed = _extract_json(raw)
    if parsed is None:
        logger.warning(
            "Buddy review: could not parse LLM output for run %s: %s", evidence.run_id, raw[:200]
        )
        return None

    try:
        rating = max(1, min(5, int(parsed.get("rating", 3))))
        dimension = str(parsed.get("dimension", "correctness")).lower().strip()
        if dimension not in ("reach", "quality", "efficiency", "correctness"):
            dimension = "correctness"
        specific_issue = str(parsed.get("specific_issue", "")).strip()[:80]
        suggested_action = str(parsed.get("suggested_action", "")).strip()[:120]
    except (TypeError, ValueError) as e:
        logger.warning("Buddy review: malformed JSON for run %s: %s", evidence.run_id, e)
        return None

    if not specific_issue:
        return None  # refuse to persist content-free reviews

    return Review(
        agent_id=evidence.agent_id,
        run_id=evidence.run_id,
        rating=rating,
        dimension=dimension,
        specific_issue=specific_issue,
        suggested_action=suggested_action,
        raw_evidence=evidence,
    )


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from an LLM response."""
    raw = raw.strip()
    if not raw:
        return None
    # Trim markdown code fences if present
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 3:
            inner = parts[1]
            inner = inner.removeprefix("json\n")
            raw = inner.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Look for the first {...} span
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


# ─── Persistence ─────────────────────────────────────────────────────


def persist_review(review: Review, *, tenant_id: str = DEFAULT_TENANT) -> str | None:
    """Write the review to agent_reviews with reviewer_type='buddy'."""
    from robothor.crm.dal import create_review

    categories = {
        "dimension": review.dimension,
        "specific_issue": review.specific_issue,
        "tool_call_count": review.raw_evidence.tool_call_count,
        "tool_error_count": review.raw_evidence.tool_error_count,
        "error_step_count": len(review.raw_evidence.error_steps),
        "duration_ms": review.raw_evidence.duration_ms,
    }
    review_id = create_review(
        agent_id=review.agent_id,
        reviewer="buddy",
        reviewer_type="buddy",
        rating=review.rating,
        categories=categories,
        feedback=f"{review.specific_issue} → {review.suggested_action}",
        action_items=[review.suggested_action] if review.suggested_action else None,
        run_id=review.run_id,
        tenant_id=tenant_id,
    )
    _journal(
        "review",
        {
            "review_id": review_id,
            "agent_id": review.agent_id,
            "run_id": review.run_id,
            "rating": review.rating,
            "dimension": review.dimension,
            "specific_issue": review.specific_issue,
            "suggested_action": review.suggested_action,
        },
    )
    return review_id


# ─── Aggregation into findings ───────────────────────────────────────


def aggregate_findings(
    window_hours: int = 24,
    *,
    tenant_id: str = DEFAULT_TENANT,
) -> list[Finding]:
    """Group recent reviews with goal breaches into actionable findings.

    One Finding per (agent, breached_metric). Each carries evidence from the
    most severe Buddy reviews on the same agent so the task body has concrete
    examples — not just "score is low."
    """
    findings: list[Finding] = []
    for agent_id, manifest in _load_manifests():
        if agent_id in EXCLUDED_FROM_SELF_IMPROVE:
            continue
        goals = parse_goals_from_manifest(manifest)
        if not goals:
            continue
        breaches = detect_goal_breach(agent_id, goals, tenant_id=tenant_id)
        if not breaches:
            continue

        reviews_for_agent = _recent_buddy_reviews(
            agent_id, window_hours=window_hours, tenant_id=tenant_id
        )

        for breach in breaches:
            if breach.priority_score < FINDING_SEVERITY_THRESHOLD:
                continue
            # Pull up to 3 worst reviews whose dimension matches this breach's category
            relevant = [r for r in reviews_for_agent if r.get("dimension") == breach.category]
            relevant.sort(key=lambda r: int(r.get("rating", 5)))  # lowest rating first
            repr_runs: list[str] = []
            repr_feedback: list[str] = []
            for r in relevant[:3]:
                if r.get("run_id"):
                    repr_runs.append(str(r["run_id"]))
                repr_feedback.append(str(r.get("feedback") or "").strip()[:240])

            finding = _finding_from_breach(breach, agent_id, repr_runs, repr_feedback, tenant_id)
            # Skip findings where the metric can't be measured right now. A
            # null baseline means `compute_goal_metrics` couldn't populate
            # that metric — opening a task against it would be unfair noise
            # (the agent can't "fix" what can't be measured). When the metric
            # implementation lands, real values will flow and the finding
            # will be re-raised automatically.
            if finding.baseline_metric is None:
                continue
            findings.append(finding)
    return findings


def _finding_from_breach(
    breach: GoalBreach,
    agent_id: str,
    repr_runs: list[str],
    repr_feedback: list[str],
    tenant_id: str,
) -> Finding:
    # Grab the current live metric value for the grader's baseline. Sample on
    # the same window the breach was declared against so the baseline the
    # grader later re-checks is apples-to-apples.
    try:
        snapshot = compute_goal_metrics(
            agent_id, window_days=breach.window_days, tenant_id=tenant_id
        )
        baseline = snapshot.get(breach.metric)
        baseline_val = float(baseline) if baseline is not None else None
    except Exception:
        baseline_val = breach.actual

    return Finding(
        agent_id=agent_id,
        dimension=breach.category,
        metric=breach.metric,
        severity=breach.priority_score,
        consecutive_days_breached=breach.consecutive_days_breached,
        baseline_metric=baseline_val,
        target=breach.target,
        representative_run_ids=repr_runs,
        representative_feedback=repr_feedback,
        corrective_actions=suggest_corrective_actions(breach),
        window_days=breach.window_days,
    )


def _recent_buddy_reviews(agent_id: str, window_hours: int, tenant_id: str) -> list[dict[str, Any]]:
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, run_id, rating, feedback, categories, created_at
            FROM agent_reviews
            WHERE agent_id = %s
              AND tenant_id = %s
              AND reviewer_type = 'buddy'
              AND created_at > NOW() - (%s || ' hours')::interval
            ORDER BY created_at DESC
            """,
            (agent_id, tenant_id, window_hours),
        )
        out: list[dict[str, Any]] = []
        for row in cur.fetchall():
            cats = row[4] or {}
            if isinstance(cats, str):
                try:
                    cats = json.loads(cats)
                except json.JSONDecodeError:
                    cats = {}
            out.append(
                {
                    "id": str(row[0]),
                    "run_id": str(row[1]) if row[1] else None,
                    "rating": row[2],
                    "feedback": row[3],
                    "dimension": cats.get("dimension"),
                    "created_at": row[5],
                }
            )
        return out


# ─── Finding → CRM task ──────────────────────────────────────────────


def open_task_for_finding(finding: Finding, *, tenant_id: str = DEFAULT_TENANT) -> str | None:
    """Create a nightwatch+self-improve task for this finding.

    Dedup: if an open task already exists for the same (agent, metric), skip
    and return None. Prevents daily re-flagging of the same breach while the
    grader is still waiting for verification.
    """
    from robothor.crm.dal import create_task, list_tasks

    if finding.agent_id in EXCLUDED_FROM_SELF_IMPROVE:
        logger.info(
            "Refusing to open self-improve task on meta-agent %s (%s)",
            finding.agent_id,
            finding.metric,
        )
        return None

    existing = list_tasks(tags=["self-improve", finding.agent_id, finding.metric])
    if isinstance(existing, list):
        for row in existing:
            status = (row.get("status") or "").upper()
            if status in ("TODO", "IN_PROGRESS", "WAITING_REVIEW"):
                logger.info(
                    "Finding for %s/%s already open (task %s) — skipping dedup",
                    finding.agent_id,
                    finding.metric,
                    row.get("id"),
                )
                return None

    task_id = create_task(
        title=finding.task_title(),
        body=finding.task_body(),
        assigned_to_agent="auto-agent",
        tags=finding.task_tags(),
        priority="high" if finding.severity >= 6.0 else "normal",
        created_by_agent="buddy",
        tenant_id=tenant_id,
    )
    _journal(
        "finding_opened",
        {
            "task_id": task_id,
            "agent_id": finding.agent_id,
            "dimension": finding.dimension,
            "metric": finding.metric,
            "severity": finding.severity,
            "consecutive_days_breached": finding.consecutive_days_breached,
            "baseline_metric": finding.baseline_metric,
            "target": finding.target,
        },
    )
    return task_id


# ─── Journal writing ─────────────────────────────────────────────────


def _journal(event: str, payload: dict[str, Any]) -> None:
    """Append one JSONL line to brain/journals/buddy/YYYY-MM-DD.jsonl."""
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).date().isoformat()
        path = JOURNAL_DIR / f"{today}.jsonl"
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            **payload,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.debug("Journal write failed (%s): %s", event, e)


# ─── Top-level passes ────────────────────────────────────────────────


async def run_review_pass(
    *,
    runs_per_agent: int = 3,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Hourly pass: sample recent runs for each agent, review, persist.

    Returns a summary dict for logging / heartbeat.
    """
    reviewed = 0
    skipped = 0
    persist_failed = 0
    for agent_id, manifest in _load_manifests():
        if not parse_goals_from_manifest(manifest):
            continue  # agents without goals can't be fairly reviewed
        run_ids = sample_runs_to_review(agent_id, n=runs_per_agent, hours=24, tenant_id=tenant_id)
        for run_id in run_ids:
            evidence = build_evidence(run_id, tenant_id=tenant_id)
            if evidence is None:
                skipped += 1
                continue
            review = await review_run(evidence)
            if review is None:
                skipped += 1
                continue
            review_id = persist_review(review, tenant_id=tenant_id)
            if review_id is None:
                persist_failed += 1
            else:
                reviewed += 1
    return {
        "reviewed": reviewed,
        "skipped": skipped,
        "persist_failed": persist_failed,
        "ts": datetime.now(UTC).isoformat(),
    }


def run_aggregation_pass(
    *,
    window_hours: int = 24,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Six-hourly pass: turn recent reviews + breaches into CRM tasks."""
    findings = aggregate_findings(window_hours=window_hours, tenant_id=tenant_id)
    opened: list[str] = []
    skipped_dedup = 0
    for finding in findings:
        task_id = open_task_for_finding(finding, tenant_id=tenant_id)
        if task_id:
            opened.append(task_id)
        else:
            skipped_dedup += 1
    return {
        "findings": len(findings),
        "opened": len(opened),
        "skipped_dedup": skipped_dedup,
        "task_ids": opened,
        "ts": datetime.now(UTC).isoformat(),
    }
