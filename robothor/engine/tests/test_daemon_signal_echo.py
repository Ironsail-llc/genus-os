"""One SIGTERM must reach the daemon's handler once.

The 2026-09-17 page, one deploy after the two-strikes handler shipped
(``daemon._request_stop``): systemd logged a single ``Stopping``, the daemon
logged ``Received SIGTERM — stopping`` at .241 and ``Received SIGTERM again
during shutdown — second signal — exiting now`` at .379, exited 1, and
OnFailure paged. Nothing human sent a second signal, and nothing under
``robothor/`` re-signals its own process or group.

The source is in-process. ``loop.add_signal_handler`` dispatches through the
interpreter's wakeup fd; ``uvicorn.Server.serve`` (the health server, a task on
the same loop and the same main thread) then wraps itself in
``capture_signals()``, which installs its own ``signal.signal`` handler over
the top. One SIGTERM now does two things: the C-level handler writes to the
wakeup fd (the daemon's handler runs) AND runs uvicorn's Python-level
``handle_exit`` (uvicorn records the signal and begins its own shutdown). When
``serve()`` unwinds ~100 ms later, ``capture_signals`` restores the previous
handler and ``signal.raise_signal``s every signal it recorded — the same
SIGTERM, a second time, into the daemon's handler. Before #598 that second
delivery set an already-set event; after it, ``_force_exit(1)``.

The first test here is that reproduction, kept as a canary: if uvicorn ever
stops re-raising, it fails and the workaround can go. The rest pin the fix —
the health server runs on a ``uvicorn.Server`` whose ``capture_signals`` is
inert (``health._health_server_class``), so the daemon's disposition is the
process's only one, the way aiogram's ``handle_signals=False`` already made it
for Telegram polling.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import threading

import pytest

from robothor.engine import health

uvicorn = pytest.importorskip("uvicorn")

pytestmark = pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="signal dispositions can only be installed from the main thread",
)


async def _noop_asgi(scope, receive, send):  # pragma: no cover - never called
    pass


@contextlib.asynccontextmanager
async def _daemon_style_handler(sig: signal.Signals):
    """The daemon's disposition: a loop callback that only counts."""
    loop = asyncio.get_running_loop()
    hits: list[float] = []
    try:
        loop.add_signal_handler(sig, lambda: hits.append(loop.time()))
    except (NotImplementedError, RuntimeError) as exc:
        pytest.skip(f"this loop cannot install signal handlers: {exc}")
    try:
        yield hits
    finally:
        loop.remove_signal_handler(sig)


async def _serve_and_signal_once(server) -> list[float]:
    """Start ``server`` on the loop, send this process ONE SIGTERM once it is
    listening, let the server unwind, and return the daemon-style handler's
    invocation times."""
    async with _daemon_style_handler(signal.SIGTERM) as hits:
        task = asyncio.create_task(server.serve())
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        task.result()  # surface a bind failure as the error it is
                    await asyncio.sleep(0.01)
                os.kill(os.getpid(), signal.SIGTERM)
                # Stock uvicorn stops itself on the signal; the daemon's server
                # does not (the daemon cancels it at the end of the drain).
                # Either way the second delivery, if any, arrives once
                # serve() has unwound, so wait for that.
                await asyncio.sleep(0.3)
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await asyncio.sleep(0.2)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        return list(hits)


def _config():
    return uvicorn.Config(_noop_asgi, host="127.0.0.1", port=0, log_level="error")


@pytest.mark.asyncio
async def test_reproduction_stock_uvicorn_delivers_one_sigterm_twice():
    """The canary. Stock ``uvicorn.Server`` on the daemon's loop: one signal,
    two invocations of the daemon's handler — the second when ``serve()``
    unwinds and ``capture_signals`` re-raises what it captured. If this ever
    fails, uvicorn changed and the inert ``capture_signals`` can be
    reconsidered."""
    hits = await _serve_and_signal_once(uvicorn.Server(_config()))

    assert len(hits) == 2, (
        f"expected the echo (2 handler invocations), got {len(hits)} — has "
        "uvicorn stopped re-raising captured signals?"
    )


@pytest.mark.asyncio
async def test_the_health_server_delivers_one_sigterm_once():
    hits = await _serve_and_signal_once(health._health_server_class()(_config()))

    assert len(hits) == 1, f"the health server must not echo the stop signal, got {hits}"


@pytest.mark.asyncio
async def test_the_health_server_leaves_the_process_dispositions_alone():
    """The daemon owns SIGTERM/SIGINT. The health server must not install a
    handler over the top of it, even one it restores later."""
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    server = health._health_server_class()(_config())

    with server.capture_signals():
        during = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    assert during == before
    assert not server._captured_signals


def test_serve_health_uses_the_server_that_does_not_capture_signals():
    """A bare ``uvicorn.Server(...)`` in serve_health is the bug coming back."""
    import inspect

    src = inspect.getsource(health.serve_health)
    assert "_health_server_class()(" in src
    assert "uvicorn.Server(" not in src
    assert issubclass(health._health_server_class(), uvicorn.Server)
