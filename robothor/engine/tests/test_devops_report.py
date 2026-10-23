"""Tests for the devops report: narrative guard + rendering from the aggregate only."""

from __future__ import annotations

import copy

from robothor.engine.reports.devops_aggregate import aggregate_people
from robothor.engine.reports.devops_report import build_report_context, check_narrative
from robothor.engine.reports.devops_team import parse_team
from robothor.engine.reports.renderer import render_devops_report

PERIOD = {"week_start": "2026-05-11", "report_label": "Week of 2026-05-11"}


def _people() -> dict:
    team = parse_team(
        {
            "roles": {
                "senior": {"title": "Senior Engineer", "focus": "Owns architecture"},
                "junior": {"title": "Junior Engineer", "focus": "Learning velocity"},
            },
            "members": [
                {
                    "id": "alice",
                    "name": "Alice Example",
                    "role": "senior",
                    "github": ["alice-gh"],
                    "jira": ["Alice Example"],
                    "usage_name": "Alice",
                },
                {"id": "bob", "name": "Bob Example", "role": "junior", "github": ["bob-gh"]},
            ],
        }
    )
    github = {
        "period": PERIOD,
        "complete": True,
        "errors": [],
        "repos": {
            "acme/w": {
                "unavailable": {},
                "merged_prs": [
                    {
                        "number": n,
                        "author": "alice-gh",
                        "reviewers": ["bob-gh"],
                        "lines_added": 10,
                        "lines_deleted": 1,
                    }
                    for n in range(1, 8)
                ],
                "reviews": [
                    {
                        "pr": 1,
                        "pr_author": "alice-gh",
                        "reviewer": "bob-gh",
                        "state": "APPROVED",
                        "turnaround_h": 3.0,
                    }
                ],
                "commits": {"by_login": {"alice-gh": 12, "bob-gh": 2}, "unlinked": {}},
                "stale_prs": [],
            }
        },
    }
    jira = {"period": PERIOD, "complete": True, "errors": [], "projects": {}}
    usage = {
        "available": True,
        "report_date": "2026-05-15",
        "rows": [
            {
                "developer": "Alice",
                "account": "Acct 1",
                "plan": "Max 20x",
                "weekly_pct": 64,
                "session_pct": 10,
                "fable_pct": None,
                "fable_limit_hit": False,
                "reading": "post_reset",
                "limit_status": "hit",
                "reset": "Wed 1:00 PM",
                "note": "Prior Limit Hit",
            },
        ],
    }
    out = aggregate_people(team, github, jira, usage)
    out["period"] = PERIOD
    return out


def _analyst() -> dict:
    return {
        "summary": "Steady week.",
        "per_person": {
            "alice": "Alice merged 7 PRs and made 12 commits; usage hit 64%.",
            "bob": "Bob gave 1 review.",
        },
        "bottlenecks": ["Review load sits on one person."],
        "si_vs_output": "Usage tracks output.",
    }


class TestGuard:
    def test_matching_numbers_pass(self):
        narrative, withheld = check_narrative(_people(), _analyst())
        assert withheld == []
        assert narrative["per_person"]["alice"].startswith("Alice merged 7")

    def test_mismatched_note_is_dropped(self):
        analyst = _analyst()
        analyst["per_person"]["alice"] = "Alice merged 9 PRs this week."
        narrative, withheld = check_narrative(_people(), analyst)
        assert "alice" not in narrative["per_person"]
        assert withheld == ["alice"]
        assert narrative["per_person"]["bob"] == "Bob gave 1 review."

    def test_wrong_percentage_is_dropped(self):
        analyst = _analyst()
        analyst["per_person"]["alice"] = "Usage at 99% this week."
        _, withheld = check_narrative(_people(), analyst)
        assert withheld == ["alice"]

    def test_number_for_unavailable_metric_is_dropped(self):
        people = _people()
        people["members"][0]["metrics"]["commits"] = None
        _, withheld = check_narrative(people, _analyst())
        assert "alice" in withheld

    def test_unknown_member_note_is_dropped(self):
        analyst = _analyst()
        analyst["per_person"]["mallory"] = "Mallory did 3 PRs."
        narrative, _ = check_narrative(_people(), analyst)
        assert "mallory" not in narrative["per_person"]

    def test_garbage_analyst_never_blocks(self):
        narrative, withheld = check_narrative(_people(), {"per_person": 5, "bottlenecks": 3})
        assert narrative["per_person"] == {}
        assert narrative["bottlenecks"] == []


class TestRender:
    def test_numbers_come_from_aggregate_not_analyst(self):
        analyst = _analyst()
        # An analyst trying to smuggle its own table and roles is ignored.
        analyst["people"] = [{"name": "Alice Example", "prs": 999, "role": "CTO"}]
        analyst["executive_summary"] = {"prs_merged": 4242}
        ctx = build_report_context(_people(), analyst)
        html = render_devops_report(ctx)["html"]
        assert "999" not in html
        assert "4242" not in html
        assert "CTO" not in html
        assert "Senior Engineer" in html
        assert "Junior Engineer" in html
        assert ">7<" in html  # alice's PRs from the aggregate

    def test_withheld_note_is_announced(self):
        analyst = _analyst()
        analyst["per_person"]["alice"] = "Alice merged 9 PRs."
        html = render_devops_report(build_report_context(_people(), analyst))["html"]
        assert "narrative withheld" in html.lower()
        assert "Alice merged 9 PRs" not in html

    def test_partial_usage_reading_is_labelled(self):
        html = render_devops_report(build_report_context(_people(), _analyst()))["html"]
        assert "post-reset" in html.lower()
        assert "partial" in html.lower()

    def test_unavailable_metric_shows_unavailable_not_zero(self):
        people = _people()
        for m in people["members"]:
            m["metrics"]["commits"] = None
            m["unavailable"]["commits"] = "acme/w: /commits 502"
        people["team"]["commits"] = None
        people["data_quality"]["unavailable_metrics"] = {"github.commits": "acme/w: /commits 502"}
        html = render_devops_report(build_report_context(people, _analyst()))["html"]
        assert "n/a" in html
        assert "/commits 502" in html

    def test_missing_analyst_still_renders(self):
        result = render_devops_report(build_report_context(_people(), None))
        assert "Alice Example" in result["html"]
        assert "narrative unavailable" in result["html"].lower()

    def test_subject_and_plain_summary(self):
        result = render_devops_report(build_report_context(_people(), _analyst()))
        assert result["subject"] == "Dev Team Operations Report — Week of 2026-05-11"
        assert "PRs merged: 7" in result["plain_summary"]

    def test_trend_arrow(self):
        people = _people()
        prior = copy.deepcopy(people["members"][0]["trend"])
        prior["prs_merged"] = [None, None, 3]
        people["members"][0]["trend"] = prior
        ctx = build_report_context(people, _analyst())
        alice = ctx["role_groups"][0]["members"][0]
        assert alice["arrows"]["prs_merged"] == "up"


def test_ticket_claim_may_match_shipped_or_jira_closed():
    from robothor.engine.reports.devops_report import _note_ok

    member = {"metrics": {"tickets_resolved": 2, "tickets_shipped": 5}}
    assert _note_ok("shipped 5 tickets", member)
    assert _note_ok("closed 2 tickets in JIRA", member)
    assert not _note_ok("shipped 7 tickets", member)
