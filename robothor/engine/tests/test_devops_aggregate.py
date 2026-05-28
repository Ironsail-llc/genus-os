"""Tests for the deterministic per-person aggregation step of the devops report."""

from __future__ import annotations

from typing import Any

from robothor.engine.reports.devops_aggregate import aggregate_people


class FakeDAL:
    """In-memory stand-in for the CRM/identity database."""

    def __init__(
        self,
        identities: dict[tuple[str, str], str],
        roster: list[dict[str, Any]],
    ) -> None:
        self._idents = identities
        self._roster = roster

    def resolve_handles(self, items: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
        return {k: self._idents[k] for k in items if k in self._idents}

    def get_roster(self) -> list[dict[str, Any]]:
        return list(self._roster)


_PERIOD = {
    "tz": "America/New_York",
    "current_week_start_et": "2026-05-18T00:00:00-04:00",
    "last_week_start_et": "2026-05-11T00:00:00-04:00",
    "last_week_end_et": "2026-05-18T00:00:00-04:00",
    "generated_at_et": "2026-05-18T09:00:00-04:00",
}


def _empty_repo() -> dict[str, Any]:
    return {
        "pr_stats_current_week": {
            "authors": {},
            "merged_count": 0,
            "merged_by": {},
        },
        "pr_stats_last_week": {
            "authors": {},
            "merged_count": 0,
            "merged_by": {},
        },
    }


def _empty_project() -> dict[str, Any]:
    return {
        "resolved_current_week": {"count": 0, "by_assignee": {}, "by_type": {}},
        "resolved_last_week": {"count": 0, "by_assignee": {}, "by_type": {}},
        "in_progress": {"count": 0, "by_assignee": {}},
    }


class TestAggregatePeople:
    def test_merges_two_github_handles_into_one_person(self):
        github_data = {
            "period": _PERIOD,
            "repos": {
                "impetus-one": {
                    **_empty_repo(),
                    "pr_stats_current_week": {
                        "authors": {"alice-dev": 2, "alice-personal": 1},
                        "merged_count": 3,
                        "merged_by": {},
                    },
                    "pr_stats_last_week": {
                        "authors": {"alice-dev": 2},
                        "merged_count": 2,
                        "merged_by": {},
                    },
                }
            },
        }
        jira_data = {"period": _PERIOD, "projects": {}}
        dal = FakeDAL(
            identities={
                ("github", "alice-dev"): "p1",
                ("github", "alice-personal"): "p1",
            },
            roster=[
                {
                    "id": "p1",
                    "name": "Alice Developer",
                    "email": "alice@example.com",
                    "job_title": "Engineer",
                    "identities": [
                        ("github", "alice-dev"),
                        ("github", "alice-personal"),
                    ],
                }
            ],
        )
        result = aggregate_people(github_data, jira_data, dal)

        assert len(result["people"]) == 1
        alice = result["people"][0]
        assert alice["person_id"] == "p1"
        assert alice["current_week"]["prs_merged"] == 3
        assert alice["last_week"]["prs_merged"] == 2
        assert sorted(alice["github_handles"]) == ["alice-dev", "alice-personal"]
        assert "impetus-one" in alice["repos_touched"]
        assert result["unresolved_handles"] == []
        assert result["missing_from_roster"] == []

    def test_unresolved_github_handle_surfaces(self):
        github_data = {
            "period": _PERIOD,
            "repos": {
                "impetus-one": {
                    **_empty_repo(),
                    "pr_stats_current_week": {
                        "authors": {"newhire-dev": 2},
                        "merged_count": 2,
                        "merged_by": {},
                    },
                }
            },
        }
        jira_data = {"period": _PERIOD, "projects": {}}
        dal = FakeDAL(identities={}, roster=[])

        result = aggregate_people(github_data, jira_data, dal)

        # newhire-dev now appears as a provisional row, NOT dropped
        assert len(result["people"]) == 1
        prov = result["people"][0]
        assert prov["resolved"] is False
        assert prov["name"] == "newhire-dev"
        assert prov["current_week"]["prs_merged"] == 2

        # Still surfaced for CRM-linking
        assert len(result["unresolved_handles"]) == 1
        unresolved = result["unresolved_handles"][0]
        assert unresolved["channel"] == "github"
        assert unresolved["identifier"] == "newhire-dev"
        assert unresolved["occurrences"] == 2
        assert any(s.startswith("impetus-one/") for s in unresolved["sources"])

    def test_bot_authors_routed_to_bots_filtered_not_unresolved(self):
        github_data = {
            "period": _PERIOD,
            "repos": {
                "impetus-one": {
                    **_empty_repo(),
                    "pr_stats_current_week": {
                        "authors": {},
                        "merged_count": 0,
                        "merged_by": {},
                        "bot_authors": {"dependabot[bot]": 5, "renovate[bot]": 2},
                        "bot_merged_count": 7,
                    },
                }
            },
        }
        jira_data = {"period": _PERIOD, "projects": {}}
        dal = FakeDAL(identities={}, roster=[])

        result = aggregate_people(github_data, jira_data, dal)

        bots = {b["identifier"]: b for b in result["bots_filtered"]}
        assert bots["dependabot[bot]"]["merged_count"] == 5
        assert bots["renovate[bot]"]["merged_count"] == 2
        # Bots must NOT show up as unresolved (they're known non-people)
        assert result["unresolved_handles"] == []

    def test_roster_engineer_with_zero_activity_listed(self):
        github_data = {"period": _PERIOD, "repos": {}}
        jira_data = {"period": _PERIOD, "projects": {}}
        dal = FakeDAL(
            identities={},
            roster=[
                {
                    "id": "p2",
                    "name": "Bob Smith",
                    "email": "bob@example.com",
                    "job_title": "Engineer",
                    "identities": [("github", "bob-dev")],
                }
            ],
        )

        result = aggregate_people(github_data, jira_data, dal)

        assert result["people"] == []
        assert len(result["missing_from_roster"]) == 1
        assert result["missing_from_roster"][0]["name"] == "Bob Smith"
        assert result["missing_from_roster"][0]["person_id"] == "p2"

    def test_jira_resolved_attributed_to_person(self):
        github_data = {"period": _PERIOD, "repos": {}}
        jira_data = {
            "period": _PERIOD,
            "projects": {
                "I1": {
                    "resolved_current_week": {
                        "count": 2,
                        "by_assignee": {"Alice Developer": 2},
                        "by_type": {},
                    },
                    "resolved_last_week": {
                        "count": 1,
                        "by_assignee": {"Alice Developer": 1},
                        "by_type": {},
                    },
                    "in_progress": {
                        "count": 1,
                        "by_assignee": {"Alice Developer": 1},
                    },
                }
            },
        }
        dal = FakeDAL(
            identities={("jira_display_name", "Alice Developer"): "p1"},
            roster=[
                {
                    "id": "p1",
                    "name": "Alice Developer",
                    "email": "alice@example.com",
                    "job_title": "Engineer",
                    "identities": [("jira_display_name", "Alice Developer")],
                }
            ],
        )

        result = aggregate_people(github_data, jira_data, dal)

        assert len(result["people"]) == 1
        alice = result["people"][0]
        assert alice["current_week"]["tickets_resolved"] == 2
        assert alice["last_week"]["tickets_resolved"] == 1
        assert alice["in_progress_tickets"] == 1

    def test_reviewer_only_person_appears_with_reviews(self):
        github_data = {
            "period": _PERIOD,
            "repos": {
                "impetus-one": {
                    **_empty_repo(),
                    "review_stats": {
                        "prs_analyzed": 10,
                        "reviewers": [
                            {
                                "reviewer": "charlie-rev",
                                "reviews_given": 5,
                                "approvals": 4,
                                "changes_requested": 1,
                                "avg_turnaround_hours": 8.0,
                            }
                        ],
                    },
                }
            },
        }
        jira_data = {"period": _PERIOD, "projects": {}}
        dal = FakeDAL(
            identities={("github", "charlie-rev"): "p3"},
            roster=[
                {
                    "id": "p3",
                    "name": "Charlie Reviewer",
                    "email": "charlie@example.com",
                    "job_title": "Engineer",
                    "identities": [("github", "charlie-rev")],
                }
            ],
        )

        result = aggregate_people(github_data, jira_data, dal)

        assert len(result["people"]) == 1
        charlie = result["people"][0]
        assert charlie["reviews_30d"]["reviews_given"] == 5
        assert charlie["reviews_30d"]["approvals"] == 4

    def test_period_block_passes_through(self):
        github_data = {"period": _PERIOD, "repos": {}}
        jira_data = {"period": _PERIOD, "projects": {}}
        dal = FakeDAL(identities={}, roster=[])

        result = aggregate_people(github_data, jira_data, dal)

        assert result["period"] == _PERIOD

    def test_unresolved_handle_appears_as_provisional_person(self):
        """Activity from unresolved handles must still appear in `people`,
        flagged with resolved=False. Linking later just upgrades the row."""
        github_data = {
            "period": _PERIOD,
            "repos": {
                "storefront": {
                    **_empty_repo(),
                    "pr_stats_current_week": {
                        "authors": {"slivas": 7},
                        "merged_count": 7,
                        "merged_by": {},
                    },
                    "pr_stats_last_week": {
                        "authors": {"slivas": 5},
                        "merged_count": 5,
                        "merged_by": {},
                    },
                }
            },
        }
        jira_data = {"period": _PERIOD, "projects": {}}
        dal = FakeDAL(identities={}, roster=[])

        result = aggregate_people(github_data, jira_data, dal)

        # slivas appears in people with resolved=False
        assert len(result["people"]) == 1
        row = result["people"][0]
        assert row["resolved"] is False
        assert row["person_id"] is None
        assert row["name"] == "slivas"
        assert row["github_handles"] == ["slivas"]
        assert row["current_week"]["prs_merged"] == 7
        assert row["last_week"]["prs_merged"] == 5
        assert "storefront" in row["repos_touched"]

        # Still surfaced in unresolved_handles so operator can link
        assert any(u["identifier"] == "slivas" for u in result["unresolved_handles"])

        # Totals INCLUDE provisional activity
        assert result["totals"]["current_week"]["prs_merged_human"] == 7
        assert result["totals"]["last_week"]["prs_merged_human"] == 5

    def test_resolved_people_marked_resolved_true(self):
        github_data = {
            "period": _PERIOD,
            "repos": {
                "repo-a": {
                    **_empty_repo(),
                    "pr_stats_current_week": {
                        "authors": {"alice-dev": 3},
                        "merged_count": 3,
                        "merged_by": {},
                    },
                }
            },
        }
        jira_data = {"period": _PERIOD, "projects": {}}
        dal = FakeDAL(
            identities={("github", "alice-dev"): "p1"},
            roster=[
                {
                    "id": "p1",
                    "name": "Alice",
                    "email": "alice@example.com",
                    "job_title": "Engineer",
                    "identities": [("github", "alice-dev")],
                }
            ],
        )

        result = aggregate_people(github_data, jira_data, dal)
        assert len(result["people"]) == 1
        assert result["people"][0]["resolved"] is True
        assert result["people"][0]["person_id"] == "p1"

    def test_missing_from_roster_only_flags_engineers(self):
        """QA/PM with github handle but no PR activity must NOT be flagged
        as absent engineers — they aren't expected to merge PRs."""
        github_data = {"period": _PERIOD, "repos": {}}
        jira_data = {"period": _PERIOD, "projects": {}}
        dal = FakeDAL(
            identities={},
            roster=[
                {
                    "id": "p-eng",
                    "name": "Engineer Idle",
                    "email": "engineer@example.com",
                    "job_title": "Senior Developer",
                    "identities": [("github", "eng-idle")],
                },
                {
                    "id": "p-qa",
                    "name": "QA Person",
                    "email": "qa@example.com",
                    "job_title": "QA Engineer",
                    "identities": [("github", "qa-handle")],
                },
                {
                    "id": "p-pm",
                    "name": "PM Person",
                    "email": "pm@example.com",
                    "job_title": "Product Manager",
                    "identities": [("github", "pm-handle")],
                },
                {
                    "id": "p-rep",
                    "name": "Rep Person",
                    "email": "rep@example.com",
                    "job_title": "Representative",
                    "identities": [("github", "rep-handle")],
                },
            ],
        )

        # Default keywords match Developer/Engineer (case-insensitive)
        result = aggregate_people(
            github_data,
            jira_data,
            dal,
            engineer_role_keywords=["Developer", "Engineer"],
        )

        names = {m["name"] for m in result["missing_from_roster"]}
        # Real engineer → flagged
        assert "Engineer Idle" in names
        # "QA Engineer" matches "Engineer" → flagged as expected
        assert "QA Person" in names
        # PM and Representative → NOT flagged
        assert "PM Person" not in names
        assert "Rep Person" not in names

    def test_missing_from_roster_strict_keyword_match(self):
        """With strict keyword set, QA shouldn't get caught by 'Engineer'."""
        github_data = {"period": _PERIOD, "repos": {}}
        jira_data = {"period": _PERIOD, "projects": {}}
        dal = FakeDAL(
            identities={},
            roster=[
                {
                    "id": "p-qa",
                    "name": "QA Person",
                    "email": "qa@example.com",
                    "job_title": "QA",
                    "identities": [("github", "qa-handle")],
                },
                {
                    "id": "p-eng",
                    "name": "Engineer Idle",
                    "email": "engineer@example.com",
                    "job_title": "Senior Developer",
                    "identities": [("github", "eng-idle")],
                },
            ],
        )
        result = aggregate_people(
            github_data,
            jira_data,
            dal,
            engineer_role_keywords=["Developer"],
        )
        names = {m["name"] for m in result["missing_from_roster"]}
        assert "Engineer Idle" in names
        assert "QA Person" not in names

    def test_unassigned_jira_surfaces_as_provisional(self):
        """`Unassigned` JIRA sentinel is included so unassigned-ticket
        totals are visible, not silently dropped."""
        github_data = {"period": _PERIOD, "repos": {}}
        jira_data = {
            "period": _PERIOD,
            "projects": {
                "I1": {
                    "resolved_current_week": {
                        "count": 0,
                        "by_assignee": {},
                        "by_type": {},
                    },
                    "resolved_last_week": {
                        "count": 0,
                        "by_assignee": {},
                        "by_type": {},
                    },
                    "in_progress": {
                        "count": 15,
                        "by_assignee": {"Unassigned": 15},
                    },
                }
            },
        }
        dal = FakeDAL(identities={}, roster=[])
        result = aggregate_people(github_data, jira_data, dal)

        names = {p["name"] for p in result["people"]}
        assert "Unassigned" in names
        unassigned = next(p for p in result["people"] if p["name"] == "Unassigned")
        assert unassigned["resolved"] is False
        assert unassigned["in_progress_tickets"] == 15

    def test_totals_match_per_person_sum(self):
        """Sum of per-person PRs/tickets must equal the totals-minus-bots."""
        github_data = {
            "period": _PERIOD,
            "repos": {
                "impetus-one": {
                    **_empty_repo(),
                    "pr_stats_current_week": {
                        "authors": {"alice-dev": 3, "bob-dev": 2},
                        "merged_count": 5,
                        "merged_by": {},
                    },
                    "pr_stats_last_week": {
                        "authors": {"alice-dev": 1},
                        "merged_count": 1,
                        "merged_by": {},
                    },
                }
            },
        }
        jira_data = {"period": _PERIOD, "projects": {}}
        dal = FakeDAL(
            identities={
                ("github", "alice-dev"): "p1",
                ("github", "bob-dev"): "p2",
            },
            roster=[
                {
                    "id": "p1",
                    "name": "Alice",
                    "email": "alice@example.com",
                    "job_title": "Engineer",
                    "identities": [("github", "alice-dev")],
                },
                {
                    "id": "p2",
                    "name": "Bob",
                    "email": "bob@example.com",
                    "job_title": "Engineer",
                    "identities": [("github", "bob-dev")],
                },
            ],
        )

        result = aggregate_people(github_data, jira_data, dal)

        assert (
            sum(p["current_week"]["prs_merged"] for p in result["people"])
            == result["totals"]["current_week"]["prs_merged_human"]
        )
        assert result["totals"]["current_week"]["prs_merged_human"] == 5
