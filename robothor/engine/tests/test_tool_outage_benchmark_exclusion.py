""" "Tool dead 13d: resolve_identities" (live, 2026-09-14): 8/8 calls failed over
7 days, 100%. Every one was a benchmark harness child looking up a fixture
person that does not exist in production; the tool had zero real calls in 30
days. Benchmark children (trigger_detail 'benchmark:…', or the sandbox
tenant) are the benchmark's business, not an outage to page the operator on.
"""

from __future__ import annotations

import pytest

from robothor.engine import detectors

# ``db_cursor`` / ``mock_get_connection`` come from the shared conftest, the
# same scratch database the other detector SQL tests run against.
pytestmark = pytest.mark.integration


def _run(cur, *, trigger_detail: str, tenant_id: str = "default") -> str:
    cur.execute(
        """
        INSERT INTO agent_runs (tenant_id, agent_id, trigger_type, trigger_detail, status, started_at)
        VALUES (%s, 'devops-analyst', 'sub_agent', %s, 'completed', NOW() - interval '2 hours')
        RETURNING id
        """,
        (tenant_id, trigger_detail),
    )
    row = cur.fetchone()  # the shared fixture hands out a dict cursor
    return str(row["id"] if isinstance(row, dict) else row[0])


def _failures(cur, tool: str, run_id: str, n: int) -> None:
    for _ in range(n):
        cur.execute(
            "INSERT INTO agent_tool_events (run_id, tool_name, success, error_type, created_at) "
            "VALUES (%s, %s, FALSE, 'unknown', NOW() - interval '1 hour')",
            (run_id, tool),
        )


def test_benchmark_children_are_excluded_but_real_failures_still_page(
    db_cursor, mock_get_connection
) -> None:
    bench = _run(db_cursor, trigger_detail="benchmark:devops-analyst-harness:honesty")
    _failures(db_cursor, "resolve_identities", bench, 8)
    sandbox = _run(db_cursor, trigger_detail="sub_agent", tenant_id="benchmark-sandbox")
    _failures(db_cursor, "search_people", sandbox, 8)
    real = _run(db_cursor, trigger_detail="sub_agent:main")
    _failures(db_cursor, "gws_gmail_get", real, 8)

    flagged = {r["tool_name"] for r in detectors.check_tool_outage(min_calls=1, failure_ratio=0.0)}

    assert "gws_gmail_get" in flagged
    assert "resolve_identities" not in flagged
    assert "search_people" not in flagged


def test_events_without_a_run_row_still_count(db_cursor, mock_get_connection) -> None:
    """The join is LEFT: an event with no run (older rows, CLI calls) is not dropped."""
    for _ in range(4):
        db_cursor.execute(
            "INSERT INTO agent_tool_events (tool_name, success, error_type, created_at) "
            "VALUES ('web_fetch', FALSE, 'timeout', NOW() - interval '1 hour')"
        )
    flagged = {r["tool_name"] for r in detectors.check_tool_outage(min_calls=1, failure_ratio=0.0)}
    assert "web_fetch" in flagged
