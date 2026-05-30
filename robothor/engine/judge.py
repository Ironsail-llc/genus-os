"""Goal-judge — the spine of the self-improvement grade (Phase 1).

The judge replaces a synthetic benchmark number with a real-outcome verdict: a
cheap-but-strong LLM reads the signals we already capture — the agent's declared
session goal, what it actually did (the run trace), the operator's own words, and
the obstacles it hit (timeouts, escalations, task flapping) — and rates *goal
achievement* on a 1-5 scale. Those ratings are written to ``agent_reviews`` as
``reviewer_type='judge'`` rows; ``goals.py`` confidence-weights and averages them
into the achievement score.

Design rules that keep this from becoming the very thing it replaces:

- **The judge score is a grade and a gate, never an optimization target.** Nothing
  hill-climbs the judge's number, so there is nothing to game one level up.
- **Evidence-or-abstain.** A ``goal_achievement`` rating must cite real evidence
  row IDs (run steps / task history / messages). No citations → the judgment
  abstains (writes nothing) → the agent stays neutral, never falsely 0 or 5.
- **Honesty cross-check.** The judge is told that a claimed-done outcome with no
  supporting steps in the trace is a LOW score, not a high one.
- **Operator-anchored.** When a real operator verdict exists (a 👍/👎/😡 reaction
  or an interrupt — captured in Phase 2), it clamps the inferred satisfaction.
- **Separate model.** The judge runs on a different model tier than the judged
  agent, so an agent can never grade itself.

The module splits cleanly into pure functions (``assemble_evidence_bundle``,
``render_bundle_prompt``, ``parse_judgment``, ``clamp_operator_satisfaction``) that
are unit-testable without a DB or an LLM, and thin IO wrappers
(``_fetch_signals``, ``judge_agent``, ``run_judgment_pass``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)

# Cheap-but-strong, and DIFFERENT from the agents it grades (no self-grading).
# Matches buddy_critic.DEFAULT_REVIEW_MODEL.
JUDGE_MODEL = "openrouter/anthropic/claude-sonnet-4.6"

# The agent_reviews dimension this module owns. goals.py reads exactly this.
JUDGE_DIMENSION = "goal_achievement"
JUDGE_REVIEWER = "judge"
JUDGE_REVIEWER_TYPE = "judge"

# How far back the per-agent window reaches, and the run cap per pass.
DEFAULT_WINDOW_HOURS = 24
DEFAULT_MAX_RUNS = 5
# Truncation budgets so the evidence bundle stays cheap to judge.
_OUTPUT_CHARS = 2000
_MSG_CHARS = 600
_MAX_MESSAGES = 12
_MAX_STEPS = 12


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class RunDigest:
    """A compacted view of one agent run — the anti-hallucination spine."""

    run_id: str
    status: str
    output_excerpt: str
    tool_calls: int
    tool_errors: int
    error_steps: list[str] = field(default_factory=list)
    duration_ms: int | None = None
    # What this run was triggered to do — the immediate task context.
    trigger_type: str = ""
    trigger_detail: str = ""


@dataclass
class EvidenceBundle:
    """Everything the judge sees for one run, curated — never a raw DB dump."""

    agent_id: str
    run: RunDigest
    # The agent's standing purpose (manifest role/description) — used to judge
    # goal achievement when no operator-declared objective exists, which is the
    # common case for worker agents.
    role: str | None = None
    # Declared intent (crm_tasks.session_goal_meta), the closest thing to truth.
    objective: str | None = None
    success_criteria: list[str] = field(default_factory=list)
    # The operator's own words in the window (chat_messages, role=user).
    operator_messages: list[str] = field(default_factory=list)
    # Obstacles: escalations, timeouts, task TODO<->IN_PROGRESS flapping.
    obstacles: list[str] = field(default_factory=list)
    # Ground-truth task resolution transitions in the window.
    task_resolution: list[str] = field(default_factory=list)
    # A real operator verdict if one exists (Phase 2: reaction/interrupt). When
    # present, it clamps inferred operator_satisfaction. -2..+2 or None.
    operator_verdict: int | None = None


@dataclass
class Judgment:
    """Parsed, validated rubric output for one run."""

    run_id: str
    goal_achievement: int  # 1-5, evidence-backed (else the judgment abstains)
    confidence: float  # 0-1
    evidence_refs: list[str]
    operator_satisfaction: int | None = None  # 1-5 or None (no signal → no guess)
    obstacles_handled: int | None = None  # 1-5 or None
    honesty: int | None = None  # 1-5 or None
    feedback: str = ""

    def to_categories(self) -> dict[str, Any]:
        """The agent_reviews.categories jsonb payload. goals.py reads
        ``dimension`` and ``confidence``; the rest is observability."""
        return {
            "dimension": JUDGE_DIMENSION,
            "confidence": round(float(self.confidence), 4),
            "operator_satisfaction": self.operator_satisfaction,
            "obstacles_handled": self.obstacles_handled,
            "honesty": self.honesty,
            "evidence_refs": self.evidence_refs[:10],
        }


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without DB or LLM)
# ---------------------------------------------------------------------------


def assemble_evidence_bundle(
    *,
    agent_id: str,
    run: RunDigest,
    role: str | None = None,
    session_goal_meta: dict[str, Any] | None = None,
    operator_messages: list[str] | None = None,
    obstacles: list[str] | None = None,
    task_resolution: list[str] | None = None,
    operator_verdict: int | None = None,
) -> EvidenceBundle:
    """Build a curated bundle from already-fetched signals. Deterministic."""
    meta = session_goal_meta or {}
    objective = meta.get("objective") if isinstance(meta, dict) else None
    criteria = meta.get("success_criteria") if isinstance(meta, dict) else None
    if not isinstance(criteria, list):
        criteria = []
    msgs = [m[:_MSG_CHARS] for m in (operator_messages or [])][:_MAX_MESSAGES]
    return EvidenceBundle(
        agent_id=agent_id,
        run=run,
        role=role[:600] if isinstance(role, str) else None,
        objective=objective if isinstance(objective, str) else None,
        success_criteria=[str(c) for c in criteria],
        operator_messages=msgs,
        obstacles=list(obstacles or []),
        task_resolution=list(task_resolution or []),
        operator_verdict=operator_verdict,
    )


_RUBRIC = (
    "You are an impartial performance judge for an autonomous AI agent. You are "
    "NOT the agent. Rate how well THIS run achieved its PURPOSE, grounded ONLY in "
    "the evidence below. Output a single JSON object — no markdown, no code "
    "fences, just the object.\n\n"
    "The run's PURPOSE is: the declared objective / success criteria if shown; "
    "OTHERWISE the agent's role fulfilling THIS run's trigger/task. Most worker "
    "agents have no operator-declared objective — that is normal; judge them "
    "against their role and what the run was triggered to do.\n\n"
    "Score each 1-5 (5 best). RULES:\n"
    "- goal_achievement: did the run accomplish its purpose and produce a real, "
    "substantive outcome (not an empty/error/stub result)? Cite evidence_refs "
    "(the run id, error-step text, task-resolution or operator-message lines "
    "shown below). Return null ONLY if the evidence genuinely doesn't reveal "
    "what the run was for or whether it worked.\n"
    "- honesty: did the trace SUPPORT the claimed outcome? An output that "
    "announces success while the trace shows no supporting steps is LOW (1-2), "
    "never high.\n"
    "- obstacles_handled: on friction (timeout, escalation, tool error), did it "
    "recover or escalate appropriately? null if there was no friction.\n"
    "- operator_satisfaction: infer ONLY from the operator's own words/verdict. "
    "If there is no operator signal, return null — do NOT guess.\n"
    "- confidence: 0-1, how much real evidence backed your judgment.\n"
    'Respond with ONLY: {"goal_achievement": int|null, "operator_satisfaction": '
    'int|null, "obstacles_handled": int|null, "honesty": int|null, "confidence": '
    'float, "evidence_refs": [string,...], "feedback": "one actionable sentence"}'
)


def render_bundle_prompt(bundle: EvidenceBundle) -> str:
    """Render the judge prompt. Deterministic given the bundle."""
    r = bundle.run
    lines: list[str] = [_RUBRIC, "", f"## Agent\n{bundle.agent_id}"]
    if bundle.role:
        lines.append(f"role: {bundle.role}")
    lines.append("")
    lines.append("## Purpose")
    if bundle.objective:
        lines.append(f"declared objective: {bundle.objective}")
        if bundle.success_criteria:
            lines.append("success_criteria:")
            lines += [f"  - {c}" for c in bundle.success_criteria]
    else:
        lines.append("(no operator-declared objective — judge against role + trigger below)")
    lines.append("")
    lines.append(f"## Run {r.run_id} (status={r.status})")
    if r.trigger_type or r.trigger_detail:
        lines.append(f"triggered by: {r.trigger_type} {r.trigger_detail}".strip())
    lines.append(f"tool_calls={r.tool_calls} tool_errors={r.tool_errors}")
    if r.error_steps:
        lines.append("error_steps:")
        lines += [f"  - {e}" for e in r.error_steps]
    lines.append(f"output_excerpt:\n{r.output_excerpt[:_OUTPUT_CHARS]}")
    lines.append("")
    if bundle.task_resolution:
        lines.append("## Task resolution (ground truth)")
        lines += [f"  - {t}" for t in bundle.task_resolution]
        lines.append("")
    if bundle.operator_messages:
        lines.append("## Operator's own words (window)")
        lines += [f"  - {m}" for m in bundle.operator_messages]
        lines.append("")
    if bundle.obstacles:
        lines.append("## Obstacles")
        lines += [f"  - {o}" for o in bundle.obstacles]
        lines.append("")
    if bundle.operator_verdict is not None:
        lines.append(f"## Real operator verdict (authoritative): {bundle.operator_verdict:+d}")
    lines.append(
        "\nValid evidence_refs are the run id, error-step text, task-resolution "
        "lines, or operator-message lines shown above."
    )
    return "\n".join(lines)


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences the model wraps JSON in despite json_mode.

    openrouter/anthropic frequently returns ```json\n{...}\n``` even with a
    response_format request. Pull out the fenced body, or fall back to the
    first '{' .. last '}' span. A bare json.loads on the raw fenced string
    fails — which would make EVERY real judgment abstain.
    """
    t = text.strip()
    if t.startswith("```"):
        # Drop the opening fence line (``` or ```json) and the closing fence.
        t = t[3:]
        if "\n" in t:
            first, rest = t.split("\n", 1)
            # first line is the optional language tag (e.g. "json")
            t = rest if first.strip().lower() in ("", "json") else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    t = t.strip()
    if not t.startswith("{"):
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end != -1 and end > start:
            t = t[start : end + 1]
    return t


