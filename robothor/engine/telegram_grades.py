"""Presentation of benchmark grades on Telegram."""

from __future__ import annotations

from typing import Any


def format_agent_grades(grades: list[dict[str, Any]]) -> str:
    """Render agent grade rows for /goals. Pure — the percentage is derived.

    The fraction and the percentage come from the same two numbers. They used
    to come from different columns: ``{passed}/{total}`` from the counts and
    ``({pct}%)`` from ``pass_rate``, which held the partial-credit aggregate.
    crm-hygiene printed as ``0/4 (18%)`` — zero cases passed, an 18% grade.
    """

    def _rate(g: dict[str, Any]) -> float:
        total = int(g.get("total_cases") or 0)
        return (int(g.get("passed") or 0) / total) if total else 0.0

    lines = ["<b>Agent Performance — job pass rate</b>", ""]
    for g in sorted(grades, key=_rate):
        total = int(g.get("total_cases") or 0)
        passed = int(g.get("passed") or 0)
        pct = int(round(_rate(g) * 100))
        agent_id = str(g.get("agent_id", "?"))
        lines.append(f"  {agent_id:<22s}{passed}/{total} ({pct}%)")

        aggregate = g.get("aggregate_score")
        if aggregate is not None:
            lines.append(f"  ↳ {float(aggregate) * 100:.0f}% partial credit")
        judge_errors = int(g.get("judge_errors") or 0)
        if judge_errors:
            plural = "s" if judge_errors != 1 else ""
            lines.append(f"  ↳ {judge_errors} judge error{plural} — graded as failures")
        failing = [str(c) for c in (g.get("failing_case_ids") or [])][:3]
        if failing:
            lines.append(f"  ↳ failing: {', '.join(failing)}")
    lines.append("")
    lines.append(
        "<i>Each line is the agent's grade on its docs/benchmarks/&lt;agent&gt;/suite.yaml: "
        "cases passed over cases in the suite. Partial credit is the weighted mean of "
        "per-case scores — it moves before the pass rate does, and is never the grade. "
        "Cost is observed, never optimized.</i>"
    )
    return "\n".join(lines)
