"""`off` must be the engine that shipped, byte for byte.

The whole ladder rests on this. An operator who dislikes the step-efficiency
controls has to be able to get the previous engine back with a flag rather than
a revert, and a sweep that compares `off` against `enforce` is measuring the
controls only if `off` is the baseline it claims to be.

The first cut was not. `note_at` appended the pace sentence on every rung, so an
`off` run got a note main never produced; the deadline log line changed level
AND text on every rung; and a note was suppressed at iteration 0, which main
never did. Each is defensible as part of the ladder and indefensible inside
`off`.

Where a helper is main's own untouched code (`deliverables.deadline_note`,
`run_budget.deadline_warning`) the comparison here is genuinely differential —
it runs main's function beside the new one on the same inputs. Where the text
was inlined in `runner.py` and has now moved, it is pinned as a snapshot taken
from `origin/main` and named as such.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

#: Verbatim from `origin/main:robothor/engine/runner.py`, the `[SOFT CHECK-IN]`
#: block. Snapshot, not a paraphrase: if this string changes, `off` has changed.
MAIN_CHECKIN = (
    "[SYSTEM] Progress check-in (iteration {n}): "
    "Are you making progress toward the goal? If you are stuck "
    "in a loop or have completed the task, provide your final "
    "answer and stop calling tools. If making progress, continue."
)

#: Verbatim from `origin/main:robothor/engine/runner.py`, the `[DEADLINE]` block:
#: ``logger.info("Deadline warning issued at iteration %d", _iteration)``.
MAIN_LOG_TEMPLATE = "Deadline warning issued at iteration {n}"


def _watchdog(elapsed: float, hard: float = 1200.0) -> Any:
    return SimpleNamespace(elapsed_seconds=elapsed, _hard_timeout=hard)


class TestTheNoteIsMainsNote:
    def test_off_reproduces_mains_note_exactly_across_the_budget(self, tmp_path: Path) -> None:
        """Run main's own `deadline_note` beside the pacer on the same inputs."""
        from robothor.engine.deliverables import deadline_note
        from robothor.engine.run_pacing import DeadlinePacer

        (tmp_path / "there.md").write_text("x", encoding="utf-8")
        cases = [
            ("", None),
            (f"save it to {tmp_path}/results/out.md", tmp_path),
            (f"write to {tmp_path}/there.md", tmp_path),
        ]
        for task_text, workspace in cases:
            for elapsed in (0.0, 599.0, 960.0, 961.0, 1140.0, 1199.0):
                for iteration in (0, 1, 40):
                    pacer = DeadlinePacer(mode="off")
                    got = pacer.note_for(
                        _watchdog(elapsed),
                        iteration=iteration,
                        task_text=task_text,
                        workspace=workspace,
                    )
                    want = deadline_note(elapsed, 1200.0, task_text, workspace)
                    assert got == want, (elapsed, iteration, task_text)

    def test_off_appends_no_pace_sentence(self) -> None:
        from robothor.engine.run_pacing import DeadlinePacer

        note = DeadlinePacer(mode="off").note_for(
            _watchdog(960.0), iteration=40, task_text="", workspace=None
        )
        assert note is not None
        assert "more steps fit" not in note

    def test_off_still_warns_at_iteration_zero(self) -> None:
        """Main had no iteration guard. Suppression belongs to the ladder."""
        from robothor.engine.run_pacing import DeadlinePacer

        note = DeadlinePacer(mode="off").note_for(
            _watchdog(1100.0), iteration=0, task_text="", workspace=None
        )
        assert note is not None

    def test_the_ladder_does_suppress_iteration_zero(self) -> None:
        from robothor.engine.run_pacing import DeadlinePacer

        for mode in ("observe", "enforce"):
            assert (
                DeadlinePacer(mode=mode).note_for(
                    _watchdog(1100.0), iteration=0, task_text="", workspace=None
                )
                is None
            ), mode

    def test_off_logs_mains_line_at_mains_level_from_mains_logger(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Level, text AND logger name. A reader filtering the journal on
        `robothor.engine.runner` would otherwise watch the line vanish from an
        `off` box and read that as the control having been removed."""
        from robothor.engine.run_pacing import DeadlinePacer

        caplog.set_level(logging.INFO)
        DeadlinePacer(mode="off").note_for(
            _watchdog(960.0), iteration=40, task_text="", workspace=None, run_id="r"
        )
        issued = [r for r in caplog.records if "Deadline warning issued" in r.getMessage()]
        assert len(issued) == 1
        assert issued[0].levelno == logging.INFO
        assert issued[0].getMessage() == MAIN_LOG_TEMPLATE.format(n=40)
        assert issued[0].name == "robothor.engine.runner"

    def test_the_ladder_raises_that_line_to_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Item 0: INFO is invisible in the container this is read from."""
        from robothor.engine.run_pacing import DeadlinePacer

        caplog.set_level(logging.INFO, logger="robothor.engine.run_pacing")
        DeadlinePacer(mode="observe").note_for(
            _watchdog(960.0), iteration=40, task_text="", workspace=None, run_id="r"
        )
        issued = [r for r in caplog.records if "Deadline warning issued" in r.getMessage()]
        assert len(issued) == 1
        assert issued[0].levelno == logging.WARNING
        assert "run r" in issued[0].getMessage()


class TestTheCheckInIsMainsCheckIn:
    def test_off_matches_the_snapshot_verbatim(self) -> None:
        from robothor.engine.run_pacing import checkin_note

        assert checkin_note(80, 80, "off") == MAIN_CHECKIN.format(n=80)

    def test_off_fires_on_exactly_mains_cadence(self) -> None:
        from robothor.engine.run_pacing import checkin_note

        fired = [i for i in range(0, 400) if checkin_note(i, 80, "off")]
        main_fired = [i for i in range(0, 400) if i > 0 and i % 80 == 0]
        assert fired == main_fired

    def test_a_disabled_interval_fires_nothing_under_off(self) -> None:
        from robothor.engine.run_pacing import checkin_note

        assert [i for i in range(0, 200) if checkin_note(i, 0, "off")] == []


class TestOffTouchesNoToolCall:
    def test_no_guard_is_built_at_all(self) -> None:
        """Not merely inert: a disabled control must cost nothing, and a guard
        object means two `asyncio.to_thread` hops per tool call."""
        from robothor.engine import session_registry
        from robothor.engine.repeat_guard import guard_for_run

        session = SimpleNamespace(run_id="run-off", messages=[], _step_counter=0, repeat_guard=None)
        session.step_efficiency_mode = "off"
        session_registry.register(session)  # type: ignore[arg-type]
        try:
            assert guard_for_run("run-off") is None
        finally:
            session_registry.unregister("run-off")

    def test_no_timeout_is_clamped(self) -> None:
        from robothor.engine.run_pacing import clamp_tool_timeout

        assert clamp_tool_timeout(900, watchdog=_watchdog(1195.0), mode="off") == (900, "")

    def test_exec_results_carry_no_engine_annotation(self, tmp_path: Path) -> None:
        import asyncio

        from robothor.engine.stall_watchdog import _active_watchdog_var
        from robothor.engine.tools.handlers.filesystem import HANDLERS

        ctx = SimpleNamespace(workspace=str(tmp_path), run_id="")
        token = _active_watchdog_var.set(_watchdog(1195.0))
        try:
            result = asyncio.run(
                HANDLERS["exec"](  # type: ignore[arg-type]
                    {"command": "echo hi", "timeout": 900}, ctx
                )
            )
        finally:
            _active_watchdog_var.reset(token)
        # The default rung is `observe`, which also annotates nothing. Either
        # way the shipped result shape is unchanged.
        assert "timeout_note" not in result
