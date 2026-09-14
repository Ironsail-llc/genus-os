"""Degraded search pages the operator instead of waiting to be discovered.

Live, 2026-09-14: 29/29 web_search calls in a day were fallbacks and the
operator learned it by asking for a bakery and getting Wikipedia.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import search_health

pytestmark = pytest.mark.integration


def _run(cur) -> str:
    cur.execute(
        "INSERT INTO agent_runs (tenant_id, agent_id, trigger_type, status, started_at) "
        "VALUES ('default', 'main', 'telegram', 'completed', NOW() - interval '1 hour') RETURNING id"
    )
    row = cur.fetchone()
    return str(row["id"] if isinstance(row, dict) else row[0])


def _search_step(cur, run_id: str, n: int, output: dict) -> None:
    cur.execute(
        "INSERT INTO agent_run_steps (run_id, step_number, step_type, tool_name, tool_output, started_at) "
        "VALUES (%s, %s, 'tool_call', 'web_search', %s, NOW() - interval '30 minutes')",
        (run_id, n, json.dumps(output)),
    )


GOOD = {"provider": "brave", "count": 3, "results": [{"url": "https://example.com"}]}
FALLBACK = {"provider": "browser", "fallback_from": "searxng", "count": 2, "results": []}
FAILED = {"error": "Search failed (engines_unresponsive); browser fallback: browser_parse_empty"}


def test_mostly_fallback_search_is_reported(db_cursor, mock_get_connection, monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    run = _run(db_cursor)
    for i in range(4):
        _search_step(db_cursor, run, i, FALLBACK)
    _search_step(db_cursor, run, 9, FAILED)

    found = search_health.check_search_degradation(hours=6, min_calls=3, ratio=0.5)

    assert found is not None
    assert found["total"] == 5 and found["degraded"] == 5
    assert found["brave_key_set"] is False
    assert search_health._severity(found) == "critical"


def test_healthy_search_is_not_reported(db_cursor, mock_get_connection) -> None:
    run = _run(db_cursor)
    for i in range(5):
        _search_step(db_cursor, run, i, GOOD)
    assert search_health.check_search_degradation(hours=6, min_calls=3, ratio=0.5) is None


def test_too_few_calls_say_nothing(db_cursor, mock_get_connection) -> None:
    run = _run(db_cursor)
    _search_step(db_cursor, run, 1, FAILED)
    assert search_health.check_search_degradation(hours=6, min_calls=3, ratio=0.5) is None


def test_with_a_key_set_it_is_a_warning_naming_the_next_place_to_look(monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    found = {
        "total": 10,
        "degraded": 9,
        "share": 0.9,
        "via_brave": 1,
        "brave_key_set": True,
        "hours": 6,
    }
    assert search_health._severity(found) == "warning"
    assert "429" in search_health._body(found)


@pytest.mark.asyncio
async def test_the_detector_alerts_once_per_day_per_severity(monkeypatch) -> None:
    found = {
        "total": 10,
        "degraded": 10,
        "share": 1.0,
        "via_brave": 0,
        "brave_key_set": False,
        "hours": 6,
    }
    monkeypatch.setattr(search_health, "check_search_degradation", lambda: found)
    from robothor.engine import detectors

    detectors._dedup.clear()
    sent = AsyncMock(return_value=True)
    with patch("robothor.engine.alerts.alert_about_run", sent):
        first = await search_health.search_degradation_detector()
        second = await search_health.search_degradation_detector()

    assert (first, second) == (1, 0)
    level, title, body = sent.call_args_list[0].args[:3]
    assert level == "critical" and "degraded" in title.lower()
    assert "BRAVE_SEARCH_API_KEY is NOT set" in body
