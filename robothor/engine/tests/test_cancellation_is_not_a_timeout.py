"""An externally cancelled run is not a timeout, and a real timeout is not a deploy.

Two bugs share one branch at runner.py's cancel arm.

1. The label is derived from `agent_config.timeout_seconds`, which
   `_defaults.yaml` pins to 0 FLEET-WIDE. But the ceiling actually applied to
   the run is the effective one (the 3600s fleet ceiling when the agent
   declares 0). So a run that genuinely blew its wall-clock is labelled
   "Run cancelled externally", and `analytics.GENUINE_TIMEOUT_SQL` then
   EXCLUDES it from the timeout rate. Real timeouts are being reported as
   deploy artifacts -- the exact inverse of the bug analytics.py was written
   to fix.

2. Both outcomes write `status='timeout'`, so nothing downstream can tell them
   apart. That is not just a reporting problem: `resume.py` selects
   `WHERE status = 'running'`, and a GRACEFUL restart tombstones in-flight runs
   as `timeout` on the way down. Measured 2026-08-27 16:11:43 -- five runs
   tombstoned two seconds before the new daemon booted, three of them holding
   checkpoints, and resume recovered NONE of them. The feature only ever fired
   on SIGKILL. Giving external cancellation its own terminal status is what
   makes durability work for the restart case that actually happens.
"""

from __future__ import annotations

import pytest

from robothor.engine.analytics import EXTERNAL_CANCEL_PREFIX
from robothor.engine.models import RunStatus


class TestTheLabelComesFromEvidence:
    def test_a_run_that_blew_the_fleet_ceiling_is_not_called_a_cancellation(self):
        """timeout_seconds is 0 fleet-wide; the ceiling applied was 3600+."""
        from robothor.engine.cancel_outcome import _cancel_outcome

        outcome = _cancel_outcome(
            timed_out=True,
            declared_timeout_seconds=0,
            effective_ceiling=7200,
            last_activity="tool:list_tasks",
            waiting_on="",
        )
        assert outcome.status is RunStatus.TIMEOUT
        assert not outcome.reason.startswith(EXTERNAL_CANCEL_PREFIX)
        assert "7200" in outcome.reason  # the ceiling it actually blew
        assert "(0s)" not in outcome.reason  # not the declared value

    def test_an_external_cancel_records_the_cancelled_status(self):
        from robothor.engine.cancel_outcome import _cancel_outcome

        outcome = _cancel_outcome(
            timed_out=False,
            declared_timeout_seconds=0,
            effective_ceiling=7200,
            last_activity="session_started",
            waiting_on="",
        )
        assert outcome.status is RunStatus.CANCELLED
        assert outcome.reason.startswith(EXTERNAL_CANCEL_PREFIX)

    def test_a_cancel_during_an_in_flight_call_says_what_it_was_waiting_on(self):
        """Retires `last activity: session_started` as the whole diagnosis."""
        from robothor.engine.cancel_outcome import _cancel_outcome

        outcome = _cancel_outcome(
            timed_out=False,
            declared_timeout_seconds=0,
            effective_ceiling=7200,
            last_activity="session_started",
            waiting_on="llm_inflight:ollama_chat/qwen3.8:27b",
        )
        assert "ollama_chat/qwen3.8:27b" in outcome.reason

    def test_an_agents_own_positive_ceiling_is_still_named(self):
        from robothor.engine.cancel_outcome import _cancel_outcome

        outcome = _cancel_outcome(
            timed_out=True,
            declared_timeout_seconds=28800,
            effective_ceiling=28800,
            last_activity="tool:x",
            waiting_on="",
        )
        assert outcome.status is RunStatus.TIMEOUT
        assert "28800" in outcome.reason


class TestAnalyticsCountsBoth:
    def test_interrupted_matches_the_new_status_and_the_legacy_rows(self):
        from robothor.engine.analytics import INTERRUPTED_SQL

        assert "cancelled" in INTERRUPTED_SQL
        assert EXTERNAL_CANCEL_PREFIX in INTERRUPTED_SQL  # 30 days of history

    def test_genuine_timeout_excludes_both_forms(self):
        from robothor.engine.analytics import GENUINE_TIMEOUT_SQL

        assert "cancelled" in GENUINE_TIMEOUT_SQL
        assert EXTERNAL_CANCEL_PREFIX in GENUINE_TIMEOUT_SQL

    def test_the_runner_does_not_carry_its_own_copy_of_the_prefix(self):
        """This repo has been bitten three times by a second copy of a rule.

        Walks the AST rather than grepping: after this change the phrase still
        appears in runner.py's docstrings, explaining the history. A mention in
        prose is not a second copy of the rule -- the same distinction that
        made the first draft of the ttft guard pass on a comment.
        """
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "runner.py").read_text()
        tree = ast.parse(src)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        offenders = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and EXTERNAL_CANCEL_PREFIX in n.value
            and id(n) not in docstrings
        ]
        assert not offenders, f"runner.py re-hardcodes the prefix: {offenders}"
        # The rule lives in exactly one place, and it is not the runner.
        helper = (Path(__file__).resolve().parents[1] / "cancel_outcome.py").read_text()
        assert "EXTERNAL_CANCEL_PREFIX" in helper


class TestResumeSeesAGracefulRestart:
    def test_the_resume_scan_is_not_limited_to_running_rows(self):
        """The 16:11:43 finding: a graceful restart tombstones before resume runs."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "daemon.py").read_text()
        scan = src.split("Resume scan failed", 1)[0]
        assert "cancelled" in scan, (
            "resume still only selects status='running', so a graceful restart "
            "-- the common case -- leaves it nothing to recover"
        )

    @pytest.mark.parametrize("status", ["running", "cancelled"])
    def test_both_interrupted_states_are_resumable(self, status):
        from robothor.engine.resume import RESUMABLE_STATUSES

        assert status in RESUMABLE_STATUSES

    def test_a_genuine_timeout_is_not_resumed(self):
        """A run that blew its own ceiling asked for the ceiling, not a retry."""
        from robothor.engine.resume import RESUMABLE_STATUSES

        assert "timeout" not in RESUMABLE_STATUSES
        assert "failed" not in RESUMABLE_STATUSES
