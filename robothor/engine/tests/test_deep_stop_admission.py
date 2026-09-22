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


async def test_deep_runner_calls_real_worker_with_progress_callback(
    engine_config, tmp_path, monkeypatch
):
    from unittest.mock import MagicMock

    from robothor.engine.tests.test_rlm_tool import _make_mock_result, _mock_rlm_modules

    monkeypatch.setenv("ROBOTHOR_RLM_LOG_DIR", str(tmp_path / "logs"))
    model = MagicMock()
    model.return_value.completion.return_value = _make_mock_result(
        "Synthetic deep result", cost=0.12
    )
    runner = AgentRunner(engine_config)
    with (
        patch("robothor.engine.runner.create_run"),
        patch.object(runner, "_finish_run", side_effect=lambda run: run),
        _mock_rlm_modules(model),
    ):
        run = await runner.execute_deep(
            "Synthetic analysis", tenant_id="tenant", user_id="operator", user_role="owner"
        )
    assert run.status == RunStatus.COMPLETED
    assert run.output_text == "Synthetic deep result"
    assert run.total_cost_usd == 0.12
    model.return_value.completion.assert_called_once()
