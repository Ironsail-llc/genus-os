"""Integration test: runner execute() → tracking → DB verification.

Verifies that agent runs are properly persisted to PostgreSQL with all
fields populated. Requires a real database connection.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from robothor.engine.models import AgentRun, RunStatus, TriggerType


@pytest.mark.integration
class TestRunnerDB:
    def test_create_run_persists_to_db(self, mock_get_connection) -> None:
        """create_run should insert a row that get_run can retrieve."""
        from robothor.engine.tracking import create_run, get_run

        run = AgentRun(
            id="test-run-001",
            tenant_id="test-tenant",
            agent_id="test-agent",
            trigger_type=TriggerType.CRON,
            trigger_detail="manual test",
            correlation_id="corr-001",
            status=RunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )

        run_id = create_run(run)
        assert run_id == "test-run-001"

        # Verify it was persisted
        row = get_run("test-run-001")
        assert row is not None
        assert row["agent_id"] == "test-agent"
        assert row["status"] == "running"

    def test_update_run_persists_changes(self, mock_get_connection) -> None:
        """update_run should modify the row in the DB."""
        from robothor.engine.tracking import create_run, get_run, update_run

        run = AgentRun(
            id="test-run-002",
            tenant_id="test-tenant",
            agent_id="test-agent",
            trigger_type=TriggerType.CRON,
            status=RunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        create_run(run)

        update_run(
            "test-run-002",
            status="completed",
            completed_at=datetime.now(UTC),
            duration_ms=5000,
            model_used="test-model",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.01,
        )

        row = get_run("test-run-002")
        assert row is not None
        assert row["status"] == "completed"
        assert row["duration_ms"] == 5000
        assert row["model_used"] == "test-model"
