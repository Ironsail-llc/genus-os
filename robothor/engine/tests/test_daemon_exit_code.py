"""A subsystem crash must exit non-zero so systemd OnFailure pages.

The daemon's shutdown trigger is any top-level task completing
(FIRST_COMPLETED). When that task ended with an exception — e.g. the
Telegram polling task dying on a persistent network failure — the engine
previously still exited 0, so ``Restart=always`` silently crash-looped
it forever and the OnFailure pager never fired. ``_log_task_results``
already knows the outcome; it now reports it, and ``run()`` threads it
into the process exit code. A normal shutdown stays exit 0.
"""

from __future__ import annotations

import asyncio
import signal

import pytest

from robothor.engine import daemon


async def _ok() -> str:
    return "done"


async def _boom() -> None:
    raise RuntimeError("subsystem crashed")


class TestLogTaskResults:
    @pytest.mark.asyncio
    async def test_reports_failure_when_a_task_raised(self):
        task = asyncio.create_task(_boom(), name="telegram")
        done, _ = await asyncio.wait({task})

        assert daemon._log_task_results(done) is True

    @pytest.mark.asyncio
    async def test_reports_clean_when_tasks_completed_normally(self):
        task = asyncio.create_task(_ok(), name="telegram")
        done, _ = await asyncio.wait({task})

        assert daemon._log_task_results(done) is False

    @pytest.mark.asyncio
    async def test_cancelled_task_does_not_crash_the_check(self):
        """Cancelled tasks are logged and skipped — .exception() would raise."""
        task = asyncio.create_task(asyncio.sleep(30), name="watchdog")
        await asyncio.sleep(0)
        task.cancel()
        done, _ = await asyncio.wait({task})

        assert daemon._log_task_results(done) is False


class TestRunExitCode:
    def test_subsystem_failure_exits_nonzero(self, monkeypatch):
        async def fake_main() -> int:
            return 1

        monkeypatch.setattr(daemon, "main", fake_main)

        with pytest.raises(SystemExit) as excinfo:
            daemon.run()
        assert excinfo.value.code == 1

    def test_normal_shutdown_exits_zero(self, monkeypatch):
        async def fake_main() -> int:
            return 0

        monkeypatch.setattr(daemon, "main", fake_main)

        daemon.run()  # must not raise SystemExit

    def test_startup_crash_still_exits_one(self, monkeypatch):
        async def fake_main() -> int:
            raise RuntimeError("startup crashed")

        monkeypatch.setattr(daemon, "main", fake_main)

        with pytest.raises(SystemExit) as excinfo:
            daemon.run()
        assert excinfo.value.code == 1


