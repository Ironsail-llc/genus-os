"""Deterministic people table for the devops weekly report.

Every number in the report comes from here, never from the analyst LLM.
Rows exist ONLY for active members of the team file
(:mod:`robothor.engine.reports.devops_team`); activity from any other handle
is listed under ``outside_roster`` (or ``bots``) and never becomes a person.

Inputs are the run files of one report week — ``github.json``
(:mod:`devops_github`), ``jira.json`` (``scripts/devops_collect_jira.py``)
and ``usage.json`` (:mod:`claude_usage_hr`) — plus up to three earlier
``people.json`` files for the trend. A metric whose source was unavailable is
``None`` with a reason in the member's ``unavailable`` map, never 0.
"""

from __future__ import annotations

from statistics import median
from typing import Any

from robothor.engine.reports.devops_team import Member, Team

#: Per-member metrics, in report order. Each maps to the collector metric(s)
#: it depends on, as ("github"|"jira", metric) pairs.
METRIC_SOURCES: dict[str, tuple[tuple[str, str], ...]] = {
    "tickets_resolved": (("jira", "resolved"),),
    "in_progress": (("jira", "in_progress"),),
    "prs_merged": (("github", "prs"),),
    "commits": (("github", "commits"),),
    "lines_added": (("github", "lines"),),
    "lines_deleted": (("github", "lines"),),
    "reviews_given": (("github", "reviews"),),
    "reviews_approved": (("github", "reviews"),),
    "reviews_changes_requested": (("github", "reviews"),),
    "reviews_commented": (("github", "reviews"),),
    "review_turnaround_median_h": (("github", "reviews"),),
    "changes_requested_received": (("github", "reviews"),),
    "merged_without_review": (("github", "prs"), ("github", "reviews")),
    "stale_prs": (("github", "stale"),),
}
METRICS = tuple(METRIC_SOURCES)
#: Metrics that count as "activity" for the no-activity flag.
ACTIVITY_METRICS = (
    "tickets_resolved",
    "in_progress",
    "prs_merged",
    "commits",
    "reviews_given",
)
#: Metrics summed into team totals (medians are not summable).
SUMMABLE = tuple(m for m in METRICS if m != "review_turnaround_median_h")

_JIRA_SENTINELS = frozenset({"", "unassigned", "unknown"})


def _unavailable_sources(
    github: dict[str, Any] | None, jira: dict[str, Any] | None
) -> dict[tuple[str, str], str]:
    """(source, metric) -> reason, for every collector metric that is not whole."""
    out: dict[tuple[str, str], str] = {}
    if github is None:
        for m in ("prs", "lines", "commits", "reviews", "stale"):
            out[("github", m)] = "GitHub collection did not run"
    else:
        for repo, rd in (github.get("repos") or {}).items():
            for m, reason in (rd.get("unavailable") or {}).items():
                out.setdefault(("github", m), f"{repo}: {reason}")
        if not github.get("repos") and github.get("errors"):
            msg = github["errors"][0].get("message", "no repos collected")
            for m in ("prs", "lines", "commits", "reviews", "stale"):
                out.setdefault(("github", m), msg)
    if jira is None:
        for m in ("resolved", "in_progress"):
            out[("jira", m)] = "JIRA collection did not run"
    else:
        for proj, pd in (jira.get("projects") or {}).items():
            for m, reason in (pd.get("unavailable") or {}).items():
                out.setdefault(("jira", m), f"{proj}: {reason}")
        for e in jira.get("errors") or []:
            q = e.get("query", "")
            if q in ("resolved", "in_progress"):
                out.setdefault(("jira", q), f"{e.get('project', '')}: {e.get('message', '')}")
    return out


def _blank() -> dict[str, Any]:
    return {
        "tickets_resolved": 0,
        "tickets_by_type": {},
        "in_progress": 0,
        "prs_merged": 0,
        "commits": 0,
        "lines_added": 0,
        "lines_deleted": 0,
        "reviews_given": 0,
        "reviews_approved": 0,
        "reviews_changes_requested": 0,
        "reviews_commented": 0,
        "turnarounds": [],
        "changes_requested_received": 0,
        "merged_without_review": 0,
        "stale_prs": 0,
    }


