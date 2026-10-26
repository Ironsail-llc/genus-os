"""scripts/devops_render_report.py: aggregate numbers, guarded analyst prose, run-dir outputs."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "devops_render_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("devops_render_report_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["devops_render_report_script"] = mod
    spec.loader.exec_module(mod)
    return mod


PEOPLE = {
    "period": {"week_start": "2026-05-11", "report_label": "Week of 2026-05-11"},
    "roles": [{"key": "eng", "title": "Engineer", "focus": ""}],
    "members": [
        {
            "id": "alice",
            "name": "Alice Example",
            "role": "eng",
            "role_title": "Engineer",
            "metrics": {"prs_merged": 3, "commits": 5},
            "unavailable": {},
            "trend": {},
            "usage": [],
        }
    ],
    "team": {"prs_merged": 3, "commits": 5},
    "usage": {"available": False, "reason": "no email"},
    "data_quality": {},
}


def _run(tmp_path: Path, analyst: dict | str | None) -> tuple[int, Path]:
    week = tmp_path / "2026-05-11"
    week.mkdir(parents=True)
    (week / "people.json").write_text(json.dumps(PEOPLE))
    if analyst is not None:
        (week / "analyst.json").write_text(
            analyst if isinstance(analyst, str) else json.dumps(analyst)
        )
    rc = _load().main(["--week-start", "2026-05-11", "--runs-dir", str(tmp_path)])
    return rc, week


def test_renders_numbers_from_aggregate_even_if_analyst_differs(tmp_path):
    rc, week = _run(
        tmp_path,
        {
            "summary": "ok",
            "per_person": {"alice": "Alice merged 30 PRs."},
            "people": [{"name": "Alice Example", "prs": 30}],
        },
    )
    assert rc == 0
    html = (week / "report.html").read_text()
    assert "Alice merged 30 PRs" not in html
    assert ">3<" in html
    meta = json.loads((week / "meta.json").read_text())
    assert meta["subject"].endswith("Week of 2026-05-11")
    report = json.loads((week / "report.json").read_text())
    assert report["data_quality"]["narrative_withheld"] == ["Alice Example"]


def test_broken_analyst_json_does_not_block(tmp_path):
    rc, week = _run(tmp_path, "{not json")
    assert rc == 0
    assert "narrative unavailable" in (week / "report.html").read_text().lower()


def test_missing_people_fails(tmp_path):
    rc = _load().main(["--week-start", "2026-05-11", "--runs-dir", str(tmp_path)])
    assert rc == 1
