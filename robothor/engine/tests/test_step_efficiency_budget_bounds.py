"""No single tool call may eat the run, and the check-in has to actually fire.

Two more findings from the same profile (2026-09-15, `sam3_debug`, killed at its
1200s ceiling with nothing written):

* every one of its 41 ``exec`` calls asked for ``timeout: 900`` on a 1200s
  budget. Nothing stopped one of them from consuming the entire remaining run,
  and the engine's only ceiling was the tool's own ``MAX_EXEC_TIMEOUT`` — a
  constant that knows nothing about the run asking.
* the soft check-in fires at ``max_iterations``, which the bench agent sets to
  80. The run took 79 iterations, so it never fired once. A cadence that only
  triggers on runs longer than the ones that fail is not a cadence.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest


def _watchdog(elapsed: float, hard: float = 1200.0) -> Any:
    return SimpleNamespace(elapsed_seconds=elapsed, _hard_timeout=hard)


class TestTheClamp:
    def test_a_900s_request_with_242s_left_becomes_212s(self) -> None:
        from robothor.engine.run_pacing import clamp_tool_timeout

        effective, note = clamp_tool_timeout(900, watchdog=_watchdog(958.0), mode="enforce")
        assert effective == 212
        assert note == "timeout clamped to 212s: the run has 242s left"

    def test_a_request_that_already_fits_is_untouched(self) -> None:
        from robothor.engine.run_pacing import clamp_tool_timeout

        effective, note = clamp_tool_timeout(30, watchdog=_watchdog(100.0), mode="enforce")
        assert effective == 30
        assert note == ""

    def test_no_watchdog_means_no_ceiling_to_clamp_to(self) -> None:
        from robothor.engine.run_pacing import clamp_tool_timeout

        effective, note = clamp_tool_timeout(900, watchdog=None, mode="enforce")
        assert effective == 900
        assert note == ""

    def test_an_uncapped_run_is_untouched(self) -> None:
        """`timeout_seconds: 0` is the manifest sentinel for no ceiling."""
        from robothor.engine.run_pacing import clamp_tool_timeout

        effective, _ = clamp_tool_timeout(900, watchdog=_watchdog(100.0, hard=0.0), mode="enforce")
        assert effective == 900

    def test_the_floor_holds_when_the_run_is_nearly_over(self) -> None:
        """A 1s timeout turns every command into a failure; the point is to
        leave room for a write, not to guarantee a timeout."""
        from robothor.engine.run_pacing import MIN_TOOL_TIMEOUT_SECONDS, clamp_tool_timeout

        effective, note = clamp_tool_timeout(900, watchdog=_watchdog(1195.0), mode="enforce")
        assert effective == MIN_TOOL_TIMEOUT_SECONDS
        assert MIN_TOOL_TIMEOUT_SECONDS == 5
        assert note

    def test_an_expired_budget_still_gets_the_floor_not_a_negative(self) -> None:
        from robothor.engine.run_pacing import clamp_tool_timeout

        effective, _ = clamp_tool_timeout(900, watchdog=_watchdog(2000.0), mode="enforce")
        assert effective == 5

    def test_off_clamps_nothing(self) -> None:
        from robothor.engine.run_pacing import clamp_tool_timeout

        effective, note = clamp_tool_timeout(900, watchdog=_watchdog(958.0), mode="off")
        assert effective == 900
        assert note == ""

    def test_observe_leaves_the_timeout_alone_and_says_what_it_would_do(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from robothor.engine.run_pacing import clamp_tool_timeout

        caplog.set_level(logging.WARNING, logger="robothor.engine.run_pacing")
        effective, note = clamp_tool_timeout(
            900, watchdog=_watchdog(958.0), mode="observe", run_id="run-3"
        )
        assert effective == 900
        assert note == ""
        shadow = [r for r in caplog.records if "step-efficiency observe" in r.getMessage()]
        assert shadow and shadow[0].levelno == logging.WARNING
        assert "212" in shadow[0].getMessage()
        assert "run-3" in shadow[0].getMessage()


class TestExecUsesIt:
    def test_the_exec_handler_clamps(self) -> None:
        import pathlib

        import robothor.engine.tools.handlers.filesystem as m

        body = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        assert "clamp_tool_timeout(" in body

    def test_a_clamped_exec_says_so_in_its_result(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        import robothor.engine.feature_flags as ff
        from robothor.engine.stall_watchdog import _active_watchdog_var
        from robothor.engine.tools.handlers.filesystem import HANDLERS

        monkeypatch.setattr(ff, "step_efficiency_mode", lambda: "enforce")
        ctx = SimpleNamespace(workspace=str(tmp_path))
        token = _active_watchdog_var.set(_watchdog(958.0))
        try:
            result = asyncio.run(
                HANDLERS["exec"]({"command": "echo hi", "timeout": 900}, ctx)  # type: ignore[arg-type]
            )
        finally:
            _active_watchdog_var.reset(token)
        assert result["exit_code"] == 0
        assert "242s left" in result["timeout_note"]

    def test_an_unclamped_exec_carries_no_note(self, tmp_path: Any) -> None:
        import asyncio

        from robothor.engine.tools.handlers.filesystem import HANDLERS

        ctx = SimpleNamespace(workspace=str(tmp_path))
        result = asyncio.run(
            HANDLERS["exec"]({"command": "echo hi", "timeout": 10}, ctx)  # type: ignore[arg-type]
        )
        assert "timeout_note" not in result


class TestTheCheckIn:
    def test_iteration_zero_is_never_a_check_in(self) -> None:
        from robothor.engine.run_pacing import checkin_note

        assert checkin_note(0, checkin_interval=80, mode="enforce") is None

    def test_the_shipped_cadence_still_fires_on_every_rung(self) -> None:
        from robothor.engine.run_pacing import checkin_note

        for mode in ("off", "observe", "enforce"):
            note = checkin_note(80, checkin_interval=80, mode=mode)
            assert note is not None, mode
            assert "iteration 80" in note

    def test_enforce_adds_a_check_in_every_twenty_five(self) -> None:
        from robothor.engine.run_pacing import checkin_note

        for iteration in (25, 50, 75):
            note = checkin_note(iteration, checkin_interval=80, mode="enforce")
            assert note is not None, iteration
            assert f"iteration {iteration}" in note

    def test_it_asks_what_has_been_written_not_how_it_feels(self) -> None:
        """'Are you making progress' is a question an agent always answers yes
        to; 'what have you WRITTEN, and where' is checkable."""
        from robothor.engine.run_pacing import checkin_note

        note = checkin_note(25, checkin_interval=80, mode="enforce")
        assert note is not None
        assert "WRITTEN" in note
        assert "path" in note

    def test_nothing_between_the_cadences(self) -> None:
        from robothor.engine.run_pacing import checkin_note

        for iteration in (1, 24, 26, 49, 79):
            assert checkin_note(iteration, checkin_interval=80, mode="enforce") is None

    def test_off_keeps_only_the_shipped_cadence(self) -> None:
        from robothor.engine.run_pacing import checkin_note

        assert checkin_note(25, checkin_interval=80, mode="off") is None
        assert checkin_note(80, checkin_interval=80, mode="off") is not None

    def test_observe_logs_the_extra_check_in_at_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from robothor.engine.run_pacing import checkin_note

        caplog.set_level(logging.WARNING, logger="robothor.engine.run_pacing")
        assert checkin_note(25, checkin_interval=80, mode="observe", run_id="run-5") is None
        shadow = [r for r in caplog.records if "step-efficiency observe" in r.getMessage()]
        assert shadow and shadow[0].levelno == logging.WARNING
        assert "run-5" in shadow[0].getMessage()

    def test_a_disabled_interval_does_not_divide_by_zero(self) -> None:
        from robothor.engine.run_pacing import checkin_note

        assert checkin_note(40, checkin_interval=0, mode="off") is None

    def test_a_run_of_seventy_nine_iterations_now_gets_three_check_ins(self) -> None:
        """The measured run. Under the shipped cadence alone it got none."""
        from robothor.engine.run_pacing import checkin_note

        fired = [i for i in range(1, 80) if checkin_note(i, checkin_interval=80, mode="enforce")]
        assert fired == [25, 50, 75]
        assert not [i for i in range(1, 80) if checkin_note(i, checkin_interval=80, mode="off")]


class TestTheRunnerUsesIt:
    def test_the_runner_delegates_the_check_in(self) -> None:
        import pathlib

        import robothor.engine.runner as m

        body = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        assert "checkin_note(" in body
