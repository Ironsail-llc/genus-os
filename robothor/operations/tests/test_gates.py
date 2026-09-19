"""Deployment exclusion covers work until its actual async completion."""

import asyncio

import pytest

from robothor.operations.tests.test_store import ops as ops


def test_exclusive_gate_refuses_active_readers_and_other_writers(ops):
    from robothor.operations.gates import gate
    from robothor.operations.store import Conflict, Operations

    with gate(ops, "example", shared=True):
        with gate(ops, "example", shared=True):
            with pytest.raises(Conflict):
                with gate(ops, "example"):
                    pytest.fail("writer entered")
        with gate(Operations(ops.tenant + "-other"), "example"):
            pass
    with gate(ops, "example"):
        with pytest.raises(Conflict):
            with gate(ops, "example", shared=True):
                pytest.fail("reader entered")
        with pytest.raises(Conflict):
            with gate(ops, "example"):
                pytest.fail("second writer entered")


@pytest.mark.asyncio
async def test_cancelled_caller_keeps_gate_until_work_has_really_finished(ops):
    from robothor.operations.gates import gate, run_shared
    from robothor.operations.store import Conflict

    started, finish = asyncio.Event(), asyncio.Event()

    async def work():
        started.set()
        await finish.wait()
        return "done"

    task = asyncio.create_task(run_shared(ops, "example", work))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    with pytest.raises(Conflict):
        with gate(ops, "example"):
            pytest.fail("cancel released still-active work")
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    with gate(ops, "example"):
        pass


@pytest.mark.asyncio
async def test_work_failure_releases_gate_and_remains_visible(ops):
    from robothor.operations.gates import gate, run_shared

    async def work():
        raise ValueError("work failed")

    with pytest.raises(ValueError, match="work failed"):
        await run_shared(ops, "example", work)
    with gate(ops, "example"):
        pass
