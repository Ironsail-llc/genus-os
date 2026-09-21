"""Deep execution must honor a durable stop before invoking its worker."""

from unittest.mock import patch

import pytest

from robothor.engine.models import RunStatus
from robothor.engine.runner import AgentRunner


@pytest.mark.parametrize("stopped", [False, True])
async def test_deep_worker_admission_checks_its_scoped_run(engine_config, stopped):
    runner = AgentRunner(engine_config)
    with (
        patch("robothor.engine.runner.create_run"),
        patch.object(runner, "_finish_run", side_effect=lambda run: run),
        patch("robothor.engine.runtime.controls.stopped", return_value=stopped) as check,
        patch(
            "robothor.engine.rlm_tool.execute_deep_reason", return_value={"response": "Done"}
        ) as worker,
    ):
        run = await runner.execute_deep(
            "Synthetic analysis", tenant_id="tenant", user_id="operator", user_role="owner"
        )
    check.assert_called_once_with("tenant", run.id)
    if stopped:
        worker.assert_not_called()
        assert run.status == RunStatus.CANCELLED
        assert run.error_message == "Stopped as requested."
    else:
        worker.assert_called_once()
        assert run.status == RunStatus.COMPLETED


async def test_deep_worker_does_not_start_without_durable_run_record(engine_config):
    runner = AgentRunner(engine_config)
    with (
        patch(
            "robothor.engine.runner.create_run", side_effect=ConnectionError("Audit unavailable")
        ),
        patch.object(runner, "_finish_run", side_effect=lambda run: run),
        patch(
            "robothor.engine.rlm_tool.execute_deep_reason", return_value={"response": "Done"}
        ) as worker,
    ):
        run = await runner.execute_deep(
            "Synthetic analysis", tenant_id="tenant", user_id="operator", user_role="owner"
        )
    worker.assert_not_called()
    assert run.status == RunStatus.FAILED
    assert "no work was started" in run.error_message
