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
    during that drain is a human insisting, and forces the exit instead of
    being swallowed by an already-set event.
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

    @pytest.mark.asyncio
    async def test_a_second_signal_forces_exit_instead_of_being_swallowed(
        self, monkeypatch, caplog
    ):
        """Two strikes: the first signal starts the drain, the second one —
        a developer hammering Ctrl-C at a hung drain, or an operator's
        second `kill` — must end the process now, and say so, not set an
        already-set event and go on draining."""
        registered: dict[int, tuple] = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, callback, *args: registered.__setitem__(sig, (callback, args)),
        )
        exits: list[int] = []
        monkeypatch.setattr(daemon, "_force_exit", exits.append)

        stop = daemon._install_shutdown_signals()
        callback, args = registered[signal.SIGTERM]

        callback(*args)
        assert stop.is_set()
        assert exits == [], "the first signal is a clean stop, not an exit"

        with caplog.at_level("WARNING", logger=daemon.logger.name):
            callback(*args)
        assert exits == [1], "the second SIGTERM must force exit 1"
        assert "second signal" in caplog.text and "exiting now" in caplog.text

    @pytest.mark.asyncio
    async def test_a_second_sigint_exits_130(self, monkeypatch):
        """128 + SIGINT, the code a shell reports for a Ctrl-C death."""
        registered: dict[int, tuple] = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, callback, *args: registered.__setitem__(sig, (callback, args)),
        )
        exits: list[int] = []
        monkeypatch.setattr(daemon, "_force_exit", exits.append)

        daemon._install_shutdown_signals()
        callback, args = registered[signal.SIGINT]
        callback(*args)
        callback(*args)

        assert exits == [130]

    def test_run_still_returns_quietly_on_keyboard_interrupt(self, monkeypatch):
        """With the handler installed a Ctrl-C never raises KeyboardInterrupt;
        the path in ``run`` stays for the two cases that still can: a Ctrl-C
        before the handler is installed, and a platform whose loop cannot
        install one. It must remain a quiet exit 0, not a crash log."""

        async def fake_main() -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(daemon, "main", fake_main)

        assert daemon.run() is None
