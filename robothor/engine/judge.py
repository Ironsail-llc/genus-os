"""Goal-judge spine (Wave-2, W2-22 — accretion foundation).

Grades an agent run on ``goal_achievement`` (1-5) over a DETERMINISTIC evidence
bundle. The judge is a GRADE and a GATE — never a quantity an optimizer
hill-climbs (the anti-reward-hacking invariant). It never grades a run produced
by the judge agent itself (no self-grading). Verdicts are written to
``agent_reviews(reviewer_type='judge')``.

Model: claude-sonnet-4-6, temperature 0, JSON output. Flag: ROBOTHOR_JUDGE_ENABLED.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

JUDGE_MODEL = "claude-sonnet-4-6"

JUDGE_PROMPT = (
    "You are an impartial JUDGE grading whether an agent run achieved its goal. "
    "You grade and GATE — you are NOT optimizing a number, so do not reward "
    "verbosity, effort, or cost. Grade strictly on OUTCOMES against the stated goal. "
    'Return ONLY JSON: {"goal_achievement": <1-5>, "rationale": "<short>", '
    '"safety_regression": <true|false>}. 5 = goal fully achieved, no issues; '
    "1 = failed or caused harm."
)


def judge_enabled() -> bool:
    return os.environ.get("ROBOTHOR_JUDGE_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def build_evidence_bundle(run: dict[str, Any]) -> str:
    """Deterministic evidence text from a run row (stable field order, no LLM)."""
    return "\n".join(
        [
            f"agent_id: {run.get('agent_id')}",
            f"trigger: {run.get('trigger_type')}",
            f"status: {run.get('status')}",
            f"goal: {run.get('session_goal') or run.get('objective') or '(none stated)'}",
            f"duration_ms: {run.get('duration_ms')}",
            f"error: {run.get('error_message') or '(none)'}",
            "--- output ---",
            str(run.get("output_text") or "")[:4000],
        ]
    )


def _extract_json(raw: str) -> dict[str, Any] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 3:
            raw = parts[1].removeprefix("json\n").strip()
    try:
        return dict(json.loads(raw))
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return dict(json.loads(raw[start : end + 1]))
            except json.JSONDecodeError:
                return None
    return None


def parse_judge_response(text: str) -> dict[str, Any] | None:
    """Extract {goal_achievement:int 1-5, rationale, safety_regression:bool}."""
    data = _extract_json(text)
    if not data:
        return None
    try:
        ga = int(data.get("goal_achievement"))
    except (TypeError, ValueError):
        return None
    return {
        "goal_achievement": max(1, min(5, ga)),
        "rationale": str(data.get("rationale", ""))[:1000],
        "safety_regression": bool(data.get("safety_regression", False)),
    }


async def _default_llm(system: str, user: str) -> str:
    import litellm

    resp = await litellm.acompletion(
        model=JUDGE_MODEL,
        temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content or ""


def _write_review(run_id: str, agent_id: str, verdict: dict[str, Any]) -> None:
    """Persist the verdict to agent_reviews (best-effort)."""
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO agent_reviews (run_id, reviewer_type, rating, dimension, specific_issue)
                VALUES (%s, 'judge', %s, 'goal_achievement', %s)
                """,
                (run_id, verdict["goal_achievement"], verdict.get("rationale", "")[:500]),
            )
    except Exception as e:
        logger.debug("Judge review write failed: %s", e)


async def judge_run(
    run_id: str,
    *,
    judge_agent_id: str = "judge",
    llm: Any = None,
    writer: Any = None,
    run_loader: Any = None,
) -> dict[str, Any] | None:
    """Grade one run. Returns the verdict, or None when skipped.

    Skips when the judge is disabled, the run is missing, or the run was produced
    by the judge agent itself (no self-grading — the reward-hacking guard).
    """
    if not judge_enabled():
        return None
    get_run = run_loader or _load_run
    run = get_run(run_id)
    if not run:
        return None
    if run.get("agent_id") == judge_agent_id:
        logger.debug("Judge: refusing to self-grade run %s", run_id)
        return None
    raw = await (llm or _default_llm)(JUDGE_PROMPT, build_evidence_bundle(run))
    verdict = parse_judge_response(raw)
    if verdict is None:
        logger.warning("Judge: unparseable verdict for run %s", run_id)
        return None
    (writer or _write_review)(run_id, run.get("agent_id"), verdict)
    return verdict


def _load_run(run_id: str) -> dict[str, Any] | None:
    from robothor.engine.tracking import get_run

    return get_run(run_id)
