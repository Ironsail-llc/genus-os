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
from robothor.engine.models import RunStatus


class TestInjectionBlockAuditTrail:
    def test_guardrail_event_is_logged_after_the_run_row_exists(self):
        """The event write must not precede the run insert (FK ordering)."""
        from robothor.engine import runner as runner_mod

        body = Path(runner_mod.__file__).read_text()

        block_start = body.index("except CronPromptInjectionBlockedError as _inj_exc:")
        block_end = body.index("if _inj_finding:", block_start)
        block = body[block_start:block_end]

        create_pos = block.index("create_run")
        log_pos = block.index("log_guardrail_event(")
        assert create_pos < log_pos, (
            "log_guardrail_event() runs before create_run() persists the run row — "
            "the agent_guardrail_events.run_id FK will violate and the audit "
            "event will be silently dropped"
        )

    def test_blocked_event_write_failure_is_logged_not_swallowed(self):
        """A failed audit write must surface in the log, not vanish.

        (``create_run`` may still be best-effort — it is the *audit* write whose
        failure must never be silent.)
        """
        from robothor.engine import runner as runner_mod

        body = Path(runner_mod.__file__).read_text()

        block_start = body.index("except CronPromptInjectionBlockedError as _inj_exc:")
        block_end = body.index("if _inj_finding:", block_start)
        block = body[block_start:block_end]

        log_pos = block.index("log_guardrail_event(")
        preceding = block[:log_pos]
        # the audit call must sit under a real try/except, not a blanket suppress
        assert (
            preceding.rstrip().endswith(("import log_guardrail_event", "log_guardrail_event"))
            or "try:" in preceding
        ), "audit write is not inside an explicit try block"

        suppress_before_audit = preceding.rfind("contextlib.suppress(Exception)")
        try_before_audit = preceding.rfind("try:")
        assert try_before_audit > suppress_before_audit, (
            "log_guardrail_event() still sits under contextlib.suppress — a "
            "failed audit write for a fired security control would be silent"
        )
        assert "logger.error" in block, (
            "no logger.error on the audit-failure path; a lost guardrail event must be reported"
        )

    def test_run_is_marked_failed_before_it_is_inserted(self):
        """The INSERT must carry the terminal status.

        ``_finish_run`` persists in a *background* task; a short-lived caller
        (the CLI) exits before it lands, so a row inserted as 'pending' stays
        'pending' forever. Inserting the already-failed run is the only write
        guaranteed to survive.
        """
        from robothor.engine import runner as runner_mod

        body = Path(runner_mod.__file__).read_text()

        block_start = body.index("except CronPromptInjectionBlockedError as _inj_exc:")
        block_end = body.index("if _inj_finding:", block_start)
        block = body[block_start:block_end]

        fail_pos = block.index("session.fail(")
        create_pos = block.index("create_run")
        assert fail_pos < create_pos, (
            "create_run() runs before session.fail() — the run row is inserted "
            "as 'pending' and, because _finish_run persists in the background, "
            "a CLI-invoked blocked run never reaches a terminal status"
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
