"""tool_success_rate counts a tool call that returned an error as a failure.

The whole path against a real database: the step a failed call leaves (its
``error_message`` read from the result by ``tool_outcome.error_text``) is
written by ``tracking.create_step`` and read back by ``get_agent_stats``. Two
calls, one of which answered ``{"error": ...}``, is a 50% success rate — not
the 100% an agent showed while three of its tools failed on every call.
"""

from __future__ import annotations

import uuid

import pytest

from robothor.engine.models import RunStep, StepType

pytestmark = pytest.mark.integration


def _seed_run(db_cursor, agent_id: str) -> str:
    run_id = str(uuid.uuid4())
    db_cursor.execute(
        """
        INSERT INTO agent_runs (id, tenant_id, agent_id, trigger_type, status, duration_ms)
        VALUES (%s, 'default', %s, 'cron', 'completed', 1000)
        """,
        (run_id, agent_id),
    )
    return run_id


def test_an_error_result_counts_as_a_failed_tool_call(db_cursor, db_conn, mock_get_connection):
    from robothor.engine.analytics import get_agent_stats
    from robothor.engine.tool_outcome import error_text
    from robothor.engine.tracking import create_step

    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    run_id = _seed_run(db_cursor, agent_id)

    results = [
        {"resolution_id": 7, "superseded_ids": []},
        {"error": "open_item and outcome are required"},
    ]
    for number, result in enumerate(results, start=1):
        create_step(
            RunStep(
                run_id=run_id,
                step_number=number,
                step_type=StepType.TOOL_CALL,
                tool_name="record_resolution",
                tool_input={},
                tool_output=result,
                error_message=error_text(result),
            )
        )

    stats = get_agent_stats(agent_id, days=1, tenant_id="default")

    assert stats["tool_success_rate"] == 0.5
