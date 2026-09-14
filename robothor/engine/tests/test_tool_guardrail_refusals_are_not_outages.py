"""A guardrail refusal is the platform working, not the tool failing.

Live, 2026-09-14: "exec + write_file tool degradation warnings all day". In
the 24 h before this change exec had 159 successes, 29 guardrail refusals
(allowlist denials, control-character refusals, secret-file refusals) and 18
real failures; write_file 66 / 18 / 4. The degradation and outage detectors
counted the refusals as failures and paged about tools that were fine —
and taught the operator to read the pager as noise. Refusals are excluded
the same way sandbox denials already were.
"""

from __future__ import annotations

import pytest

from robothor.engine import detectors

pytestmark = pytest.mark.integration


def _events(cur, tool: str, n: int, *, success: bool, error_type: str | None) -> None:
    for _ in range(n):
        cur.execute(
            "INSERT INTO agent_tool_events (tool_name, success, error_type, created_at) "
            "VALUES (%s, %s, %s, NOW() - interval '30 minutes')",
            (tool, success, error_type),
        )


def test_refusals_do_not_make_a_healthy_tool_degraded(db_cursor, mock_get_connection) -> None:
    _events(db_cursor, "exec", 20, success=True, error_type=None)
    _events(db_cursor, "exec", 30, success=False, error_type="guardrail_blocked")
    flagged = {r["tool_name"] for r in detectors.check_tool_degradation(hours=24)}
    assert "exec" not in flagged


def test_real_failures_still_count(db_cursor, mock_get_connection) -> None:
    _events(db_cursor, "exec", 2, success=True, error_type=None)
    _events(db_cursor, "exec", 30, success=False, error_type="timeout")
    flagged = {r["tool_name"] for r in detectors.check_tool_degradation(hours=24)}
    assert "exec" in flagged


def test_refusals_do_not_make_a_tool_look_dead(db_cursor, mock_get_connection) -> None:
    _events(db_cursor, "write_file", 12, success=False, error_type="guardrail_blocked")
    flagged = {r["tool_name"] for r in detectors.check_tool_outage(min_calls=1, failure_ratio=0.0)}
    assert "write_file" not in flagged
