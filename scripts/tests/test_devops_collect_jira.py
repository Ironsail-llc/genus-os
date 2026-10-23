"""Tests for scripts/devops_collect_jira.py — paginated, per-assignee, honest failures."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "devops_collect_jira.py"


def _load():
    spec = importlib.util.spec_from_file_location("devops_collect_jira_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["devops_collect_jira_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def _issue(n, assignee="Alice Example", itype="Story"):
    return {
        "key": f"ENG-{n}",
        "summary": f"Ticket {n}",
        "status": "Done",
        "assignee": assignee,
        "issue_type": itype,
        "resolved": "2026-05-12T10:00:00.000-0400",
    }


class FakeSearch:
    def __init__(self, by_kind: dict[str, list[dict]], fail: set[str] | None = None):
        self.by_kind = by_kind
        self.fail = fail or set()
        self.jqls: list[str] = []

    @staticmethod
    def kind(jql: str) -> str:
        if "resolved >=" in jql:
            return "resolved"
        if "In Progress" in jql:
            return "in_progress"
        if "updated <=" in jql:
            return "stale"
        return "open"

    async def __call__(self, args: dict, ctx) -> dict:
        jql = args["jql"]
        self.jqls.append(jql)
        k = self.kind(jql)
        if k in self.fail:
            return {"error": "JIRA authentication failed — check API token"}
        items = self.by_kind.get(k, [])
        page = int(args.get("next_page_token") or 0)
        size = args.get("max_results", 50)
        chunk = items[page * size : (page + 1) * size]
        last = (page + 1) * size >= len(items)
        return {
            "issues": chunk,
            "is_last": last,
            "next_page_token": None if last else str(page + 1),
        }


@pytest.mark.asyncio
async def test_resolved_paginates_past_100_and_keeps_types():
    mod = _load()
    fake = FakeSearch(
        {"resolved": [_issue(i, itype="Bug" if i % 2 else "Story") for i in range(250)]}
    )
    out = await mod.collect_jira(["ENG"], "2026-05-11", "2026-05-18", fake)
    resolved = out["projects"]["ENG"]["resolved"]
    assert len(resolved) == 250
    assert {r["type"] for r in resolved} == {"Bug", "Story"}
    assert out["complete"] is True
    assert any('resolved >= "2026-05-11" AND resolved < "2026-05-18"' in j for j in fake.jqls)


@pytest.mark.asyncio
async def test_failure_marks_metric_unavailable_and_records_error():
    mod = _load()
    fake = FakeSearch({"resolved": [_issue(1)]}, fail={"in_progress"})
    out = await mod.collect_jira(["ENG"], "2026-05-11", "2026-05-18", fake)
    proj = out["projects"]["ENG"]
    assert "in_progress" in proj["unavailable"]
    assert out["errors"][0]["query"] == "in_progress"
    assert out["complete"] is False


def test_main_writes_json_and_exits_nonzero_on_error(tmp_path, monkeypatch):
    mod = _load()
    fake = FakeSearch({}, fail={"resolved"})
    monkeypatch.setenv("DEVOPS_JIRA_PROJECTS", "ENG")
    monkeypatch.setattr(mod, "_jira_search", fake)
    rc = mod.main(["--week-start", "2026-05-11", "--runs-dir", str(tmp_path)])
    assert rc == 1
    data = json.loads((tmp_path / "2026-05-11" / "jira.json").read_text())
    assert data["errors"]
    assert data["period"]["week_start"] == "2026-05-11"


@pytest.mark.asyncio
async def test_done_statuses_count_as_delivered_and_leave_in_progress():
    """A workflow whose 'done' is a status (e.g. Deployed) rather than a resolution."""
    mod = _load()
    fake = FakeSearch({"resolved": [_issue(1)]})
    await mod.collect_jira(
        ["ENG"], "2026-05-11", "2026-05-18", fake, done_statuses=["Deployed", "Shipped"]
    )
    resolved_jql = next(j for j in fake.jqls if "resolved >=" in j)
    assert 'status CHANGED TO ("Deployed", "Shipped") DURING ("2026-05-11", "2026-05-18")' in (
        resolved_jql
    )
    # resolved later after being deployed in an earlier week is not counted twice
    assert 'NOT status WAS IN ("Deployed", "Shipped") BEFORE "2026-05-11"' in resolved_jql
    in_progress_jql = next(j for j in fake.jqls if "statusCategory" in j)
    assert 'status NOT IN ("Deployed", "Shipped")' in in_progress_jql
    open_jql = next(j for j in fake.jqls if "resolution = Unresolved" in j and "updated" not in j)
    assert 'status NOT IN ("Deployed", "Shipped")' in open_jql


@pytest.mark.asyncio
async def test_without_done_statuses_queries_are_resolution_only():
    mod = _load()
    fake = FakeSearch({})
    await mod.collect_jira(["ENG"], "2026-05-11", "2026-05-18", fake)
    assert not any("CHANGED TO" in j or "NOT IN" in j for j in fake.jqls)
