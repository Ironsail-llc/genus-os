"""Compare legacy Buddy overall_score against goals.py achievement score.

Produces a per-agent table showing how ranking changes when we swap scoring
sources. Also runs a coverage check: every metric in the taxonomy declared on
any agent should produce a non-None value for at least one agent.

Throwaway Phase-2 tool — safe to delete after the validation sign-off.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.engine.goals import (
    compute_achievement_score,
    compute_goal_metrics,
    parse_goals_from_manifest,
)

AGENTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "agents"
REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def load_manifests() -> list[tuple[str, dict]]:
    manifests: list[tuple[str, dict]] = []
    for path in sorted(AGENTS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
            continue
        agent_id = data.get("id")
        if not agent_id:
            continue
        manifests.append((agent_id, data))
    return manifests


def legacy_scores() -> dict[str, tuple[int | None, str | None]]:
    """Latest agent_buddy_stats row per agent — (overall_score, stat_date)."""
    out: dict[str, tuple[int | None, str | None]] = {}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (agent_id)
                agent_id, overall_score, stat_date
            FROM agent_buddy_stats
            ORDER BY agent_id, stat_date DESC
            """
        )
        for agent_id, overall, stat_date in cur.fetchall():
            out[agent_id] = (overall, str(stat_date) if stat_date else None)
    return out


def run_activity(agent_id: str, days: int = 7) -> tuple[int, int]:
    """(total_runs, successful_runs) over the trailing window."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'completed' AND error_message IS NULL) AS ok
            FROM agent_runs
            WHERE agent_id = %s
              AND parent_run_id IS NULL
              AND started_at >= NOW() - (%s || ' days')::interval
            """,
            (agent_id, days),
        )
        row = cur.fetchone()
        return (int(row[0] or 0), int(row[1] or 0))


def metric_coverage_check(manifests: list[tuple[str, dict]]) -> dict[str, dict[str, object]]:
    """For every declared (agent, metric, window_days), record the computed value."""
    out: dict[str, dict[str, object]] = {}
    for agent_id, manifest in manifests:
        goals = parse_goals_from_manifest(manifest)
        windows = {g.window_days for g in goals}
        snapshots = {w: compute_goal_metrics(agent_id, window_days=w) for w in windows}
        for g in goals:
            value = snapshots[g.window_days].get(g.metric)
            out.setdefault(g.metric, {})[agent_id] = value
    return out


