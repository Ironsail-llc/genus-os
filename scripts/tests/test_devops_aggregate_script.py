"""End-to-end test for scripts/devops_aggregate_people.py (run-dir in, people.json out)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

_SCRIPT = Path(__file__).resolve().parent.parent / "devops_aggregate_people.py"


def _load():
    spec = importlib.util.spec_from_file_location("devops_aggregate_people_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["devops_aggregate_people_script"] = mod
    spec.loader.exec_module(mod)
    return mod


TEAM = {
    "roles": {"eng": {"title": "Engineer", "focus": "Ships"}},
    "members": [{"id": "alice", "name": "Alice Example", "role": "eng", "github": ["alice-gh"]}],
}


def test_writes_people_json_with_trend(tmp_path: Path):
    team = tmp_path / "team.yaml"
    team.write_text(yaml.safe_dump(TEAM))
    runs = tmp_path / "runs"
    week = runs / "2026-05-11"
    week.mkdir(parents=True)
    (week / "github.json").write_text(
        json.dumps(
            {
                "complete": True,
                "errors": [],
                "repos": {
                    "acme/w": {
                        "unavailable": {},
                        "merged_prs": [{"number": 1, "author": "alice-gh", "reviewers": []}],
                        "reviews": [],
                        "commits": {"by_login": {}, "unlinked": {}},
                        "stale_prs": [],
                    }
                },
            }
        )
    )
    prior = runs / "2026-05-04"
    prior.mkdir()
    (prior / "people.json").write_text(
        json.dumps({"members": [{"id": "alice", "metrics": {"prs_merged": 4}}]})
    )

    rc = _load().main(
        ["--week-start", "2026-05-11", "--runs-dir", str(runs), "--team-file", str(team)]
    )
    assert rc == 0
    people = json.loads((week / "people.json").read_text())
    alice = people["members"][0]
    assert alice["metrics"]["prs_merged"] == 1
    assert alice["trend"]["prs_merged"] == [None, None, 4]
    # no jira.json -> tickets unavailable, not zero
    assert alice["metrics"]["tickets_resolved"] is None
    assert people["period"]["week_start"] == "2026-05-11"
    assert (runs / "latest" / "people.json").exists()


def test_missing_team_file_fails(tmp_path: Path):
    rc = _load().main(
        [
            "--week-start",
            "2026-05-11",
            "--runs-dir",
            str(tmp_path),
            "--team-file",
            str(tmp_path / "nope.yaml"),
        ]
    )
    assert rc == 1
