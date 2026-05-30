"""Tests for robothor.memory.working_context — live-state block refresher."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from robothor.memory import working_context as wc


def test_render_has_sections_and_excludes_nothing() -> None:
    text = wc._render(
        tasks=[{"status": "TODO", "title": "Fix OpenRouter login", "next_action": "confirm"}],
        facts=[{"category": "decision", "fact_text": "Paused email-briefing cron"}],
        intents=[{"title": "Grow Valhalla revenue"}],
    )
    assert "Open tasks (1)" in text
    assert "[TODO] Fix OpenRouter login → confirm" in text
    assert "(decision) Paused email-briefing cron" in text
    assert "Standing intents" in text and "Grow Valhalla revenue" in text
    assert "refreshed" in text


def test_render_empty_state() -> None:
    text = wc._render(tasks=[], facts=[], intents=[])
    assert "(none open)" in text
    assert "no recent high-importance facts" in text
    assert "Standing intents" not in text  # omitted when empty


def test_open_tasks_excludes_run_artifacts() -> None:
    cur = MagicMock()
    cur.fetchall.return_value = []
    wc._open_tasks(cur, "t1")
    sql = cur.execute.call_args.args[0]
    # the runner's per-run artifact tasks must be filtered out of the snapshot
    assert "cron run" in sql and "workflow run" in sql and "sub_agent run" in sql
    assert "status = ANY(%s)" in sql


def test_recent_facts_excludes_vision_and_low_importance() -> None:
    cur = MagicMock()
    cur.fetchall.return_value = []
    wc._recent_facts(cur, "t1")
    sql = cur.execute.call_args.args[0]
    assert "importance_score >= 0.6" in sql
    assert "camera" in sql and "in the image" in sql


def test_refresh_writes_block() -> None:
    cur = MagicMock()
    cur.fetchall.side_effect = [
        [{"status": "TODO", "title": "T", "next_action": "", "updated_at": None}],  # tasks
        [{"fact_text": "F", "category": "event"}],  # facts
        [],  # intents
    ]
    conn = MagicMock()
    conn.cursor.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = None
    with (
        patch("robothor.memory.working_context.get_connection", return_value=cm),
        patch("robothor.memory.working_context.write_block") as wb,
    ):
        stats = wc.refresh_working_context("t1")
    assert stats == {"tasks": 1, "facts": 1, "intents": 0}
    # wrote the working_context block with assembled content
    assert wb.call_args.args[0] == "working_context"
    assert "Open tasks (1)" in wb.call_args.args[1]
