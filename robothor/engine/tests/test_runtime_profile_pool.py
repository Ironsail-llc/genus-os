"""Profile reads cannot exhaust the executor used by request controls/audits."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import UTC, datetime
from threading import Event, Lock

import pytest

from robothor.engine.runtime.contracts import ExecutionContext, RunRequest
from robothor.engine.runtime.profile_progress import read_with_progress


@pytest.mark.parametrize("tenants", [1, 2, 5, 20])
def test_blocked_profile_reads_leave_default_executor_available(tenants):
    tenant = ContextVar("test_profile_tenant")
    release, entered = Event(), Event()
    lock = Lock()
    count = 0

    def loader(*args):
        nonlocal count
        with lock:
            count += 1
            if count == min(tenants, 4):
                entered.set()
        assert release.wait(3), "fixture reads must be released"
        return tenant.get()

    async def scenario():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        tasks = []
        for name in [f"tenant-{i}" for i in range(tenants)]:
            token = tenant.set(name)
            try:
                request = RunRequest(ExecutionContext(name, "operator", name), "main", "Do work")
                tasks.append(
                    asyncio.create_task(
                        read_with_progress(request, datetime.now(UTC), loader, "fixture")
                    )
                )
            finally:
                tenant.reset(token)
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(0.005)
            # Durable controls and audit writes use this same default executor.
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "available"), 1) == "available"
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        assert results == [f"tenant-{i}" for i in range(tenants)]

    asyncio.run(scenario())


def test_cancelled_waiters_do_not_free_running_read_capacity():
    from robothor.engine.runtime.profile_pool import ProfilePool

    pool = ProfilePool(workers=1)
    entered, release, finished = Event(), Event(), Event()
    calls = []

    def loader(name):
        calls.append(name)
        entered.set()
        try:
            assert release.wait(3)
            return name
        finally:
            finished.set()

    async def scenario():
        first = asyncio.create_task(pool.read(loader, "first"))
        second = None
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(0.005)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            second = asyncio.create_task(pool.read(loader, "second"))
            await asyncio.sleep(0.1)
            assert calls == ["first"] and not second.done()
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        finally:
            release.set()
            if second and not second.done():
                second.cancel()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 1)
        assert calls == ["first"]

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        pool.shutdown()
