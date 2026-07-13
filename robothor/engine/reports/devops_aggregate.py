"""Deterministic per-person aggregation for the DevOps weekly report.

Given the JSON outputs of `devops_collect_github.py` + `devops_collect_jira.py`,
join every author/assignee/reviewer handle to a CRM person via
`contact_identifiers`, sum per-person totals, and surface anomalies so the
report's "Report Quality" panel can show what was missed.

Designed as a pure function plus a thin DAL protocol — both are easily
unit-testable without a live database.
"""

from __future__ import annotations

from typing import Any, Protocol

_BOT_LOGIN_BASENAMES = frozenset(
    {
        "dependabot",
        "renovate",
        "github-actions",
        "release-please",
        "pre-commit-ci",
        "codecov",
        "codecov-commenter",
        "semantic-release",
    }
)


def _is_bot_login(login: str) -> bool:
    if not login:
        return False
    if login.endswith("[bot]"):
        return True
    return login.lower() in _BOT_LOGIN_BASENAMES


class AggregateDAL(Protocol):
    def resolve_handles(self, items: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """Return person_id for each (channel, identifier) that resolves; omit misses."""
        ...

    def get_roster(self) -> list[dict[str, Any]]:
        """Return canonical engineering roster.

        Each entry has: id, name, email, job_title, identities=[(channel, identifier), ...].
        """
        ...


class PostgresAggregateDAL:
    """Real DAL backed by the CRM Postgres database."""

    def __init__(self, conn: Any, tenant_id: str) -> None:
        self._conn = conn
        self._tenant_id = tenant_id

    def resolve_handles(self, items: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
        if not items:
            return {}
        channels = list({c for c, _ in items})
        identifiers = list({i for _, i in items})
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT ci.channel, ci.identifier, ci.person_id::text
                FROM contact_identifiers ci
                JOIN crm_people p ON p.id = ci.person_id
                WHERE ci.tenant_id = %s
                  AND ci.channel = ANY(%s)
                  AND ci.identifier = ANY(%s)
                  AND ci.person_id IS NOT NULL
                  AND p.deleted_at IS NULL
                """,
                (self._tenant_id, channels, identifiers),
            )
            rows = cur.fetchall()
        return {(r[0], r[1]): r[2] for r in rows if (r[0], r[1]) in items}

    def get_roster(self) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT
                    p.id::text,
                    COALESCE(NULLIF(TRIM(CONCAT_WS(' ', p.first_name, p.last_name)), ''), p.email),
                    p.email,
                    p.job_title
                FROM crm_people p
                JOIN contact_identifiers ci ON ci.person_id = p.id
                WHERE p.deleted_at IS NULL
                  AND p.tenant_id = %s
                  AND ci.tenant_id = %s
                  AND ci.channel IN ('github', 'jira_display_name')
                """,
                (self._tenant_id, self._tenant_id),
            )
            people = cur.fetchall()

            if not people:
                return []

            cur.execute(
                """
                SELECT person_id::text, channel, identifier
                FROM contact_identifiers
                WHERE tenant_id = %s
                  AND person_id = ANY(%s::uuid[])
                  AND channel IN ('github', 'jira_display_name')
                """,
                (self._tenant_id, [p[0] for p in people]),
            )
            id_rows = cur.fetchall()

        by_person: dict[str, list[tuple[str, str]]] = {}
        for pid, ch, ident in id_rows:
            by_person.setdefault(pid, []).append((ch, ident))

        return [
            {
                "id": pid,
                "name": name,
                "email": email or "",
                "job_title": job_title or "",
                "identities": by_person.get(pid, []),
            }
            for pid, name, email, job_title in people
        ]


def _collect_handles(
    github_data: dict[str, Any], jira_data: dict[str, Any]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Walk the collector outputs, returning every handle observed.

    Returns a dict keyed by (channel, identifier), each value holding:
        occurrences: int
        sources: set[str]  — "<repo>/authors", "<project>/by_assignee", etc.
        is_bot: bool
        per_repo: set[str]  — repos touched (for repos_touched aggregation)
    """
    seen: dict[tuple[str, str], dict[str, Any]] = {}

    def _bump(channel: str, ident: str, source: str, count: int) -> None:
        key = (channel, ident)
        entry = seen.setdefault(
            key,
            {
                "occurrences": 0,
                "sources": set(),
                "is_bot": _is_bot_login(ident) if channel == "github" else False,
            },
        )
        entry["occurrences"] += count
        entry["sources"].add(source)

    # GitHub authors / merged_by / reviewers — across both weekly windows
    for repo, repo_data in (github_data.get("repos") or {}).items():
        for week_key in ("pr_stats_current_week", "pr_stats_last_week"):
            stats = repo_data.get(week_key) or {}
            for login, n in (stats.get("authors") or {}).items():
                _bump("github", login, f"{repo}/{week_key}/authors", n)
            for login, n in (stats.get("merged_by") or {}).items():
                _bump("github", login, f"{repo}/{week_key}/merged_by", n)
            for login, n in (stats.get("bot_authors") or {}).items():
                _bump("github", login, f"{repo}/{week_key}/bot_authors", n)
        for r in (repo_data.get("review_stats") or {}).get("reviewers", []):
            login = r.get("reviewer")
            n = r.get("reviews_given", 0)
            if login:
                _bump("github", login, f"{repo}/reviewers", n)

    # JIRA assignees — across both weekly windows + in-progress
    for project, proj_data in (jira_data.get("projects") or {}).items():
        for week_key in ("resolved_current_week", "resolved_last_week"):
            agg = proj_data.get(week_key) or {}
            for name, n in (agg.get("by_assignee") or {}).items():
                _bump("jira_display_name", name, f"{project}/{week_key}", n)
        wip = proj_data.get("in_progress") or {}
        for name, n in (wip.get("by_assignee") or {}).items():
            _bump("jira_display_name", name, f"{project}/in_progress", n)

    return seen


def _person_totals(
    person_id: str,
    handles: dict[tuple[str, str], dict[str, Any]],
    handle_to_person: dict[tuple[str, str], str],
    github_data: dict[str, Any],
    jira_data: dict[str, Any],
) -> dict[str, Any]:
    """Sum activity for a single person across all their resolved handles."""
    my_keys = {k for k, p in handle_to_person.items() if p == person_id}
    my_github = {ident for (ch, ident) in my_keys if ch == "github"}
    my_jira = {ident for (ch, ident) in my_keys if ch == "jira_display_name"}

    prs_current = 0
    prs_last = 0
    repos_touched: set[str] = set()
    reviews_given = 0
    approvals = 0
    changes_requested = 0
    turnaround_hours: list[float] = []

    for repo, repo_data in (github_data.get("repos") or {}).items():
        cw = (repo_data.get("pr_stats_current_week") or {}).get("authors") or {}
        lw = (repo_data.get("pr_stats_last_week") or {}).get("authors") or {}
        touched = False
        for h in my_github:
            if h in cw:
                prs_current += cw[h]
                touched = True
            if h in lw:
                prs_last += lw[h]
                touched = True
        for r in (repo_data.get("review_stats") or {}).get("reviewers", []):
            if r.get("reviewer") in my_github:
                reviews_given += r.get("reviews_given", 0)
                approvals += r.get("approvals", 0)
                changes_requested += r.get("changes_requested", 0)
                t = r.get("avg_turnaround_hours")
                if isinstance(t, int | float):
                    turnaround_hours.append(float(t))
                touched = True
        if touched:
            repos_touched.add(repo)

    tickets_current = 0
    tickets_last = 0
    in_progress_tickets = 0
    for proj_data in (jira_data.get("projects") or {}).values():
        cw = (proj_data.get("resolved_current_week") or {}).get("by_assignee") or {}
        lw = (proj_data.get("resolved_last_week") or {}).get("by_assignee") or {}
        wip = (proj_data.get("in_progress") or {}).get("by_assignee") or {}
        for j in my_jira:
            tickets_current += cw.get(j, 0)
            tickets_last += lw.get(j, 0)
            in_progress_tickets += wip.get(j, 0)

    avg_turnaround = (
        round(sum(turnaround_hours) / len(turnaround_hours), 1) if turnaround_hours else None
    )

    return {
        "current_week": {
            "prs_merged": prs_current,
            "tickets_resolved": tickets_current,
        },
        "last_week": {
            "prs_merged": prs_last,
            "tickets_resolved": tickets_last,
        },
        "reviews_30d": {
            "reviews_given": reviews_given,
            "approvals": approvals,
            "changes_requested": changes_requested,
            "avg_turnaround_hours": avg_turnaround,
        },
        "in_progress_tickets": in_progress_tickets,
        "repos_touched": sorted(repos_touched),
        "github_handles": sorted(my_github),
        "jira_handles": sorted(my_jira),
    }


def _provisional_totals(
    channel: str,
    identifier: str,
    github_data: dict[str, Any],
    jira_data: dict[str, Any],
) -> dict[str, Any]:
    """Activity totals for a single unresolved handle (no CRM person yet)."""
    fake_pid = f"__provisional__::{channel}::{identifier}"
    handle_to_person = {(channel, identifier): fake_pid}
    return _person_totals(fake_pid, {}, handle_to_person, github_data, jira_data)


_DEFAULT_ENGINEER_ROLE_KEYWORDS: tuple[str, ...] = (
    "Engineer",
    "Developer",
    "SRE",
    "DevOps",
)


def _is_engineer(job_title: str, keywords: tuple[str, ...] | list[str]) -> bool:
    if not job_title:
        return False
    t = job_title.lower()
    return any(k.lower() in t for k in keywords)


def aggregate_people(
    github_data: dict[str, Any],
    jira_data: dict[str, Any],
    dal: AggregateDAL,
    *,
    engineer_role_keywords: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build the per-person rollup for the devops report.

    Resolved CRM people appear with `resolved: True`. Unresolved handles
    (new hires, contractors, JIRA sentinels like 'Unassigned') appear as
    provisional rows with `resolved: False` so their activity is never
    silently dropped — they're also surfaced in `unresolved_handles` so
    the operator knows which handles still need `contact_identifiers`
    rows.

    `missing_from_roster` is narrowed to roster members whose `job_title`
    matches one of `engineer_role_keywords` (default: Engineer / Developer
    / SRE / DevOps). QA / PM / stakeholders who have github handles for
    permissions but aren't expected to merge PRs don't appear there.

    Sort order: primary sort by last_week activity (the completed full
    week), not current_week (which is always a partial day at Monday 9 AM
    report time).
    """
    keywords = (
        tuple(engineer_role_keywords)
        if engineer_role_keywords is not None
        else _DEFAULT_ENGINEER_ROLE_KEYWORDS
    )
    handles = _collect_handles(github_data, jira_data)

    # Partition out bots before resolving — they will never be people.
    non_bot_handles = {k for k, v in handles.items() if not v["is_bot"]}

    handle_to_person = dal.resolve_handles(non_bot_handles)
    resolved_keys = set(handle_to_person.keys())

    # Roster: anyone the CRM knows has a github/jira handle is an "expected"
    # engineer; missing entirely from this run → missing_from_roster.
    roster = dal.get_roster()
    roster_by_id = {p["id"]: p for p in roster}

    # Resolved CRM people with activity
    seen_person_ids: set[str] = set()
    people: list[dict[str, Any]] = []
    for pid, person in roster_by_id.items():
        if pid not in set(handle_to_person.values()):
            continue
        totals = _person_totals(pid, handles, handle_to_person, github_data, jira_data)
        if (
            totals["current_week"]["prs_merged"] == 0
            and totals["last_week"]["prs_merged"] == 0
            and totals["current_week"]["tickets_resolved"] == 0
            and totals["last_week"]["tickets_resolved"] == 0
            and totals["reviews_30d"]["reviews_given"] == 0
            and totals["in_progress_tickets"] == 0
        ):
            # Resolved but no activity → falls through to missing_from_roster
            continue
        people.append(
            {
                "person_id": pid,
                "name": person.get("name", ""),
                "email": person.get("email", ""),
                "job_title": person.get("job_title", ""),
                "resolved": True,
                **totals,
            }
        )
        seen_person_ids.add(pid)

    # Provisional rows for unresolved (non-bot) handles — include them so
    # the report covers ALL observed work, even if CRM hasn't caught up.
    unresolved = []
    for (ch, ident), info in handles.items():
        if info["is_bot"]:
            continue
        if (ch, ident) in resolved_keys:
            continue
        totals = _provisional_totals(ch, ident, github_data, jira_data)
        people.append(
            {
                "person_id": None,
                "name": ident,
                "email": "",
                "job_title": "",
                "resolved": False,
                "unresolved_channel": ch,
                **totals,
            }
        )
        unresolved.append(
            {
                "channel": ch,
                "identifier": ident,
                "occurrences": info["occurrences"],
                "sources": sorted(info["sources"]),
            }
        )

    # Missing from roster (on roster, no activity this period).
    # Narrowed to engineering roles — QA/PM/stakeholders with github
    # handles aren't expected to merge PRs in normal course.
    missing = []
    for pid, person in roster_by_id.items():
        if pid in seen_person_ids:
            continue
        if not _is_engineer(person.get("job_title", ""), keywords):
            continue
        missing.append(
            {
                "person_id": pid,
                "name": person.get("name", ""),
                "job_title": person.get("job_title", ""),
                "reason": "expected on roster, no activity this period",
            }
        )

    # Bots: surface them so they're visible, not silently dropped.
    bot_rows: dict[str, dict[str, Any]] = {}
    for repo, repo_data in (github_data.get("repos") or {}).items():
        for week in ("pr_stats_current_week", "pr_stats_last_week"):
            for login, count in ((repo_data.get(week) or {}).get("bot_authors") or {}).items():
                row = bot_rows.setdefault(
                    login,
                    {
                        "channel": "github",
                        "identifier": login,
                        "merged_count": 0,
                        "repos": set(),
                    },
                )
                row["merged_count"] += count
                row["repos"].add(repo)
    bots_filtered = [{**row, "repos": sorted(row["repos"])} for row in bot_rows.values()]

    # Totals — sum of resolved per-person figures, by week.
    totals = {
        "current_week": {
            "prs_merged_human": sum(p["current_week"]["prs_merged"] for p in people),
            "tickets_resolved": sum(p["current_week"]["tickets_resolved"] for p in people),
        },
        "last_week": {
            "prs_merged_human": sum(p["last_week"]["prs_merged"] for p in people),
            "tickets_resolved": sum(p["last_week"]["tickets_resolved"] for p in people),
        },
        "people_active": len(people),
        "people_missing": len(missing),
        "unresolved_handle_count": len(unresolved),
        "bots_filtered_count": sum(b["merged_count"] for b in bots_filtered),
    }

    return {
        "period": github_data.get("period") or jira_data.get("period") or {},
        "people": sorted(
            people,
            # Primary sort: last_week activity (completed full week).
            # current_week is always partial at Monday 9 AM report time.
            key=lambda p: (
                -(p["last_week"]["prs_merged"] + p["last_week"]["tickets_resolved"]),
                p["name"],
            ),
        ),
        "missing_from_roster": sorted(missing, key=lambda m: m["name"]),
        "unresolved_handles": sorted(
            unresolved, key=lambda u: (-u["occurrences"], u["identifier"])
        ),
        "bots_filtered": sorted(bots_filtered, key=lambda b: -b["merged_count"]),
        "totals": totals,
    }
