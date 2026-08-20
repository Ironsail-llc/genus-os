"""DAL-level benchmark sandbox gate for session-goal creation.

create_session_goal writes an operator-facing crm_task (tags
``session_goal``/``thread``, requires_human), so a benchmark run reaching
it materializes fixture text as a real escalation. The DAL cannot see
ToolContext — the engine's tool dispatch sets a ContextVar for benchmark
calls, and create_session_goal refuses while it is active.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.crm import dal


def test_benchmark_sandbox_default_inactive() -> None:
    assert dal.benchmark_sandbox_active() is False


def test_create_session_goal_refuses_when_sandbox_active() -> None:
    token = dal.set_benchmark_sandbox(True)
    try:
        with pytest.raises(ValueError, match="benchmark sandbox"):
            dal.create_session_goal(
                objective="synthetic objective",
                success_criteria=["criterion"],
                agent_id="benchmark-agent",
            )
    finally:
        dal.reset_benchmark_sandbox(token)


def test_create_session_goal_proceeds_when_sandbox_inactive() -> None:
    """Without the sandbox, the guard must not trip: the function reaches the
    DB layer (proven by a sentinel error from the patched connection)."""

    class _SentinelError(RuntimeError):
        pass

    with (
        patch.object(dal, "get_connection", side_effect=_SentinelError("db reached")),
        pytest.raises(_SentinelError),
    ):
        dal.create_session_goal(
            objective="real objective",
            success_criteria=["criterion"],
            agent_id="main",
        )


def test_set_reset_roundtrip() -> None:
    token = dal.set_benchmark_sandbox(True)
    assert dal.benchmark_sandbox_active() is True
    dal.reset_benchmark_sandbox(token)
    assert dal.benchmark_sandbox_active() is False
