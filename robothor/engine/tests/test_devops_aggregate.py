"""Tests for the deterministic people table of the devops weekly report."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from robothor.engine.reports.devops_aggregate import aggregate_people
from robothor.engine.reports.devops_team import parse_team

PERIOD = {"week_start": "2026-05-11", "report_label": "Week of 2026-05-11"}
REPO = "acme/widgets"


def _team():
    return parse_team(
        {
            "roles": {
                "senior": {"title": "Senior Engineer", "focus": "Architecture"},
                "ai_reviewer": {"title": "AI Reviewer", "focus": "Reviews"},
            },
            "members": [
                {
                    "id": "alice",
                    "name": "Alice Example",
                    "role": "senior",
                    "github": ["alice-gh", "alice-alt"],
                    "jira": ["Alice Example"],
                    "usage_name": "Alice",
                },
                {
                    "id": "bob",
                    "name": "Bob Example",
                    "role": "senior",
                    "github": ["bob-gh"],
                    "jira": ["Bob Example"],
                    "usage_name": "Bob",
                },
                {
                    "id": "reviewer",
                    "name": "Review Agent",
                    "role": "ai_reviewer",
                    "github": ["review-agent[bot]"],
                },
                {
                    "id": "carol",
                    "name": "Carol Former",
                    "role": "senior",
                    "github": ["carol-gh"],
                    "active": False,
                },
            ],
            "bots_excluded": ["dependabot[bot]"],
        }
    )


def _merged(n, author, *, added=10, deleted=2, reviewers=("bob-gh",)):
    return {
        "number": n,
        "title": f"PR {n}",
        "author": author,
        "base": "main",
        "merged_by": author,
        "lines_added": added,
        "lines_deleted": deleted,
        "reviewers": list(reviewers) if reviewers is not None else None,
    }


def _review(reviewer, state, pr_author="alice-gh", turnaround=None, pr=1):
    return {
        "pr": pr,
        "pr_author": pr_author,
        "reviewer": reviewer,
        "state": state,
        "turnaround_h": turnaround,
    }


def _github() -> dict[str, Any]:
    return {
        "period": PERIOD,
        "complete": True,
        "errors": [],
        "repos": {
            REPO: {
                "unavailable": {},
                "merged_prs": [
                    _merged(1, "alice-gh"),
                    _merged(2, "alice-alt", added=5, deleted=5),
                    _merged(3, "dependabot[bot]", reviewers=[]),
                    _merged(4, "mallory-gh", reviewers=["dependabot[bot]"]),
                    _merged(5, "carol-gh"),
                ],
                "reviews": [
                    _review("bob-gh", "APPROVED", turnaround=2.0),
                    _review("bob-gh", "CHANGES_REQUESTED", turnaround=6.0, pr=2),
                    _review("bob-gh", "COMMENTED", turnaround=None, pr=2),
                    _review("review-agent[bot]", "COMMENTED", turnaround=1.0),
                ],
                "commits": {
                    "by_login": {"alice-gh": 3, "alice-alt": 1, "dependabot[bot]": 2},
                    "unlinked": {"ghost@example.com": 1},
                },
                "stale_prs": [{"number": 9, "title": "Old", "author": "bob-gh", "age_days": 20}],
            }
        },
    }


def _jira() -> dict[str, Any]:
    return {
        "period": PERIOD,
        "complete": True,
        "errors": [],
        "projects": {
            "ENG": {
                "resolved": [
                    {"key": "ENG-1", "assignee": "Alice Example", "type": "Bug"},
                    {"key": "ENG-2", "assignee": "Alice Example", "type": "Story"},
                    {"key": "ENG-3", "assignee": "Dan Outsider", "type": "Task"},
                ],
                "in_progress": [{"key": "ENG-4", "assignee": "Bob Example", "type": "Story"}],
            }
        },
    }


def _usage() -> dict[str, Any]:
    return {
        "available": True,
        "report_date": "2026-05-15",
        "rows": [
            {"developer": "Alice", "account": "Acct 1", "weekly_pct": 80, "reading": "completed"},
            {"developer": "Bob", "account": "Acct 1", "weekly_pct": 5, "reading": "post_reset"},
            {"developer": "Zed", "account": "Acct 1", "weekly_pct": 50, "reading": "completed"},
        ],
    }


def _member(out, mid):
    return next(m for m in out["members"] if m["id"] == mid)


class TestRosterMapping:
    def test_multiple_handles_sum_into_one_person(self):
        out = aggregate_people(_team(), _github(), _jira(), _usage())
        alice = _member(out, "alice")["metrics"]
        assert alice["prs_merged"] == 2
        assert alice["commits"] == 4
        assert alice["lines_added"] == 15
        assert alice["lines_deleted"] == 7
        assert alice["tickets_resolved"] == 2
        assert alice["tickets_by_type"] == {"Bug": 1, "Story": 1}
        assert alice["changes_requested_received"] == 1

    def test_rows_only_for_active_roster_members(self):
        out = aggregate_people(_team(), _github(), _jira(), _usage())
        assert [m["id"] for m in out["members"]] == ["alice", "bob", "reviewer"]

    def test_non_roster_activity_goes_to_outside_roster(self):
        out = aggregate_people(_team(), _github(), _jira(), _usage())
        handles = {(o["source"], o["handle"]) for o in out["outside_roster"]}
        assert ("github", "mallory-gh") in handles
        assert ("jira", "Dan Outsider") in handles
        assert ("git-email", "ghost@example.com") in handles
        assert ("github", "carol-gh") in handles  # inactive member: not a row
        assert not any(o["handle"] == "dependabot[bot]" for o in out["outside_roster"])

    def test_bots_only_on_bots_line(self):
        out = aggregate_people(_team(), _github(), _jira(), _usage())
        bots = {b["handle"]: b for b in out["bots"]}
        assert bots["dependabot[bot]"]["prs_merged"] == 1
        assert bots["dependabot[bot]"]["commits"] == 2

    def test_roster_ai_member_is_counted(self):
        out = aggregate_people(_team(), _github(), _jira(), _usage())
        reviewer = _member(out, "reviewer")
        assert reviewer["metrics"]["reviews_given"] == 1
        assert reviewer["role_title"] == "AI Reviewer"

    def test_review_breakdown_and_median_turnaround(self):
        out = aggregate_people(_team(), _github(), _jira(), _usage())
        bob = _member(out, "bob")["metrics"]
        assert bob["reviews_given"] == 3
        assert bob["reviews_approved"] == 1
        assert bob["reviews_changes_requested"] == 1
        assert bob["reviews_commented"] == 1
        assert bob["review_turnaround_median_h"] == 4.0
        assert bob["stale_prs"] == 1
        assert bob["in_progress"] == 1

    def test_merged_without_human_review(self):
        out = aggregate_people(_team(), _github(), _jira(), _usage())
        nums = {p["number"] for p in out["merged_without_review"]}
        # PR 4's only reviewer is a bot; bot-authored PR 3 is not listed
        assert nums == {4}

    def test_member_with_no_activity_is_flagged(self):
        gh = _github()
        gh["repos"][REPO]["reviews"] = [
            r for r in gh["repos"][REPO]["reviews"] if r["reviewer"] != "review-agent[bot]"
        ]
        out = aggregate_people(_team(), gh, _jira(), _usage())
        assert [m["id"] for m in out["no_activity"]] == ["reviewer"]


class TestUnavailable:
    def test_failed_repo_metric_is_null_with_reason_not_zero(self):
        gh = _github()
        gh["repos"][REPO]["unavailable"] = {"commits": "/commits: 502 boom"}
        gh["complete"] = False
        out = aggregate_people(_team(), gh, _jira(), _usage())
        alice = _member(out, "alice")
        assert alice["metrics"]["commits"] is None
        assert "502" in alice["unavailable"]["commits"]
        assert alice["metrics"]["prs_merged"] == 2
        assert out["team"]["commits"] is None

    def test_missing_github_file_nulls_every_github_metric(self):
        out = aggregate_people(_team(), None, _jira(), _usage())
        alice = _member(out, "alice")
        assert alice["metrics"]["prs_merged"] is None
        assert alice["metrics"]["tickets_resolved"] == 2
        assert out["sources"]["github"]["available"] is False

    def test_jira_project_failure_nulls_tickets(self):
        jira = _jira()
        jira["errors"] = [{"project": "ENG", "query": "resolved", "message": "401"}]
        jira["projects"]["ENG"]["unavailable"] = {"resolved": "401"}
        out = aggregate_people(_team(), _github(), jira, _usage())
        assert _member(out, "alice")["metrics"]["tickets_resolved"] is None


class TestUsageJoin:
    def test_usage_rows_join_by_usage_name(self):
        out = aggregate_people(_team(), _github(), _jira(), _usage())
        assert _member(out, "alice")["usage"][0]["weekly_pct"] == 80
        assert out["usage"]["unmatched"] == ["Zed"]

    def test_partial_readings_listed(self):
        out = aggregate_people(_team(), _github(), _jira(), _usage())
        partial = out["data_quality"]["partial_usage"]
        assert [(p["developer"], p["reading"]) for p in partial] == [("Bob", "post_reset")]

    def test_usage_unavailable_is_carried(self):
        out = aggregate_people(
            _team(), _github(), _jira(), {"available": False, "reason": "no email"}
        )
        assert out["usage"]["available"] is False
        assert out["usage"]["reason"] == "no email"


class TestTrend:
    def test_prior_weeks_included_per_metric(self):
        prior = aggregate_people(_team(), _github(), _jira(), _usage())
        older = copy.deepcopy(prior)
        _member(older, "alice")["metrics"]["prs_merged"] = 7
        out = aggregate_people(_team(), _github(), _jira(), _usage(), history=[None, older, prior])
        assert _member(out, "alice")["trend"]["prs_merged"] == [None, 7, 2]


@pytest.mark.parametrize("key", ["prs_merged", "commits", "tickets_resolved"])
def test_team_totals(key):
    out = aggregate_people(_team(), _github(), _jira(), _usage())
    assert out["team"][key] == sum(m["metrics"][key] or 0 for m in out["members"])


class TestTicketsShipped:
    """Tickets a person shipped = distinct ticket keys in their merged PR titles.

    JIRA assignee often moves to QA/PM before an issue closes, so the JIRA
    count alone under-credits the developer who did the work.
    """

    def test_distinct_project_keys_in_merged_pr_titles(self):
        gh = _github()
        prs = gh["repos"][REPO]["merged_prs"]
        prs[0]["title"] = "feat(ENG-7, ENG-8): two tickets"
        prs[1]["title"] = "fix(ENG-7): same ticket again"
        prs[4]["title"] = "OTHER-1 not a tracked project"
        out = aggregate_people(_team(), gh, _jira(), _usage())
        assert _member(out, "alice")["metrics"]["tickets_shipped"] == 2
        assert _member(out, "bob")["metrics"]["tickets_shipped"] == 0
        assert out["team"]["tickets_shipped"] == 2

    def test_unavailable_when_github_prs_unavailable(self):
        gh = _github()
        gh["repos"][REPO]["unavailable"] = {"prs": "HTTP 500"}
        out = aggregate_people(_team(), gh, _jira(), _usage())
        assert _member(out, "alice")["metrics"]["tickets_shipped"] is None
