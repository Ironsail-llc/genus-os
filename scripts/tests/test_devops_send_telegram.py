"""Tests for the deterministic Telegram digest of the devops report."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "devops_send_telegram.py"
_SPEC = importlib.util.spec_from_file_location("devops_send_telegram", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["devops_send_telegram"] = _MOD
_SPEC.loader.exec_module(_MOD)


def _ctx() -> dict:
    return {
        "period": {"week_start": "2026-05-11", "report_label": "Week of 2026-05-11", "tz": "x"},
        "team": {
            "prs_merged": 25,
            "commits": 80,
            "tickets_resolved": 10,
            "reviews_given": 18,
            "merged_without_review": 2,
        },
        "open_backlog": 0,
        "narrative": {
            "summary": "Solid week.",
            "bottlenecks": [{"text": "Reviews pile up", "severity": "high", "recommendation": ""}],
        },
        "data_quality": {},
    }


class TestFormatReport:
    def test_numbers_from_team_totals(self):
        out = _MOD.format_report(_ctx())
        assert "PRs merged: 25" in out
        assert "Commits: 80" in out
        assert "Tickets resolved: 10" in out
        assert "Open backlog: 0" in out
        assert "Reviews pile up" in out

    def test_period_label_not_repr(self):
        out = _MOD.format_report(_ctx())
        assert "Week of 2026-05-11" in out
        assert "'tz'" not in out

    def test_unavailable_metric_is_na_not_zero(self):
        ctx = _ctx()
        ctx["team"]["commits"] = None
        assert "Commits: n/a" in _MOD.format_report(ctx)

    def test_lists_or_ints_both_count(self):
        """The old crash: an analyst turned a list into an int (or back)."""
        ctx = _ctx()
        ctx["data_quality"] = {
            "outside_roster": 3,
            "no_activity": [{"name": "Bob"}],
            "unavailable_metrics": {"github.commits": "502"},
            "narrative_withheld": "Alice",
        }
        out = _MOD.format_report(ctx)
        assert "3 active handle(s) not on the roster" in out
        assert "1 roster member(s) with no activity" in out
        assert "1 metric(s) unavailable" in out

    def test_garbage_bottlenecks_do_not_crash(self):
        ctx = _ctx()
        ctx["narrative"]["bottlenecks"] = [5, "plain text", {"description": "d"}]
        out = _MOD.format_report(ctx)
        assert "plain text" in out


def test_main_reads_run_dir(tmp_path, monkeypatch):
    week = tmp_path / "2026-05-11"
    week.mkdir()
    (week / "report.json").write_text(json.dumps(_ctx()))
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "1")
    sent = []
    monkeypatch.setattr(
        _MOD, "send_telegram", lambda tok, chat, text: sent.append(text) or {"ok": True}
    )
    assert _MOD.main(["--week-start", "2026-05-11", "--runs-dir", str(tmp_path)]) == 0
    assert "PRs merged: 25" in sent[0]
