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
