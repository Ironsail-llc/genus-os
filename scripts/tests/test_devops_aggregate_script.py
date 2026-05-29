"""End-to-end smoke test for scripts/devops_aggregate_people.py.

Exercises the full read-input → aggregate → write-output path with the
Postgres DAL stubbed out, against fixture JSON that mirrors the
collectors' real schemas.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "devops_aggregate_people.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("devops_aggregate_people_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["devops_aggregate_people_script"] = mod
    spec.loader.exec_module(mod)
    return mod


PERIOD = {
    "tz": "America/New_York",
    "current_week_start_et": "2026-05-18T00:00:00-04:00",
    "last_week_start_et": "2026-05-11T00:00:00-04:00",
    "last_week_end_et": "2026-05-18T00:00:00-04:00",
    "generated_at_et": "2026-05-18T09:00:00-04:00",
}

GITHUB_FIXTURE = {
    "period": PERIOD,
    "repos": {
        "impetus-one": {
            "pr_stats_current_week": {
                "merged_count": 3,
                "authors": {"alice-dev": 2, "bob-dev": 1},
                "merged_by": {"alice-dev": 3},
                "bot_authors": {"dependabot[bot]": 4},
                "bot_merged_count": 4,
            },
            "pr_stats_last_week": {
                "merged_count": 2,
                "authors": {"alice-dev": 2},
                "merged_by": {"alice-dev": 2},
            },
            "review_stats": {
                "prs_analyzed": 5,
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
    "totals": {"merged": 3, "reviews": 5},
    "stale_prs": [],
    "errors": [],
}

JIRA_FIXTURE = {
    "period": PERIOD,
    "projects": {
        "I1": {
            "resolved_current_week": {
                "count": 2,
                "by_assignee": {"Alice Developer": 2},
                "by_type": {"Task": 2},
            },
            "resolved_last_week": {
                "count": 1,
                "by_assignee": {"Alice Developer": 1},
                "by_type": {"Bug": 1},
            },
            "in_progress": {"count": 1, "by_assignee": {"Alice Developer": 1}},
        }
    },
    "totals": {"resolved": 2, "stale": 0},
    "errors": [],
}


class FakeConn:
    """Stand-in for a psycopg2 connection used by PostgresAggregateDAL."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fixture_inputs(tmp_path, monkeypatch):
    """Write fixture JSONs at the script's expected /tmp paths via monkeypatch."""
    gh = tmp_path / "devops_github_data.json"
    jr = tmp_path / "devops_jira_data.json"
    out = tmp_path / "devops_people_rollup.json"
    gh.write_text(json.dumps(GITHUB_FIXTURE))
    jr.write_text(json.dumps(JIRA_FIXTURE))

    mod = _load_script_module()
    monkeypatch.setattr(mod, "GITHUB_IN", gh)
    monkeypatch.setattr(mod, "JIRA_IN", jr)
    monkeypatch.setattr(mod, "OUT", out)
    return mod, out


class TestAggregateScript:
    def test_full_pipeline_writes_expected_rollup(self, fixture_inputs):
        mod, out_path = fixture_inputs

        # Stub PostgresAggregateDAL with deterministic in-memory data
        from robothor.engine.tests.test_devops_aggregate import FakeDAL

        fake_dal = FakeDAL(
            identities={
                ("github", "alice-dev"): "p-alice",
                ("github", "bob-dev"): "p-bob",
                ("github", "charlie-rev"): "p-charlie",
                ("jira_display_name", "Alice Developer"): "p-alice",
            },
            roster=[
                {
                    "id": "p-alice",
                    "name": "Alice Developer",
                    "email": "alice@example.com",
                    "job_title": "Engineer",
                    "identities": [
                        ("github", "alice-dev"),
                        ("jira_display_name", "Alice Developer"),
                    ],
                },
                {
                    "id": "p-bob",
                    "name": "Bob Smith",
                    "email": "bob@example.com",
                    "job_title": "Engineer",
                    "identities": [("github", "bob-dev")],
                },
                {
                    "id": "p-charlie",
                    "name": "Charlie Reviewer",
                    "email": "charlie@example.com",
                    "job_title": "Engineer",
                    "identities": [("github", "charlie-rev")],
                },
                {
                    "id": "p-david",
                    "name": "David OnVacation",
                    "email": "david@example.com",
                    "job_title": "Engineer",
                    "identities": [("github", "david-vac")],
                },
            ],
        )

        # Replace the real DAL constructor inside the script's module ns
        def _fake_get_connection():
            return FakeConn()

        with (
            patch.object(mod, "PostgresAggregateDAL", lambda conn, tid: fake_dal),
            patch(
                "robothor.db.connection.get_connection",
                side_effect=_fake_get_connection,
            ),
        ):
            rc = mod.main()

        assert rc == 0
        rollup = json.loads(out_path.read_text())

        # Period passed through
        assert rollup["period"]["tz"] == "America/New_York"

        # 3 people active (Alice, Bob, Charlie); David on roster with zero activity
        people_by_id = {p["person_id"]: p for p in rollup["people"]}
        assert set(people_by_id) == {"p-alice", "p-bob", "p-charlie"}

        # Alice's GitHub + JIRA both attributed to her
        alice = people_by_id["p-alice"]
        assert alice["current_week"]["prs_merged"] == 2
        assert alice["last_week"]["prs_merged"] == 2
        assert alice["current_week"]["tickets_resolved"] == 2
        assert alice["last_week"]["tickets_resolved"] == 1
        assert alice["in_progress_tickets"] == 1

        # Charlie only has reviews → still appears
        charlie = people_by_id["p-charlie"]
        assert charlie["reviews_30d"]["reviews_given"] == 5

        # David on roster but no activity → in missing_from_roster
        missing_ids = {m["person_id"] for m in rollup["missing_from_roster"]}
        assert "p-david" in missing_ids

        # dependabot[bot] surfaced as bot, not unresolved
        bot_idents = {b["identifier"] for b in rollup["bots_filtered"]}
        assert "dependabot[bot]" in bot_idents
        assert all(u["identifier"] != "dependabot[bot]" for u in rollup["unresolved_handles"])

        # No unresolved handles in this fixture (every handle has a CRM mapping)
        assert rollup["unresolved_handles"] == []

        # Totals sum equals per-person sum
        sum_prs = sum(p["current_week"]["prs_merged"] for p in rollup["people"])
        assert rollup["totals"]["current_week"]["prs_merged_human"] == sum_prs

    def test_missing_input_returns_nonzero(self, tmp_path, monkeypatch):
        mod = _load_script_module()
        monkeypatch.setattr(mod, "GITHUB_IN", tmp_path / "does-not-exist.json")
        monkeypatch.setattr(mod, "JIRA_IN", tmp_path / "also-not.json")
        monkeypatch.setattr(mod, "OUT", tmp_path / "out.json")
        assert mod.main() != 0
