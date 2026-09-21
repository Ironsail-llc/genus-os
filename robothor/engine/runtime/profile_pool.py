"""Keep blocked profile reads out of the executor used for durable controls."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import BoundedSemaphore, Lock


class ProfilePool:
    def __init__(self, workers=4):
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="profile-read")
        self._slots = BoundedSemaphore(workers)

    async def read(self, loader, *args):
        # Do not enqueue unlimited jobs behind a stuck filesystem read. Waiting
        # callers remain cancellable and consume no default-executor threads.
        while not self._slots.acquire(blocking=False):
            await asyncio.sleep(0.05)
        try:
            future = self._executor.submit(copy_context().run, loader, *args)
        except BaseException:
            self._slots.release()
            raise
        # Cancellation of the asyncio waiter must not release a running read's
        # capacity. The underlying concurrent future owns this release.
        future.add_done_callback(lambda _: self._slots.release())
        return await asyncio.wrap_future(future)

    def shutdown(self):
        self._executor.shutdown(wait=True, cancel_futures=True)


_pool = None
_lock = Lock()


def _after_fork():
    global _pool, _lock
    _pool, _lock = None, Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


async def read_profile(loader, *args):
    global _pool
    with _lock:
        if _pool is None:
            _pool = ProfilePool()
        pool = _pool
    return await pool.read(loader, *args)
