"""An enforce-mode injection block must leave an audit trail.

Regression test for a defect found while flipping ROBOTHOR_INJECTION_SCAN_MODE
to enforce on 2026-07-13: the block worked (the run was refused), but

  1. ``log_guardrail_event`` was called BEFORE ``create_run`` persisted the
     ``agent_runs`` row, so the ``agent_guardrail_events.run_id`` foreign key
     violated and the write was silently dropped by ``contextlib.suppress``;
  2. the run was left in ``pending`` instead of a terminal ``failed``.

Net effect: a security control fired in production and left no evidence —
the daily soak report showed zero enforce-mode blocks because none could be
recorded. Ordering is the contract under test here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from robothor.engine.cron_safety import CronPromptInjectionBlockedError
from robothor.engine.models import RunStatus, TriggerType


class TestInjectionBlockAuditTrail:
    """These were source greps over `execute`, because the ordering they check
    could not be observed from outside a 1,100-line method. It moved to
    `injection_screen`, where the calls can simply be watched — so they are
    behavioural now. The reasoning each one encodes is a live incident and is
    kept verbatim.
    """

    @staticmethod
    async def _block_a_run():
        """Drive a real block and return the call order plus the failed run."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from robothor.engine.cron_safety import CronPromptInjectionBlockedError
        from robothor.engine.injection_screen import screen_run_prompt

        order: list[str] = []
        failed = SimpleNamespace(id="blocked-1", status=RunStatus.FAILED)
        session = SimpleNamespace(
            run=SimpleNamespace(id="run-1"),
            fail=lambda reason: (order.append("fail"), failed)[1],
        )
        with (
            patch(
                "robothor.engine.cron_safety.screen_cron_prompt",
                side_effect=CronPromptInjectionBlockedError("ignore previous"),
            ),
            patch(
                "robothor.engine.tracking.create_run",
                side_effect=lambda r: order.append("insert"),
            ),
            patch(
                "robothor.engine.tracking.log_guardrail_event",
                side_effect=lambda **k: order.append("audit"),
            ),
        ):
            verdict = await screen_run_prompt(
                session,
                agent_id="crm-hygiene",
                trigger_type=TriggerType.CRON,
                system_prompt="SOUL",
                message="do the thing",
            )
        return order, verdict

    async def test_guardrail_event_is_logged_after_the_run_row_exists(self):
        """The event write must not precede the run insert (FK ordering).

        `agent_guardrail_events.run_id` is an FK to `agent_runs`. Logging first
        violates it and the audit event is silently dropped.
        """
        order, _ = await self._block_a_run()

        assert order.index("insert") < order.index("audit")

    async def test_run_is_marked_failed_before_it_is_inserted(self):
        """The INSERT must carry the terminal status.

        `_finish_run` persists in a *background* task; a short-lived caller
        (the CLI) exits before it lands, so a row inserted as 'pending' stays
        'pending' forever. Inserting the already-failed run is the only write
        guaranteed to survive.
        """
        order, verdict = await self._block_a_run()

        assert order.index("fail") < order.index("insert")
        assert verdict.blocked_run.status == RunStatus.FAILED

    async def test_blocked_event_write_failure_is_logged_not_swallowed(self, caplog):
        """A failed audit write must surface in the log, not vanish.

        (`create_run` may still be best-effort — it is the *audit* write whose
        failure must never be silent: a security control fired.)
        """
        import logging
        from types import SimpleNamespace
        from unittest.mock import patch

        from robothor.engine.cron_safety import CronPromptInjectionBlockedError
        from robothor.engine.injection_screen import screen_run_prompt

        session = SimpleNamespace(
            run=SimpleNamespace(id="run-1"),
            fail=lambda reason: SimpleNamespace(id="blocked-1", status=RunStatus.FAILED),
        )
        with (
            patch(
                "robothor.engine.cron_safety.screen_cron_prompt",
                side_effect=CronPromptInjectionBlockedError("ignore previous"),
            ),
            patch("robothor.engine.tracking.create_run"),
            patch(
                "robothor.engine.tracking.log_guardrail_event",
                side_effect=RuntimeError("db gone"),
            ),
            caplog.at_level(logging.ERROR),
        ):
            verdict = await screen_run_prompt(
                session,
                agent_id="crm-hygiene",
                trigger_type=TriggerType.CRON,
                system_prompt="SOUL",
                message="do the thing",
            )

        assert verdict.blocked is True, "a lost audit write must not unblock the run"
        assert any("could not be recorded" in r.getMessage() for r in caplog.records)

    def test_watchdog_is_stopped_before_the_injection_block_return(self):
        """The stall watchdog started for this run must not survive the block.

        watchdog.start(...) runs well before the try/finally (around
        `watchdog.stop()` + `_active_watchdog_var.reset(...)`) that normally
        tears it down. The injection-block path `return`s above that
        try/finally entirely, so on an inline cron fire the watchdog keeps
        monitoring whatever task happens to be `asyncio.current_task()` at
        that moment — the daemon's own long-running loop task — and cancels
        it ~150s later, taking the whole daemon down (Aug 5/9 crashes).
        """
        # Teardown deliberately stayed in the RUNNER when the screening logic
        # moved out: the watchdog token and _finish_run belong to it, and a
        # screening function has no business ending a run.
        from robothor.engine import runner as runner_mod

        body = Path(runner_mod.__file__).read_text()

        block_start = body.index("if _screen.blocked:")
        block_end = body.index("# ── Warmup phase instrumentation", block_start)
        block = body[block_start:block_end]

        return_pos = block.index("return self._finish_run(")
        stop_pos = block.index("watchdog.stop()")
        reset_pos = block.index("_active_watchdog_var.reset(")

        assert stop_pos < return_pos, (
            "watchdog.stop() must run before the injection-block early return — "
            "otherwise the watchdog started at the top of execute() is orphaned "
            "and cancels whatever task is current ~150s later"
        )
        assert reset_pos < return_pos, (
            "_active_watchdog_var.reset(...) must also run before the "
            "injection-block early return, or the contextvar leaks the stopped "
            "watchdog into whatever runs next on this task"
        )

    def test_session_fail_marks_run_failed_not_pending(self):
        """session.fail() must produce a terminal status for the blocked run."""
        from robothor.engine.session import AgentSession

        session = MagicMock(spec=AgentSession)
        run = MagicMock()
        run.status = RunStatus.PENDING
        session.run = run

        def _fail(msg, traceback=None):
            run.status = RunStatus.FAILED
            run.error_message = msg
            return run

        session.fail.side_effect = _fail

        result = session.fail(
            f"Blocked by injection scan: {CronPromptInjectionBlockedError('matched pattern: x')}"
        )
        assert result.status == RunStatus.FAILED
        assert "Blocked by injection scan" in result.error_message