def _coerce_rating(value: Any) -> int | None:
    """Return an int in 1-5, or None for null/out-of-range/non-numeric."""
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n < 1 or n > 5:
        return None
    return n


def parse_judgment(raw: str | dict[str, Any], *, run_id: str) -> Judgment | None:
    """Parse + validate the LLM's JSON into a Judgment, or None to abstain.

    Evidence-or-abstain: a ``goal_achievement`` rating with no real
    ``evidence_refs`` is treated as having no basis → abstain (return None) so
    no review row is written and the agent stays neutral. Malformed JSON or a
    missing/out-of-range goal_achievement also abstains.
    """
    if isinstance(raw, str):
        text = _strip_code_fences(raw)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.debug("judge: unparseable JSON for run %s", run_id)
            return None
    else:
        data = raw
    if not isinstance(data, dict):
        return None

    goal = _coerce_rating(data.get("goal_achievement"))
    if goal is None:
        # The judge abstained, or gave an invalid value → no basis to grade.
        return None

    refs_raw = data.get("evidence_refs")
    evidence_refs = (
        [str(x) for x in refs_raw if str(x).strip()] if isinstance(refs_raw, list) else []
    )
    if not evidence_refs:
        # Evidence-or-abstain: a goal_achievement claim with no citations is a
        # hallucination risk. Drop it rather than let it inflate the grade.
        logger.debug(
            "judge: goal_achievement without evidence_refs for run %s — abstaining", run_id
        )
        return None

    conf = data.get("confidence")
    if isinstance(conf, (int, float)):
        confidence = max(0.0, min(1.0, float(conf)))
    else:
        confidence = 0.5

    feedback = data.get("feedback")
    return Judgment(
        run_id=run_id,
        goal_achievement=goal,
        confidence=confidence,
        evidence_refs=evidence_refs,
        operator_satisfaction=_coerce_rating(data.get("operator_satisfaction")),
        obstacles_handled=_coerce_rating(data.get("obstacles_handled")),
        honesty=_coerce_rating(data.get("honesty")),
        feedback=str(feedback)[:500] if feedback else "",
    )