def aggregate_people(
    team: Team,
    github: dict[str, Any] | None,
    jira: dict[str, Any] | None,
    usage: dict[str, Any] | None = None,
    *,
    history: list[dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Build ``people.json`` for one report week.

    ``history`` is up to three earlier ``people.json`` dicts, oldest first
    (``None`` for a week with no file); each member's ``trend[metric]`` lists
    those weeks' values in the same order.
    """
    unavailable = _unavailable_sources(github, jira)
    acc: dict[str, dict[str, Any]] = {m.id: _blank() for m in team.active_members}
    outside: dict[tuple[str, str], dict[str, Any]] = {}
    bots: dict[str, dict[str, int]] = {}
    merged_without_review: list[dict[str, Any]] = []
    stale_all: list[dict[str, Any]] = []
    all_prs_merged = 0

    def owner(channel: str, handle: str) -> Member | None:
        m = team.member_for(channel, handle)
        return m if m is not None and m.active else None

    def bump_outside(source: str, handle: str, metric: str, n: int = 1) -> None:
        if channel_is_bot(source, handle):
            b = bots.setdefault(handle, {"prs_merged": 0, "commits": 0, "reviews": 0})
            b[metric] = b.get(metric, 0) + n
            return
        key = (source, handle)
        entry = outside.setdefault(key, {"handle": handle, "source": source, "counts": {}})
        m = team.member_for("jira" if source == "jira" else "github", handle)
        if m is not None and not m.active:
            entry["note"] = f"inactive roster member {m.id}"
        entry["counts"][metric] = entry["counts"].get(metric, 0) + n

    def channel_is_bot(source: str, handle: str) -> bool:
        return source == "github" and team.is_bot(handle)

    # ── GitHub ──
    for repo, rd in ((github or {}).get("repos") or {}).items():
        short = repo.split("/")[-1]
        for pr in rd.get("merged_prs") or []:
            author = pr.get("author") or ""
            m = owner("github", author)
            if not team.is_bot(author):
                all_prs_merged += 1
            if m is None:
                bump_outside("github", author, "prs_merged")
            else:
                a = acc[m.id]
                a["prs_merged"] += 1
                a["lines_added"] += pr.get("lines_added") or 0
                a["lines_deleted"] += pr.get("lines_deleted") or 0
            reviewers = pr.get("reviewers")
            if reviewers is not None and not team.is_bot(author):
                human = [r for r in reviewers if r != author and not team.is_bot(r)]
                if not human:
                    merged_without_review.append(
                        {
                            "repo": short,
                            "number": pr.get("number"),
                            "title": pr.get("title", ""),
                            "author": m.name if m else author,
                        }
                    )
                    if m is not None:
                        acc[m.id]["merged_without_review"] += 1
        for rv in rd.get("reviews") or []:
            reviewer = rv.get("reviewer") or ""
            m = owner("github", reviewer)
            state = rv.get("state")
            if m is None:
                bump_outside("github", reviewer, "reviews")
            else:
                a = acc[m.id]
                a["reviews_given"] += 1
                if state == "APPROVED":
                    a["reviews_approved"] += 1
                elif state == "CHANGES_REQUESTED":
                    a["reviews_changes_requested"] += 1
                elif state == "COMMENTED":
                    a["reviews_commented"] += 1
                if isinstance(rv.get("turnaround_h"), int | float):
                    a["turnarounds"].append(float(rv["turnaround_h"]))
            if state == "CHANGES_REQUESTED":
                am = owner("github", rv.get("pr_author") or "")
                if am is not None:
                    acc[am.id]["changes_requested_received"] += 1
        commits = rd.get("commits") or {}
        for login, n in (commits.get("by_login") or {}).items():
            m = owner("github", login)
            if m is None:
                bump_outside("github", login, "commits", n)
            else:
                acc[m.id]["commits"] += n
        for email, n in (commits.get("unlinked") or {}).items():
            bump_outside("git-email", email, "commits", n)
        for sp in rd.get("stale_prs") or []:
            m = owner("github", sp.get("author") or "")
            if team.is_bot(sp.get("author") or ""):
                continue
            stale_all.append({**sp, "repo": short, "author": m.name if m else sp.get("author")})
            if m is not None:
                acc[m.id]["stale_prs"] += 1

    # ── JIRA ──
    for pd in ((jira or {}).get("projects") or {}).values():
        for issue in pd.get("resolved") or []:
            name = issue.get("assignee") or ""
            if name.strip().lower() in _JIRA_SENTINELS:
                continue
            m = owner("jira", name)
            if m is None:
                bump_outside("jira", name, "tickets_resolved")
                continue
            a = acc[m.id]
            a["tickets_resolved"] += 1
            t = issue.get("type") or "Other"
            a["tickets_by_type"][t] = a["tickets_by_type"].get(t, 0) + 1
        for issue in pd.get("in_progress") or []:
            name = issue.get("assignee") or ""
            if name.strip().lower() in _JIRA_SENTINELS:
                continue
            m = owner("jira", name)
            if m is None:
                bump_outside("jira", name, "in_progress")
            else:
                acc[m.id]["in_progress"] += 1

    # ── Usage ──
    usage = usage or {"available": False, "reason": "usage collection did not run"}
    usage_rows = (usage.get("rows") or []) if usage.get("available") else []
    by_usage_name: dict[str, list[dict[str, Any]]] = {}
    for row in usage_rows:
        by_usage_name.setdefault(str(row.get("developer", "")).casefold(), []).append(row)
    roster_usage_names = {
        m.usage_name.casefold() for m in team.active_members if m.usage_name
    }
    unmatched = sorted(
        {
            str(r.get("developer", ""))
            for r in usage_rows
            if str(r.get("developer", "")).casefold() not in roster_usage_names
        }
    )

    # ── Rows ──
    hist = list(history or [])[-3:]
    while len(hist) < 3:
        hist.insert(0, None)
    hist_rows = [
        {m["id"]: m for m in (h or {}).get("members", [])} if h else {} for h in hist
    ]

    members_out: list[dict[str, Any]] = []
    no_activity: list[dict[str, Any]] = []
    for member in team.active_members:
        a = acc[member.id]
        metrics: dict[str, Any] = {}
        reasons: dict[str, str] = {}
        for metric, sources in METRIC_SOURCES.items():
            missing = [unavailable[s] for s in sources if s in unavailable]
            if missing:
                metrics[metric] = None
                reasons[metric] = missing[0]
            elif metric == "review_turnaround_median_h":
                metrics[metric] = round(median(a["turnarounds"]), 1) if a["turnarounds"] else None
            else:
                metrics[metric] = a[metric]
        metrics["tickets_by_type"] = (
            None if metrics["tickets_resolved"] is None else dict(sorted(a["tickets_by_type"].items()))
        )
        trend = {
            metric: [
                ((hr.get(member.id) or {}).get("metrics") or {}).get(metric) for hr in hist_rows
            ]
            for metric in METRICS
        }
        role = team.roles[member.role]
        row = {
            "id": member.id,
            "name": member.name,
            "role": member.role,
            "role_title": role.title,
            "metrics": metrics,
            "unavailable": reasons,
            "trend": trend,
            "usage": by_usage_name.get(member.usage_name.casefold(), []) if member.usage_name else [],
            "usage_name": member.usage_name,
        }
        known = [metrics[k] for k in ACTIVITY_METRICS if metrics[k] is not None]
        row["no_activity"] = bool(known) and all(v == 0 for v in known)
        if row["no_activity"]:
            no_activity.append({"id": member.id, "name": member.name, "role_title": role.title})
        members_out.append(row)

    team_totals: dict[str, Any] = {}
    for metric in SUMMABLE:
        vals = [r["metrics"][metric] for r in members_out]
        team_totals[metric] = None if any(v is None for v in vals) else sum(vals)
    team_totals["all_prs_merged_non_bot"] = (
        None if ("github", "prs") in unavailable else all_prs_merged
    )
    team_totals["members"] = len(members_out)

    partial_usage = [
        {
            "developer": r.get("developer"),
            "account": r.get("account"),
            "reading": r.get("reading"),
        }
        for r in usage_rows
        if r.get("reading") not in (None, "completed")
    ]
    missing_usage = [
        m.name
        for m in team.active_members
        if m.usage_name and m.usage_name.casefold() not in by_usage_name and usage.get("available")
    ]

    sources = {
        "github": {
            "available": github is not None and bool((github or {}).get("repos")),
            "complete": bool(github and github.get("complete")),
            "errors": list((github or {}).get("errors") or []),
        },
        "jira": {
            "available": jira is not None,
            "complete": bool(jira and jira.get("complete", not jira.get("errors"))),
            "errors": list((jira or {}).get("errors") or []),
        },
        "usage": {
            "available": bool(usage.get("available")),
            "reason": usage.get("reason"),
        },
    }
    outside_list = sorted(
        outside.values(), key=lambda o: (-sum(o["counts"].values()), o["source"], o["handle"])
    )
    return {
        "period": (github or {}).get("period") or (jira or {}).get("period") or {},
        "sources": sources,
        "unavailable_metrics": {f"{s}.{m}": r for (s, m), r in sorted(unavailable.items())},
        "roles": [
            {"key": r.key, "title": r.title, "focus": r.focus} for r in team.roles.values()
        ],
        "members": members_out,
        "team": team_totals,
        "outside_roster": outside_list,
        "bots": [
            {"handle": h, **counts}
            for h, counts in sorted(bots.items(), key=lambda kv: -sum(kv[1].values()))
        ],
        "no_activity": no_activity,
        "merged_without_review": merged_without_review,
        "stale_prs": sorted(stale_all, key=lambda p: -(p.get("age_days") or 0)),
        "usage": {
            "available": bool(usage.get("available")),
            "reason": usage.get("reason"),
            "report_date": usage.get("report_date"),
            "period": usage.get("period"),
            "rows": usage_rows,
            "unmatched": unmatched,
            "missing_for_members": missing_usage,
        },
        "data_quality": {
            "github_errors": sources["github"]["errors"],
            "jira_errors": sources["jira"]["errors"],
            "unavailable_metrics": {f"{s}.{m}": r for (s, m), r in sorted(unavailable.items())},
            "outside_roster": outside_list,
            "no_activity": no_activity,
            "partial_usage": partial_usage,
            "usage_unavailable": None if usage.get("available") else usage.get("reason"),
        },
    }
