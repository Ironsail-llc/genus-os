"""Tests for the deterministic Telegram formatter used by the devops pipeline."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "devops_send_telegram.py"
_SPEC = importlib.util.spec_from_file_location("devops_send_telegram", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["devops_send_telegram"] = _MOD
_SPEC.loader.exec_module(_MOD)


def _base_report() -> dict:
    return {
        "period": "Week of 2026-05-12",
        "executive_summary": {
            "current_week": {"tickets_resolved": 12, "prs_merged": 30},
            "last_week": {"tickets_resolved": 10, "prs_merged": 25},
            "open_backlog": "120",
        },
        "github": {"review_coverage": 45, "total_reviews": 18},
        "bottlenecks": [],
        "personnel_analysis": [],
        "jira": {"stale_tickets": []},
    }


class TestFormatReport:
    def test_data_quality_line_absent_when_clean(self):
        report = _base_report()
        out = _MOD.format_report(report)
        assert "Report quality" not in out

    def test_data_quality_line_shown_when_unresolved_handles(self):
        report = _base_report()
        report["data_quality"] = {
            "unresolved_handles": [
                {"channel": "github", "identifier": "newhire", "occurrences": 3}
            ],
            "missing_from_roster": [],
        }
        out = _MOD.format_report(report)
        assert "Report quality" in out
        assert "1 unresolved handle" in out

    def test_data_quality_line_shown_when_missing_roster(self):
        report = _base_report()
        report["data_quality"] = {
            "unresolved_handles": [],
            "missing_from_roster": [
                {"person_id": "p1", "name": "Bob Smith", "job_title": "Engineer"}
            ],
        }
        out = _MOD.format_report(report)
        assert "Report quality" in out
        assert "1 engineer(s) absent" in out

    def test_executive_summary_uses_nested_week_objects(self):
        report = _base_report()
        out = _MOD.format_report(report)
        # Sanity: current week numbers appear
        assert "12" in out
        assert "30" in out