class TestADeliberateStopExitsZero:
    """SIGTERM is how systemd stops this process; the daemon owns it.

    The daemon had no SIGTERM disposition of its own: the shutdown trigger was
    aiogram's polling task ending, because aiogram installs a SIGTERM handler
    while it polls. Outside that window — startup, a Telegram network backoff,
    an instance with no bot token at all — the default disposition killed the
    process with no drain at all. (That was never what paged: systemd files a
    MAIN process killed by SIGTERM as success. The 2026-09-17 pages were a
    CONTROL process, ExecStartPre=load-secrets.sh, killed when a second
    restart superseded the first one's start job — see
    tests/test_pager_hardening.py. This is shutdown hygiene, not the pager
    fix.)

    The handler only sets an event. The existing FIRST_COMPLETED wait turns
    that into the ordinary drain, and a task that completed without raising is
    a clean stop — so ``main`` returns 0 and ``run`` exits 0. A SECOND signal
    within ``STOP_SIGNAL_ECHO_WINDOW_SECONDS`` of the first is the same stop
    echoing through the process and is ignored; one later than that during
    the drain is a human insisting, and forces the exit (0 for SIGTERM, 130
    for SIGINT) instead of being swallowed by an already-set event.
    """

    @pytest.mark.asyncio
    async def test_it_installs_a_handler_for_sigterm_and_sigint(self, monkeypatch):
        registered: dict[int, tuple] = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, callback, *args: registered.__setitem__(sig, (callback, args)),
        )

        daemon._install_shutdown_signals()

        assert set(registered) == {signal.SIGTERM, signal.SIGINT}

    @pytest.mark.asyncio
    async def test_the_handler_sets_the_stop_event(self, monkeypatch):
        """Driven exactly as the loop would drive it — no real signal is
        raised in the test process, where a missing handler would kill pytest
        rather than fail a test."""
        registered: dict[int, tuple] = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, callback, *args: registered.__setitem__(sig, (callback, args)),
        )

        stop = daemon._install_shutdown_signals()
        assert not stop.is_set()

        callback, args = registered[signal.SIGTERM]
        callback(*args)

        assert stop.is_set()

    @pytest.mark.asyncio
    async def test_a_platform_without_signal_handlers_still_starts(self, monkeypatch):
        """add_signal_handler is not implemented everywhere. The daemon comes
        up with no handler rather than failing to start."""
        loop = asyncio.get_running_loop()

        def unsupported(*_args, **_kwargs):
            raise NotImplementedError

        monkeypatch.setattr(loop, "add_signal_handler", unsupported)

        stop = daemon._install_shutdown_signals()

        assert isinstance(stop, asyncio.Event)
        assert not stop.is_set()

    @pytest.mark.asyncio
    async def test_a_signalled_shutdown_is_not_a_subsystem_crash(self):
        """The task the stop event completes is what ends the FIRST_COMPLETED
        wait. It must read as a clean stop — exit 0 — not as a crash."""
        stop = asyncio.Event()
        task = asyncio.create_task(stop.wait(), name="stop-signal")
        stop.set()
        done, _ = await asyncio.wait({task})

        assert daemon._log_task_results(done) is False

    @staticmethod
    def _armed(monkeypatch) -> tuple[dict[int, tuple], list[int], list[float]]:
        """Install the handlers against a fake loop and a fake clock.

        Returns the registered ``sig -> (callback, args)`` table, the list
        ``_force_exit`` appends to instead of ending pytest, and a one-slot
        list holding the fake monotonic time the stop state reads.
        """
        registered: dict[int, tuple] = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, callback, *args: registered.__setitem__(sig, (callback, args)),
        )
        exits: list[int] = []
        monkeypatch.setattr(daemon, "_force_exit", exits.append)
        now = [1000.0]
        daemon._install_shutdown_signals()
        for _callback, args in registered.values():
            args[0].clock = lambda: now[0]
        return registered, exits, now

    @pytest.mark.asyncio
    async def test_a_second_signal_inside_the_echo_window_is_the_same_stop(
        self, monkeypatch, caplog
    ):
        """2026-09-17, one deploy after the two-strikes handler shipped: systemd
        sent ONE SIGTERM, the daemon's handler saw it twice 138 ms apart,
        ``_force_exit(1)`` ran, and OnFailure paged. The second delivery was
        uvicorn re-raising the signal it had captured (test_daemon_signal_echo
        .py). A repeat this soon after the first is that stop echoing through
        the process, never a human insisting — humans do not repeat a kill
        inside two seconds. It is one info line and nothing else."""
        registered, exits, now = self._armed(monkeypatch)
        callback, args = registered[signal.SIGTERM]
        stop = args[0].event

        callback(*args)
        assert stop.is_set()
        assert exits == [], "the first signal is a clean stop, not an exit"

        now[0] += 0.138
        with caplog.at_level("INFO", logger=daemon.logger.name):
            callback(*args)
        assert exits == [], "an echo of the first signal must not end the process"
        assert stop.is_set()
        assert "exiting now" not in caplog.text
        assert not [r for r in caplog.records if r.levelname == "WARNING"], (
            "an echo is not worth a warning"
        )
        assert "echo" in caplog.text or "same stop" in caplog.text

    @pytest.mark.asyncio
    async def test_a_second_signal_just_inside_the_window_edge_is_still_an_echo(self, monkeypatch):
        registered, exits, now = self._armed(monkeypatch)
        callback, args = registered[signal.SIGTERM]
        callback(*args)
        now[0] += daemon.STOP_SIGNAL_ECHO_WINDOW_SECONDS - 0.01
        callback(*args)
        assert exits == []

    @pytest.mark.asyncio
    async def test_a_late_second_sigterm_forces_exit_zero_with_a_warning(self, monkeypatch, caplog):
        """Two strikes still exist for a human at a hung drain: a second
        SIGTERM AFTER the echo window ends the process now. But it exits 0 —
        an operator's deliberate second ``kill`` is not a service failure, and
        exit 1 under systemd is OnFailure and a page. The page is for crashes;
        a drain that outlives TimeoutStopSec is still SIGKILLed by systemd and
        still pages on its own."""
        registered, exits, now = self._armed(monkeypatch)
        callback, args = registered[signal.SIGTERM]
        callback(*args)

        now[0] += daemon.STOP_SIGNAL_ECHO_WINDOW_SECONDS + 0.5
        with caplog.at_level("WARNING", logger=daemon.logger.name):
            callback(*args)
        assert exits == [0], "a deliberate repeated SIGTERM ends the drain, exit 0"
        assert "exiting now" in caplog.text

    @pytest.mark.asyncio
    async def test_a_late_second_sigint_exits_130(self, monkeypatch):
        """128 + SIGINT, the code a shell reports for a Ctrl-C death — a shell
        convention, not a systemd one, so it stays."""
        registered, exits, now = self._armed(monkeypatch)
        callback, args = registered[signal.SIGINT]
        callback(*args)
        now[0] += daemon.STOP_SIGNAL_ECHO_WINDOW_SECONDS + 0.5
        callback(*args)

        assert exits == [130]

    @pytest.mark.asyncio
    async def test_the_echo_window_is_short_enough_to_be_an_echo_and_nothing_else(self):
        """Seconds, not tens of seconds: a human at a hung drain must not
        have to wait long for the second strike to count, and the budget the
        whole drain has is TimeoutStopSec=15."""
        assert 0.5 <= daemon.STOP_SIGNAL_ECHO_WINDOW_SECONDS <= 3.0

    def test_run_still_returns_quietly_on_keyboard_interrupt(self, monkeypatch):
        """With the handler installed a Ctrl-C never raises KeyboardInterrupt;
        the path in ``run`` stays for the two cases that still can: a Ctrl-C
        before the handler is installed, and a platform whose loop cannot
        install one. It must remain a quiet exit 0, not a crash log."""

        async def fake_main() -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(daemon, "main", fake_main)

        assert daemon.run() is None
