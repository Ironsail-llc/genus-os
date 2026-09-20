"""An instance that never enabled sales must not be reported unready.

``sales_runtime`` is registered in the engine's readiness map unconditionally
and ``health_contract.readiness_response`` turns any non-"ok" answer into a
503 for the WHOLE engine. So a lagging migration, an RLS problem on
``sales_settings`` or a bootstrap exception the scheduler only logged used to
take ``/ready`` down on instances that have no sales deployment at all — a
deploy-time outage caused by a disabled feature.

The second half of the same defect is cost: ``readiness()`` read the database
twice per poll, forever, on every instance, including the ones with nothing to
verify.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psycopg2
import pytest

from robothor.operations.store import Conflict


@pytest.fixture
async def runtime(tmp_path):
    """A NativeSalesRuntime whose only database is a counter we control."""
    from robothor.engine.sales_runtime import NativeSalesRuntime

    from apscheduler.schedulers.base import STATE_RUNNING

    engine = SimpleNamespace(config=SimpleNamespace(tenant_id="tenant-under-test"))
    apscheduler = Mock(state=STATE_RUNNING)
    apscheduler.get_jobs.return_value = []
    apscheduler.get_job.return_value = None
    scheduler = SimpleNamespace(workflow_engine=engine, scheduler=apscheduler)
    sales = SimpleNamespace(tenant="tenant-under-test", ops=Mock())
    built = NativeSalesRuntime(scheduler, sales, tmp_path, Mock())
    built.reads = []
    return built


def _clean():
    return {"config": {}, "revision": 0, "pending": None}


def _deployed():
    return {"config": {"fleet_release_id": "a" * 64}, "revision": 3, "pending": None}


def _counted(runtime, answer):
    """Install a `_state` that records every call instead of touching Postgres."""

    def state():
        runtime.reads.append(True)
        if isinstance(answer, BaseException):
            raise answer
        return answer() if callable(answer) else answer

    runtime._state = state
    return runtime


async def test_readiness_is_ok_when_sales_was_never_configured(runtime):
    """The clean-instance path: bootstrap found nothing, so there is nothing to verify."""
    _counted(runtime, _clean())

    await runtime.bootstrap()

    assert await runtime.readiness() == "ok"


async def test_an_unconfigured_instance_stops_polling_the_database(runtime):
    """Two round trips per poll, forever, on every instance that has no sales."""
    _counted(runtime, _clean())
    await runtime.bootstrap()
    runtime.reads.clear()

    for _ in range(5):
        assert await runtime.readiness() == "ok"

    assert runtime.reads == []


async def test_a_missing_sales_schema_is_not_an_unready_engine(runtime):
    """A lagging migration on an instance with no sales is not a deploy outage."""
    _counted(runtime, psycopg2.errors.UndefinedTable('relation "sales_settings" does not exist'))

    await runtime.bootstrap()

    assert await runtime.readiness() == "ok"


async def test_a_swallowed_bootstrap_failure_does_not_wedge_an_unconfigured_instance(runtime):
    """scheduler.start() logs a bootstrap exception and carries on; _booted stays false."""
    _counted(runtime, _clean())
    runtime._apply = Mock(side_effect=ValueError("Managed artifact unavailable"))

    with pytest.raises(ValueError):
        await runtime.bootstrap()
    assert runtime._booted is False

    assert await runtime.readiness() == "ok"


async def test_a_configured_instance_that_failed_to_boot_is_still_unready(runtime):
    """The check must keep its teeth where a sales release really was selected."""
    _counted(runtime, _deployed())
    runtime._apply = Mock(side_effect=ValueError("Managed artifact unavailable"))

    with pytest.raises(ValueError):
        await runtime.bootstrap()

    with pytest.raises(Conflict):
        await runtime.readiness()


async def test_readiness_cannot_decide_while_the_database_is_unreachable(runtime):
    """An unknown answer is not "ok"; it is the 503 the operator needs to see."""
    _counted(runtime, psycopg2.OperationalError("could not connect to server"))

    with pytest.raises(psycopg2.OperationalError):
        await runtime.bootstrap()
    with pytest.raises(psycopg2.OperationalError):
        await runtime.readiness()


async def test_a_deployed_release_still_re_reads_state_after_verification(runtime, monkeypatch):
    """The trailing read is a change-during-verification guard, not waste.

    An instance that HAS selected a release pays two round trips per poll, and
    should: the second read is what refuses a control transition that began
    while the asset checks were running. The saving this change makes is for
    instances with nothing selected, which now pay none — see the tests above.
    """
    answers = [_deployed(), _deployed(), {**_deployed(), "revision": 4}]
    _counted(runtime, lambda: answers[min(len(runtime.reads), len(answers)) - 1])
    runtime._apply = AsyncMock()
    await runtime.bootstrap()
    monkeypatch.setattr(runtime, "verify_admission", AsyncMock())

    with pytest.raises(Conflict):
        await runtime.readiness()

    assert len(runtime.reads) == 3  # one to boot, then the verified pair
