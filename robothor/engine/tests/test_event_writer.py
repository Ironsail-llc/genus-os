"""The single background writer for audit and guardrail event rows.

Rows are queued from the event loop and written, in submission order, by one
task that hops to a worker thread per batch. These tests pin the contract the
call sites rely on: order is kept, ``flush`` means "everything submitted before
this is written", nothing queued is lost on a normal shutdown, and with no
running loop a submit writes before it returns.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time

import pytest


def _writer():
    from robothor.engine.event_writer import EventWriter

    return EventWriter()


async def test_rows_are_written_in_submission_order():
    writer = _writer()
    written: list[int] = []

    for i in range(500):
        writer.submit(written.append, i)
    await writer.flush()

    assert written == list(range(500))
    await writer.shutdown()


async def test_flush_waits_for_everything_submitted_before_it():
    writer = _writer()
    written: list[int] = []

    def slow_append(i: int) -> None:
        time.sleep(0.01)
        written.append(i)

    for i in range(20):
        writer.submit(slow_append, i)
    await writer.flush()

    assert written == list(range(20))
    await writer.shutdown()


async def test_rows_are_written_off_the_loop_thread():
    writer = _writer()
    threads: list[int] = []

    writer.submit(lambda: threads.append(threading.get_ident()))
    await writer.flush()

    assert threads and threads[0] != threading.get_ident()
    await writer.shutdown()


async def test_submit_returns_without_waiting_for_the_write():
    writer = _writer()
    written: list[int] = []

    started = time.monotonic()
    writer.submit(lambda: (time.sleep(0.2), written.append(1)))
    assert time.monotonic() - started < 0.02
    assert written == []

    await writer.flush()
    assert written == [1]
    await writer.shutdown()


async def test_shutdown_writes_every_queued_row():
    writer = _writer()
    written: list[int] = []

    for i in range(200):
        writer.submit(written.append, i)
    await writer.shutdown()

    assert written == list(range(200))


async def test_a_submit_after_shutdown_writes_inline():
    writer = _writer()
    written: list[int] = []
    await writer.shutdown()

    writer.submit(written.append, 1)

    assert written == [1]


async def test_a_failing_row_does_not_stop_the_rows_behind_it():
    writer = _writer()
    written: list[int] = []

    def boom() -> None:
        raise RuntimeError("database went away")

    writer.submit(written.append, 1)
    writer.submit(boom)
    writer.submit(written.append, 2)
    await writer.flush()

    assert written == [1, 2]
    await writer.shutdown()


async def test_each_row_sees_the_context_it_was_submitted_in():
    writer = _writer()
    tenant: contextvars.ContextVar[str] = contextvars.ContextVar("tenant", default="none")
    seen: list[str] = []

    async def submit_as(name: str) -> None:
        tenant.set(name)
        writer.submit(lambda: seen.append(tenant.get()))

    await asyncio.gather(submit_as("tenant-a"), submit_as("tenant-b"))
    await writer.flush()

    assert seen == ["tenant-a", "tenant-b"]
    await writer.shutdown()


def test_without_a_running_loop_a_submit_writes_before_returning():
    writer = _writer()
    written: list[int] = []

    writer.submit(written.append, 1)

    assert written == [1]


def test_rows_left_on_a_finished_loop_are_written_by_the_next_one():
    """A loop that ends without a flush (a test, a CLI call) loses nothing."""
    writer = _writer()
    written: list[int] = []

    async def first() -> None:
        writer.submit(lambda: (time.sleep(0.05), written.append(1)))
        writer.submit(written.append, 2)

    asyncio.run(first())

    async def second() -> None:
        writer.submit(written.append, 3)
        await writer.flush()

    asyncio.run(second())
    assert written == [1, 2, 3]


def test_the_engine_has_one_shared_writer():
    from robothor.engine.event_writer import get_event_writer

    assert get_event_writer() is get_event_writer()


@pytest.mark.parametrize("count", [1, 250])
async def test_flush_on_an_idle_writer_returns(count):
    writer = _writer()
    for _ in range(count):
        await writer.flush()
    await writer.shutdown()