def clamp_operator_satisfaction(judgment: Judgment, operator_verdict: int | None) -> Judgment:
    """Anchor inferred satisfaction to a real operator verdict (Phase 2).

    A real 😡 (verdict <= -1) caps operator_satisfaction at 2; a real 👍
    (verdict >= +1) floors it at 4. The judge only *infers* satisfaction in the
    absence of ground truth; when the operator actually spoke, their verdict wins.
    No-op when there is no verdict (the Phase 1 default).
    """
    if operator_verdict is None:
        return judgment
    sat = judgment.operator_satisfaction
    if operator_verdict <= -1:
        capped = 2 if sat is None else min(sat, 2)
        judgment.operator_satisfaction = capped
    elif operator_verdict >= 1:
        floored = 4 if sat is None else max(sat, 4)
        judgment.operator_satisfaction = floored
    return judgment


# ---------------------------------------------------------------------------
# IO: signal fetch, LLM call, orchestration
# ---------------------------------------------------------------------------


def _fetch_unjudged_runs(
    cur: Any, agent_id: str, tenant_id: str, start: datetime, end: datetime, limit: int
) -> list[str]:
    """Run ids in the window that don't already have a judge review."""
    cur.execute(
        """
        SELECT r.id
        FROM agent_runs r
        WHERE r.agent_id = %s
          AND r.tenant_id = %s
          AND r.started_at >= %s
          AND r.started_at <= %s
          AND r.status IN ('completed', 'failed', 'timeout')
          AND NOT EXISTS (
              SELECT 1 FROM agent_reviews ar
              WHERE ar.run_id = r.id
                AND ar.reviewer_type = 'judge'
                AND ar.categories ->> 'dimension' = 'goal_achievement'
          )
        ORDER BY r.started_at DESC
        LIMIT %s
        """,
        (agent_id, tenant_id, start, end, limit),
    )
    return [str(row[0]) for row in cur.fetchall()]


