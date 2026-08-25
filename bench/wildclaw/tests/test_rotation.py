"""The nightly rotation — the benchmark as a standing practice, not a campaign.

Every number this project has believed about itself came from ad-hoc campaign
runs, and every campaign eventually ends. The rotation runs ONE category per
night against the same model, harness, and graders as the published OpenClaw
baseline, and appends the outcome to a ledger. Six nights is a full sweep;
a regression shows up within a week of being introduced instead of at the
next campaign.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from bench.wildclaw.rotation import (
    ledger_entry,
    pick_category,
    runnable_categories,
)


def _stage(root: Path, cats: dict[str, bool]) -> tuple[Path, Path]:
    """Build a fake repo + data tree. cats maps category -> has workspace data."""
    repo = root / "repo"
    data = root / "data"
    for cat, has_data in cats.items():
        d = repo / "tasks" / cat
        d.mkdir(parents=True)
        (d / f"{cat}_task_1_example.md").write_text("# t", encoding="utf-8")
        if has_data:
            (data / "workspace" / cat / "task_1_example").mkdir(parents=True)
    return repo, data


class TestRunnableCategories:
    def test_only_categories_with_staged_data_are_runnable(self, tmp_path):
        repo, data = _stage(
            tmp_path,
            {"01_Productivity_Flow": True, "05_Creative_Synthesis": False},
        )
        assert runnable_categories(repo, data) == ["01_Productivity_Flow"]

    def test_sorted_and_stable(self, tmp_path):
        repo, data = _stage(
            tmp_path,
            {"06_Safety_Alignment": True, "01_Productivity_Flow": True},
        )
        assert runnable_categories(repo, data) == [
            "01_Productivity_Flow",
            "06_Safety_Alignment",
        ]


class TestPickCategory:
    def test_rotates_through_all_categories(self):
        cats = ["a", "b", "c"]
        picked = [pick_category(day, cats) for day in range(6)]
        assert picked == ["a", "b", "c", "a", "b", "c"]

    def test_refuses_an_empty_roster(self):
        import pytest

        with pytest.raises(ValueError):
            pick_category(3, [])


class TestLedgerEntry:
    def _summary(self):
        return {
            "category": "04_Search_Retrieval",
            "tasks_attempted": 11,
            "tasks_graded": 11,
            "mean_score": 0.50,
            "total_cost_usd": 0.0,
            "tasks_without_workspace": 0,
            "results": [
                {"task_id": "t1", "score": 1.0, "harness_kill": False},
                {"task_id": "t2", "score": 0.0, "harness_kill": True},
            ],
        }

    def test_delta_against_the_published_baseline(self):
        baselines = {"04_Search_Retrieval": {"mean": 0.5636, "tasks": {}}}
        entry = ledger_entry(self._summary(), baselines, when="2026-08-25T04:15:00Z")
        assert entry["category"] == "04_Search_Retrieval"
        assert entry["mean"] == 0.50
        assert entry["baseline_mean"] == 0.5636
        assert entry["delta"] == round(0.50 - 0.5636, 4)
        assert entry["harness_kills"] == 1
        assert entry["when"] == "2026-08-25T04:15:00Z"

    def test_a_category_without_a_baseline_still_ledgers(self):
        entry = ledger_entry(self._summary(), {}, when="2026-08-25T04:15:00Z")
        assert entry["baseline_mean"] is None
        assert entry["delta"] is None

    def test_per_task_scores_survive_into_the_ledger(self):
        entry = ledger_entry(self._summary(), {}, when="2026-08-25T04:15:00Z")
        assert entry["per_task"] == {"t1": 1.0, "t2": 0.0}

    def test_the_entry_is_one_json_line(self):
        entry = ledger_entry(self._summary(), {}, when="2026-08-25T04:15:00Z")
        line = json.dumps(entry)
        assert "\n" not in line


class TestEnsurePod:
    """The pod does not survive a reboot; a nightly unit that pages every
    morning after one is decoration. The rotation provisions what it needs."""

    def _record(self, monkeypatch, pod_exists: bool):
        import subprocess as sp

        from bench.wildclaw import rotation

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[:3] == ["podman", "pod", "exists"]:
                return sp.CompletedProcess(cmd, 0 if pod_exists else 1)
            return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(rotation.subprocess, "run", fake_run)
        return rotation, calls

    def test_an_existing_pod_is_left_alone(self, monkeypatch):
        rotation, calls = self._record(monkeypatch, pod_exists=True)
        rotation.ensure_pod()
        assert not any(c[:3] == ["podman", "pod", "create"] for c in calls)

    def test_a_missing_pod_is_built_and_migrated(self, monkeypatch):
        rotation, calls = self._record(monkeypatch, pod_exists=False)
        rotation.ensure_pod()
        assert any(c[:3] == ["podman", "pod", "create"] for c in calls)
        started = " ".join(" ".join(c) for c in calls)
        assert "gb-pg" in started and "gb-redis" in started
        assert "robothor.cli migrate" in started, "the database must be migrated"


class TestEnvFallbacks:
    """The systemd unit passes no arguments — the render gate forbids ${...}
    in directives, so the paths ride WILDCLAW_* environment variables and the
    rotation resolves them itself."""

    def test_flags_fall_back_to_the_environment(self, monkeypatch, tmp_path):
        from bench.wildclaw.rotation import resolve_paths

        monkeypatch.setenv("WILDCLAW_REPO", str(tmp_path / "repo"))
        monkeypatch.setenv("WILDCLAW_DATA", str(tmp_path / "data"))
        monkeypatch.setenv("WILDCLAW_OUT", str(tmp_path / "out"))
        repo, data, out = resolve_paths(None, None, None)
        assert (repo, data, out) == (tmp_path / "repo", tmp_path / "data", tmp_path / "out")

    def test_explicit_flags_beat_the_environment(self, monkeypatch, tmp_path):
        from pathlib import Path

        from bench.wildclaw.rotation import resolve_paths

        monkeypatch.setenv("WILDCLAW_REPO", "/env/repo")
        repo, _, _ = resolve_paths(Path("/flag/repo"), Path("/d"), Path("/o"))
        assert repo == Path("/flag/repo")

    def test_missing_both_is_a_loud_error(self, monkeypatch):
        import pytest

        from bench.wildclaw.rotation import resolve_paths

        monkeypatch.delenv("WILDCLAW_REPO", raising=False)
        monkeypatch.delenv("WILDCLAW_DATA", raising=False)
        monkeypatch.delenv("WILDCLAW_OUT", raising=False)
        with pytest.raises(SystemExit):
            resolve_paths(None, None, None)
