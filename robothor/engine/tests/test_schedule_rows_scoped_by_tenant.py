"""The scheduler and warmup ask for THIS tenant's schedule row.

A job id names a row only together with a tenant. A caller that leaves the
tenant out reads ``DEFAULT_TENANT``'s row, or, on a connection no policy binds,
another tenant's: a tenant engine would then count its failures against the
root's breaker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.config import EngineConfig
from robothor.engine.models import AgentConfig, AgentRun, RunStatus
from robothor.engine.scheduler import CronScheduler
from robothor.engine.warmup import _build_history_section, _build_peer_section

TENANT = "test-tenant"


def _scheduler() -> CronScheduler:
    return CronScheduler(EngineConfig(tenant_id=TENANT), MagicMock())


def test_the_breaker_reads_this_tenants_row() -> None:
    with patch("robothor.engine.tracking.get_schedule", return_value=None) as read:
        _scheduler()._circuit_breaker_tripped(
            "alice-agent", AgentConfig(id="alice-agent", name="A")
        )
    read.assert_called_once_with("alice-agent", tenant_id=TENANT)


def test_a_timeout_is_counted_on_this_tenants_row() -> None:
    with (
        patch("robothor.engine.tracking.get_schedule", return_value=None) as read,
        patch("robothor.engine.scheduler.update_schedule_state") as write,
    ):
        _scheduler()._record_timeout("alice-agent")
    read.assert_called_once_with("alice-agent", tenant_id=TENANT)
    assert write.call_args.kwargs["tenant_id"] == TENANT


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "errors"), [(RunStatus.COMPLETED, 0), (RunStatus.FAILED, 3)])
async def test_a_finished_run_is_recorded_on_this_tenants_row(status, errors) -> None:
    """The main post-run write. Nothing else would notice it going astray: on a
    bound connection another tenant's row matches nothing and the write returns
    ``False``, so the breaker and catch-up would quietly stop for this tenant."""
    scheduler = _scheduler()
    agent = AgentConfig(id="alice-agent", name="A")
    run = AgentRun(agent_id="alice-agent", status=status, duration_ms=1)
    scheduler.runner = MagicMock(execute=AsyncMock(return_value=run))
    with (
        patch("robothor.engine.tracking.get_schedule", return_value={"consecutive_errors": 2}),
        patch("robothor.engine.scheduler.update_schedule_state") as write,
        patch("robothor.engine.warmup.build_warmth_preamble", return_value=""),
        patch("robothor.engine.scheduler.deliver", new_callable=AsyncMock),
    ):
        await scheduler._execute_and_deliver("alice-agent", "alice-agent", agent, "cron")
    assert write.call_args.kwargs["tenant_id"] == TENANT
    assert write.call_args.kwargs["consecutive_errors"] == errors


def test_catch_up_reads_this_tenants_row() -> None:
    with patch("robothor.engine.scheduler.get_schedule", return_value=None) as read:
        _scheduler()._catch_up_one(AgentConfig(id="alice-agent", name="A"))
    read.assert_called_once_with("alice-agent", tenant_id=TENANT)


def test_an_ambient_digest_is_recorded_on_this_tenants_row() -> None:
    result = SimpleNamespace(
        run_status="failed", started_at=datetime.now(UTC), run_id="r1", duration_ms=1
    )
    with (
        patch("robothor.engine.scheduler.get_schedule", return_value=None) as read,
        patch("robothor.engine.scheduler.update_schedule_state") as write,
    ):
        _scheduler()._record_ambient_state("alice-agent:ambient", result)
    read.assert_called_once_with("alice-agent:ambient", tenant_id=TENANT)
    assert write.call_args.kwargs["tenant_id"] == TENANT


def test_warmup_history_reads_this_tenants_row() -> None:
    with patch("robothor.engine.tracking.get_schedule", return_value=None) as read:
        _build_history_section("alice-agent", tenant_id=TENANT)
    read.assert_called_once_with("alice-agent", tenant_id=TENANT)


def test_warmup_peers_read_this_tenants_rows() -> None:
    with patch("robothor.engine.tracking.get_schedule", return_value=None) as read:
        _build_peer_section(["bob-agent"], tenant_id=TENANT)
    read.assert_called_once_with("bob-agent", tenant_id=TENANT)
