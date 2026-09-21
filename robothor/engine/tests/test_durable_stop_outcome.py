"""An operator stop is cancellation; other budget/provider failures remain failures."""

import pytest

from robothor.engine.models import RunStatus
from robothor.engine.request_budget import RequestBudgetError
from robothor.engine.runtime.failure import failed_or_stopped
from robothor.engine.runtime.provider_budget import DurableStopError
from robothor.engine.session import AgentSession


@pytest.mark.parametrize(
    "error,expected",
    [
        (DurableStopError("Durable stop denies another provider request"), RunStatus.CANCELLED),
        (RequestBudgetError("Insufficient token budget"), RunStatus.FAILED),
        (RequestBudgetError("Durable stop denies another provider request"), RunStatus.FAILED),
        (RuntimeError("Provider unavailable"), RunStatus.FAILED),
    ],
)
def test_typed_stop_preserves_operator_outcome(error, expected, caplog):
    session = AgentSession("synthetic")
    try:
        raise error
    except Exception as caught:
        run = failed_or_stopped(session, caught, "recorded traceback")
    assert run.status == expected
    assert run.error_message == (
        "Stopped as requested." if expected == RunStatus.CANCELLED else str(error)
    )
    assert run.completed_at is not None
    if expected == RunStatus.CANCELLED:
        assert "cancelled by durable stop" in caplog.text
        assert "Agent synthetic failed" not in caplog.text
    else:
        assert run.error_traceback == "recorded traceback"
