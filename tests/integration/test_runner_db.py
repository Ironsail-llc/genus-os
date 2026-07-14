"""Integration test: runner execute() → tracking → DB verification.

Verifies that agent runs are properly persisted to PostgreSQL with all
fields populated. Requires a real database connection.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from robothor.engine.models import AgentRun, RunStatus, TriggerType

_RUN_ONE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee1"
_RUN_TWO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee2"
_CORRELATION_ONE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeff"


@pytest.mark.integration
class TestRunnerDB:
    def test_create_run_persists_to_db(self, mock_get_connection) -> None:
        """create_run should insert a row that get_run can retrieve."""
        from robothor.engine.tracking import create_run, get_run

        run = AgentRun(
            id=_RUN_ONE,
            tenant_id="default",
            agent_id="test-agent",
            trigger_type=TriggerType.CRON,
            trigger_detail="manual test",
            correlation_id=_CORRELATION_ONE,
            status=RunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )

        run_id = create_run(run)
        assert run_id == _RUN_ONE

        # Verify it was persisted
        row = get_run(_RUN_ONE)
        assert row is not None
        assert row["agent_id"] == "test-agent"
        assert row["status"] == "running"

    def test_update_run_persists_changes(self, mock_get_connection) -> None:
        """update_run should modify the row in the DB."""
        from robothor.engine.tracking import create_run, get_run, update_run

        run = AgentRun(
            id=_RUN_TWO,
            tenant_id="default",
            agent_id="test-agent",
            trigger_type=TriggerType.CRON,
            status=RunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        create_run(run)

        update_run(
            _RUN_TWO,
            status="completed",
            completed_at=datetime.now(UTC),
            duration_ms=5000,
            model_used="test-model",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.01,
        )

        row = get_run(_RUN_TWO)
        assert row is not None
        assert row["status"] == "completed"
        assert row["duration_ms"] == 5000
        assert row["model_used"] == "test-model"