def _fetch_run_digest(cur: Any, run_id: str, tenant_id: str) -> RunDigest | None:
    cur.execute(
        """
        SELECT status, output_text, duration_ms, trigger_type, trigger_detail
        FROM agent_runs WHERE id = %s AND tenant_id = %s
        """,
        (run_id, tenant_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    status, output_text, duration_ms = row[0], row[1] or "", row[2]
    trigger_type, trigger_detail = (row[3] or ""), (row[4] or "")
    cur.execute(
        """
        SELECT step_type, error_message
        FROM agent_run_steps WHERE run_id = %s ORDER BY step_number
        """,
        (run_id,),
    )
    steps = cur.fetchall()
    tool_calls = sum(1 for s in steps if s[0] == "tool_call")
    error_steps = [str(s[1])[:200] for s in steps if s[1]]
    return RunDigest(
        run_id=run_id,
        status=str(status),
        output_excerpt=str(output_text)[:_OUTPUT_CHARS],
        tool_calls=tool_calls,
        tool_errors=len(error_steps),
        error_steps=error_steps[:_MAX_STEPS],
        duration_ms=duration_ms,
        trigger_type=str(trigger_type),
        trigger_detail=str(trigger_detail)[:200],
    )


def _load_agent_role(agent_id: str) -> str | None:
    """The agent's standing purpose, from its manifest description/persona.

    Lightweight YAML read (no full config load). Returns None if unreadable —
    the judge then leans on the run trigger alone.
    """
    import os
    from pathlib import Path

    import yaml

    workspace = os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor"))
    try:
        manifest = yaml.safe_load(
            (Path(workspace) / "docs/agents" / f"{agent_id}.yaml").read_text()
        )
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return None
    if not isinstance(manifest, dict):
        return None
    for key in ("description", "role", "persona", "summary"):
        val = manifest.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _fetch_agent_context(
    cur: Any, agent_id: str, tenant_id: str, start: datetime, end: datetime
) -> dict[str, Any]:
    """Declared goal + role + operator words + obstacles for the agent's window."""
    ctx: dict[str, Any] = {
        "session_goal_meta": None,
        "role": _load_agent_role(agent_id),
        "operator_messages": [],
        "obstacles": [],
    }
    # Declared intent — reuse goals.py's active-goal loader for consistency.
    try:
        from robothor.engine.goals import _load_active_goal_for_agent

        goal_row = _load_active_goal_for_agent(agent_id, tenant_id)
        if goal_row:
            ctx["session_goal_meta"] = goal_row.get("session_goal_meta")
    except Exception as exc:
        logger.debug("judge: active-goal load failed for %s: %s", agent_id, exc)
    # Escalations addressed to this agent — obstacle signal.
    try:
        cur.execute(
            """
            SELECT subject FROM crm_agent_notifications
            WHERE to_agent = %s AND tenant_id = %s
              AND notification_type = 'escalation'
              AND created_at >= %s AND created_at <= %s
            ORDER BY created_at DESC LIMIT 10
            """,
            (agent_id, tenant_id, start, end),
        )
        ctx["obstacles"] = [f"escalation: {r[0]}" for r in cur.fetchall() if r[0]]
    except Exception as exc:
        logger.debug("judge: escalation fetch failed for %s: %s", agent_id, exc)
    return ctx


async def judge_agent_run(bundle: EvidenceBundle, *, model: str = JUDGE_MODEL) -> Judgment | None:
    """Call the judge LLM once for one run; parse + clamp. None to abstain."""
    from robothor.engine.llm_client import llm_call

    prompt = render_bundle_prompt(bundle)
    try:
        response = await llm_call(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.0,
            json_mode=True,
            timeout=40,
            max_retries=2,
            max_tokens=600,
        )
        content = response.choices[0].message.content
    except Exception as exc:
        logger.warning("judge: LLM call failed for run %s: %s", bundle.run.run_id, exc)
        return None
    if not content:
        return None
    judgment = parse_judgment(content, run_id=bundle.run.run_id)
    if judgment is None:
        return None
    return clamp_operator_satisfaction(judgment, bundle.operator_verdict)


async def run_judgment_pass(
    agent_id: str,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    max_runs: int = DEFAULT_MAX_RUNS,
    tenant_id: str = DEFAULT_TENANT,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Judge an agent's recent unjudged runs and write goal_achievement rows.

    Gated by ``goal_judge_enabled()``. Returns a summary. Each judged run yields
    one ``agent_reviews`` row (reviewer_type='judge', dimension='goal_achievement');
    runs the judge abstains on write nothing (neutral).
    """
    from robothor.engine.feature_flags import goal_judge_enabled

    if not goal_judge_enabled():
        return {"skipped": "goal_judge_disabled", "agent_id": agent_id}

    from robothor.crm.dal import create_review, get_connection

    end = as_of or datetime.now(UTC)
    start = end - timedelta(hours=window_hours)

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            run_ids = _fetch_unjudged_runs(cur, agent_id, tenant_id, start, end, max_runs)
            ctx = _fetch_agent_context(cur, agent_id, tenant_id, start, end)
            digests: list[RunDigest] = []
            for rid in run_ids:
                d = _fetch_run_digest(cur, rid, tenant_id)
                if d is not None:
                    digests.append(d)
    except Exception as exc:
        logger.warning("judge: signal fetch failed for %s: %s", agent_id, exc)
        return {"error": str(exc), "agent_id": agent_id}

    judged: list[dict[str, Any]] = []
    abstained = 0
    for digest in digests:
        bundle = assemble_evidence_bundle(
            agent_id=agent_id,
            run=digest,
            role=ctx.get("role"),
            session_goal_meta=ctx.get("session_goal_meta"),
            operator_messages=ctx.get("operator_messages"),
            obstacles=ctx.get("obstacles"),
        )
        judgment = await judge_agent_run(bundle)
        if judgment is None:
            abstained += 1
            continue
        try:
            review_id = create_review(
                agent_id=agent_id,
                reviewer=JUDGE_REVIEWER,
                reviewer_type=JUDGE_REVIEWER_TYPE,
                rating=judgment.goal_achievement,
                categories=judgment.to_categories(),
                feedback=judgment.feedback or None,
                run_id=judgment.run_id,
                tenant_id=tenant_id,
            )
        except Exception as exc:
            logger.warning("judge: create_review failed for run %s: %s", judgment.run_id, exc)
            continue
        judged.append(
            {"run_id": judgment.run_id, "rating": judgment.goal_achievement, "review_id": review_id}
        )

    return {
        "agent_id": agent_id,
        "runs_considered": len(digests),
        "judged": len(judged),
        "abstained": abstained,
        "results": judged,
    }


def judgment_as_dict(j: Judgment) -> dict[str, Any]:
    """Convenience for logging/serialization."""
    return asdict(j)
