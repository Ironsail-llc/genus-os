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
            "tickets_resolved": 10,
            "prs_merged": 25,
            "open_backlog": "120",
            "throughput_rate": "1.4 tickets/day, 3.6 PRs/day (7-day avg)",
            "last_week": {"tickets_resolved": 10, "prs_merged": 25},
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

    def test_executive_summary_uses_last_week_numbers(self):
        report = _base_report()
        out = _MOD.format_report(report)
        # Last week numbers appear as primary (10 tickets, 25 PRs)
        assert "10" in out
        assert "25" in out
        # Throughput rate appears
        assert "1.4 tickets/day" in out

    def test_no_current_week_wow_comparison(self):
        """The formatter should NOT show 'vs last week' current_week comparisons."""
        report = _base_report()
        out = _MOD.format_report(report)
        assert "vs" not in out.lower()


def _period_dict() -> dict:
    """A realistic period_block dict (what collectors actually stamp)."""
    return {
        "tz": "America/New_York",
        "current_week_start_et": "2026-05-18T00:00:00-04:00",
        "last_week_start_et": "2026-05-11T00:00:00-04:00",
        "last_week_end_et": "2026-05-18T00:00:00-04:00",
        "generated_at_et": "2026-05-18T09:00:00-04:00",
        "report_label": "Week of 2026-05-11",
    }


class TestPeriodHandling:
    def test_dict_period_renders_label_not_repr(self):
        report = _base_report()
        report["period"] = _period_dict()
        out = _MOD.format_report(report)
        assert "Week of 2026-05-11" in out
        assert "'tz'" not in out
        assert "America/New_York" not in out

    def test_dict_period_without_report_label_derives_label(self):
        report = _base_report()
        period = _period_dict()
        del period["report_label"]
        report["period"] = period
        out = _MOD.format_report(report)
        assert "Week of 2026-05-11" in out
        assert "'tz'" not in out


class TestOpenBacklog:
    def test_open_backlog_zero_is_shown(self):
        report = _base_report()
        report["executive_summary"]["open_backlog"] = 0
        out = _MOD.format_report(report)
        assert "Open backlog: 0" in out

    def test_open_backlog_empty_string_hidden(self):
        report = _base_report()
        report["executive_summary"]["open_backlog"] = ""
        out = _MOD.format_report(report)
        assert "Open backlog:" not in out


class TestDeadCodeRemoved:
    def test_pct_helper_removed(self):
        assert not hasattr(_MOD, "_pct")
