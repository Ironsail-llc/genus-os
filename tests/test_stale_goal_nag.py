"""Nothing should rot silently.

Six session goals sat in REVIEW for 2–5 weeks with nothing noticing — including
main's own "URGENT: auto-agent cron non-functional 22+ days" (which turned out
to be a false alarm about a deliberate pause). They blocked completion contracts
and quietly aged.

The daily guardrail-watch already nags on flags past their promotion deadline.
It must do the same for goals that have stopped moving: a goal in REVIEW past
its staleness window is either finished, wrong, or abandoned — and all three
deserve the operator's attention.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "guardrail_watch", REPO_ROOT / "scripts" / "guardrail_watch.py"
)
guardrail_watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(spec and guardrail_watch)


def test_detects_goals_stale_past_the_window():
    today = dt.date(2026, 7, 13)
    goals = [
        {"title": "old one", "agent": "main", "status": "REVIEW", "updated": dt.date(2026, 6, 1)},
        {"title": "fresh", "agent": "crm", "status": "REVIEW", "updated": dt.date(2026, 7, 12)},
    ]
    stale = guardrail_watch.stale_goals(goals, today=today, max_age_days=14)
    assert [g["title"] for g in stale] == ["old one"]


def test_nag_names_the_goal_its_agent_and_its_age():
    today = dt.date(2026, 7, 13)
    stale = [
        {
            "title": "URGENT: cron dead",
            "agent": "main",
            "status": "REVIEW",
            "updated": dt.date(2026, 6, 19),
        }
    ]
    msg = guardrail_watch.format_stale_goal_nag(stale, today=today)
    assert "URGENT: cron dead" in msg
    assert "main" in msg
    assert "24" in msg  # days stale


def test_silent_when_nothing_is_stale():
    assert guardrail_watch.format_stale_goal_nag([], today=dt.date(2026, 7, 13)) == ""
