"""Assemble the devops weekly report from the aggregate plus the analyst's prose.

The split of responsibility is strict:

* ``people.json`` (:mod:`devops_aggregate`) supplies EVERY number, name and
  role in the report.
* The analyst LLM supplies only narrative, in this contract
  (``analyst.json``)::

      {
        "summary": "2-4 sentences on the week",
        "per_person": {"<member id>": "one short note"},
        "bottlenecks": ["text", ...]   # or [{"text", "severity", "recommendation"}]
        "si_vs_output": "how Claude usage relates to output"
      }

  Anything else in the analyst's file is ignored.

:func:`check_narrative` is a deterministic guard: a per-person note that
attributes a number to the member (``7 PRs``, ``12 commits``, ``3 reviews``,
``2 tickets``, ``64%``) which does not match the table is dropped and the
report says "narrative withheld" instead. A narrative failure never blocks
the report.
"""

from __future__ import annotations

import re
from typing import Any

#: Number + unit in a per-person note -> the metric it must equal.
_CLAIM = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*"
    r"(%|prs?\b|pull requests?\b|commits?\b|reviews?\b|tickets?\b)",
    re.IGNORECASE,
)
_UNIT_METRIC = {
    "pr": "prs_merged",
    "prs": "prs_merged",
    "pull request": "prs_merged",
    "pull requests": "prs_merged",
    "commit": "commits",
    "commits": "commits",
    "review": "reviews_given",
    "reviews": "reviews_given",
    "ticket": ("tickets_shipped", "tickets_resolved"),
    "tickets": ("tickets_shipped", "tickets_resolved"),
}
TABLE_METRICS = (
    "tickets_shipped",
    "tickets_resolved",
    "in_progress",
    "prs_merged",
    "commits",
    "lines_added",
    "lines_deleted",
    "reviews_given",
    "review_turnaround_median_h",
    "changes_requested_received",
    "stale_prs",
)
WITHHELD_TEXT = "narrative withheld: numbers didn't match data"


def _note_ok(note: str, member: dict[str, Any]) -> bool:
    metrics = member.get("metrics") or {}
    pcts: set[float] = set()
    for row in member.get("usage") or []:
        for k in ("weekly_pct", "session_pct", "fable_pct"):
            if isinstance(row.get(k), int | float):
                pcts.add(float(row[k]))
    for m in _CLAIM.finditer(note):
        value = float(m.group(1))
        unit = m.group(2).lower()
        if unit == "%":
            if value not in pcts:
                return False
            continue
        keys = _UNIT_METRIC[unit]
        actual = [metrics.get(k) for k in ((keys,) if isinstance(keys, str) else keys)]
        if not any(isinstance(a, int | float) and float(a) == value for a in actual):
            return False
    return True


def _bottlenecks(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out = []
    for b in raw:
        if isinstance(b, str) and b.strip():
            out.append({"text": b.strip(), "severity": "medium", "recommendation": ""})
        elif isinstance(b, dict):
            text = b.get("text") or b.get("description") or ""
            if text:
                out.append(
                    {
                        "text": str(text),
                        "severity": str(b.get("severity") or "medium"),
                        "recommendation": str(b.get("recommendation") or ""),
                    }
                )
    return out


def check_narrative(
    people: dict[str, Any], analyst: dict[str, Any] | None
) -> tuple[dict[str, Any], list[str]]:
    """Clean narrative + ids of members whose note was withheld."""
    analyst = analyst if isinstance(analyst, dict) else {}
    members = {m["id"]: m for m in people.get("members") or []}
    raw_notes = analyst.get("per_person")
    if isinstance(raw_notes, list):
        raw_notes = {
            str(n.get("member_id") or n.get("id")): n.get("note")
            for n in raw_notes
            if isinstance(n, dict)
        }
    if not isinstance(raw_notes, dict):
        raw_notes = {}
    notes: dict[str, str] = {}
    withheld: list[str] = []
    for mid, note in raw_notes.items():
        if mid not in members or not isinstance(note, str) or not note.strip():
            continue
        if _note_ok(note, members[mid]):
            notes[mid] = note.strip()
        else:
            withheld.append(mid)

    def text(key: str) -> str:
        v = analyst.get(key)
        return v.strip() if isinstance(v, str) else ""

    return (
        {
            "summary": text("summary"),
            "si_vs_output": text("si_vs_output"),
            "per_person": notes,
            "bottlenecks": _bottlenecks(analyst.get("bottlenecks")),
        },
        sorted(withheld),
    )


def _arrow(current: Any, trend: list[Any] | None) -> str | None:
    prev = (trend or [None])[-1]
    if not isinstance(current, int | float) or not isinstance(prev, int | float):
        return None
    if current > prev:
        return "up"
    if current < prev:
        return "down"
    return "flat"


def build_report_context(
    people: dict[str, Any], analyst: dict[str, Any] | None, *, jira: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Everything the template renders. Numbers only ever come from ``people``."""
    narrative, withheld = check_narrative(people, analyst)
    narrative_error = None
    if not isinstance(analyst, dict) or not any(
        analyst.get(k) for k in ("summary", "per_person", "bottlenecks", "si_vs_output")
    ):
        narrative_error = "narrative unavailable (analyst output missing or empty)"

    members = people.get("members") or []
    by_role: dict[str, list[dict[str, Any]]] = {}
    for m in members:
        row = dict(m)
        row["arrows"] = {
            k: _arrow((m.get("metrics") or {}).get(k), (m.get("trend") or {}).get(k))
            for k in TABLE_METRICS
        }
        row["note"] = narrative["per_person"].get(m["id"])
        row["note_withheld"] = m["id"] in withheld
        by_role.setdefault(m.get("role", ""), []).append(row)
    role_groups = [
        {**r, "members": by_role.pop(r["key"])}
        for r in people.get("roles") or []
        if r["key"] in by_role
    ]
    role_groups.extend(
        {"key": key, "title": key, "focus": "", "members": rows} for key, rows in by_role.items()
    )

    usage = dict(people.get("usage") or {})
    name_by_usage = {
        (m.get("usage_name") or "").casefold(): m["name"] for m in members if m.get("usage_name")
    }
    usage["rows"] = [
        {**r, "member": name_by_usage.get(str(r.get("developer", "")).casefold())}
        for r in usage.get("rows") or []
    ]

    dq = dict(people.get("data_quality") or {})
    dq["narrative_withheld"] = [
        next((m["name"] for m in members if m["id"] == mid), mid) for mid in withheld
    ]
    dq["narrative_error"] = narrative_error
    dq["has_issues"] = any(
        dq.get(k)
        for k in (
            "github_errors",
            "jira_errors",
            "unavailable_metrics",
            "outside_roster",
            "no_activity",
            "partial_usage",
            "usage_unavailable",
            "narrative_withheld",
            "narrative_error",
        )
    )
    open_backlog = None
    if jira:
        counts = [p.get("open_count") for p in (jira.get("projects") or {}).values()]
        if counts and all(isinstance(c, int) for c in counts):
            open_backlog = sum(counts)
    return {
        "period": people.get("period") or {},
        "team": people.get("team") or {},
        "open_backlog": open_backlog,
        "role_groups": role_groups,
        "usage": usage,
        "narrative": narrative,
        "data_quality": dq,
        "merged_without_review": people.get("merged_without_review") or [],
        "stale_prs": people.get("stale_prs") or [],
        "bots": people.get("bots") or [],
        "withheld_text": WITHHELD_TEXT,
    }