def main() -> int:
    manifests = load_manifests()
    legacy = legacy_scores()

    rows: list[dict[str, object]] = []
    for agent_id, manifest in manifests:
        goals = parse_goals_from_manifest(manifest)
        if not goals:
            legacy_overall, legacy_date = legacy.get(agent_id, (None, None))
            total, ok = run_activity(agent_id)
            rows.append(
                {
                    "agent_id": agent_id,
                    "legacy": legacy_overall,
                    "legacy_date": legacy_date,
                    "goals_score_0_100": None,
                    "goals_rating_1_5": None,
                    "satisfied": None,
                    "breached": None,
                    "runs_7d": total,
                    "ok_7d": ok,
                    "note": "no goals declared",
                }
            )
            continue

        achievement = compute_achievement_score(agent_id, goals, tenant_id=DEFAULT_TENANT)
        legacy_overall, legacy_date = legacy.get(agent_id, (None, None))
        total, ok = run_activity(agent_id)
        rows.append(
            {
                "agent_id": agent_id,
                "legacy": legacy_overall,
                "legacy_date": legacy_date,
                "goals_score_0_100": int(round(achievement["score"] * 100)),
                "goals_rating_1_5": achievement["rating"],
                "satisfied": len(achievement["satisfied_goals"]),
                "breached": len(achievement["breached_goals"]),
                "runs_7d": total,
                "ok_7d": ok,
                "note": "",
            }
        )

    # Rank by new score (desc), then legacy as tiebreak.
    rows.sort(
        key=lambda r: (
            -(r["goals_score_0_100"] if r["goals_score_0_100"] is not None else -1),
            -(r["legacy"] if r["legacy"] is not None else -1),
        )
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / "buddy-goals-validation-2026-04-18.md"

    lines: list[str] = []
    lines.append("# Buddy Scoring Validation — 2026-04-18")
    lines.append("")
    lines.append(
        "Side-by-side: legacy `agent_buddy_stats.overall_score` vs. "
        "goals.py `compute_achievement_score`. Sorted by new score (desc)."
    )
    lines.append("")
    lines.append(
        "| Agent | Legacy overall | New score (0-100) | New rating (1-5) | Satisfied/breached | 7d runs (ok/total) | Note |"
    )
    lines.append(
        "|-------|----------------|--------------------|-------------------|---------------------|---------------------|------|"
    )
    for r in rows:
        legacy_cell = "—" if r["legacy"] is None else str(r["legacy"])
        new_cell = "—" if r["goals_score_0_100"] is None else str(r["goals_score_0_100"])
        rating_cell = "—" if r["goals_rating_1_5"] is None else str(r["goals_rating_1_5"])
        sat_cell = "—" if r["satisfied"] is None else f"{r['satisfied']}/{r['breached']}"
        lines.append(
            f"| {r['agent_id']} | {legacy_cell} | {new_cell} | {rating_cell} | "
            f"{sat_cell} | {r['ok_7d']}/{r['runs_7d']} | {r['note']} |"
        )

    lines.append("")
    lines.append("## Metric coverage check")
    lines.append("")
    lines.append("For every metric declared on any agent, the computed value per agent.")
    lines.append("Null = metric returned None (likely insufficient data or query missed).")
    lines.append("")
    coverage = metric_coverage_check(manifests)
    lines.append("| Metric | Agents with values / declarations | Sample |")
    lines.append("|--------|-----------------------------------|--------|")
    for metric, per_agent in sorted(coverage.items()):
        total_declare = len(per_agent)
        have_value = sum(1 for v in per_agent.values() if v is not None)
        sample_entries = sorted(per_agent.items())[:3]
        sample = ", ".join(
            f"{aid}={'null' if v is None else round(float(v), 3) if isinstance(v, (int, float)) else v}"
            for aid, v in sample_entries
        )
        lines.append(f"| `{metric}` | {have_value}/{total_declare} | {sample} |")

    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append(
        "Operator's claim: legacy scores reward idleness because `debug=100` when there are no errors. "
        "Validation passes if high-activity agents (email-responder, curiosity-engine, email-analyst) "
        "rank at or above low-activity agents (morning-briefing, vision-monitor) under the new score, "
        "**and** high-activity agents' legacy/new delta is negative (legacy over-estimated them)."
    )
    lines.append("")
    lines.append("### Biggest legacy/new deltas")
    lines.append("")
    lines.append("| Agent | Legacy | New | Delta | 7d runs |")
    lines.append("|-------|--------|-----|-------|---------|")
    delta_rows = [
        r for r in rows if isinstance(r["legacy"], int) and isinstance(r["goals_score_0_100"], int)
    ]
    delta_rows.sort(
        key=lambda r: abs(int(r["goals_score_0_100"]) - int(r["legacy"])),
        reverse=True,
    )
    for r in delta_rows[:10]:
        delta = int(r["goals_score_0_100"]) - int(r["legacy"])
        lines.append(
            f"| {r['agent_id']} | {r['legacy']} | {r['goals_score_0_100']} | "
            f"{'+' if delta > 0 else ''}{delta} | {r['ok_7d']}/{r['runs_7d']} |"
        )
    lines.append("")
    lines.append("### Metric implementation gaps (feed into Phase 3)")
    lines.append("")
    gap_metrics = [
        (m, per_agent)
        for m, per_agent in coverage.items()
        if sum(1 for v in per_agent.values() if v is not None) == 0
    ]
    if gap_metrics:
        lines.append(
            "These metrics are declared on agent manifests but `get_agent_stats` / "
            "`compute_goal_metrics` never populate them. Agents declaring them get "
            "null → counted as breached → achievement score unfairly penalised."
        )
        lines.append("")
        for m, per_agent in sorted(gap_metrics):
            agents = ", ".join(sorted(per_agent.keys()))
            lines.append(f"- `{m}` (declared on {len(per_agent)} agents: {agents})")
        lines.append("")
        lines.append(
            "Phase 3 resolves this by either implementing the metric in "
            "`robothor/engine/analytics.py` or removing the declaration where "
            "the metric can't be computed today."
        )
    else:
        lines.append("All declared metrics have at least one agent with a computed value.")
    lines.append("")

    out_path.write_text("\n".join(lines) + "\n")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
