"""What a run is told about the time it has left, and whether anyone can see it.

Item 0 first, because it decides whether anything else here is worth building.
The engine's only time-awareness was ONE note at 80% of the wall-clock ceiling,
announced with ``logger.info("Deadline warning issued …")``. Neither of the two
profiled WildClawBench runs that hit their budget had that line in ``agent.log``
— so either the note never fired, or it fired and nobody could see it.

It fired. ``bench/wildclaw/run_one.py`` builds ``AgentRunner`` and calls
``execute()``, which sets ``_active_watchdog_var`` before ``_run_loop`` is
awaited in the same task, and the bench passes the task's own budget straight
through, so the ceiling the note measures against is the same 1200s the
container is killed at and 80% of it is 960s — comfortably inside the run.

What nobody could see is the log line. ``run_one.py`` installs no logging
configuration, so the root logger has no handlers and Python falls back to
``logging.lastResort``, whose level is WARNING. Every ``logger.info`` in the
engine is dropped on the floor in that container. The control was not inert;
it was UNOBSERVABLE, which is how a whole day of profiling concluded it had
never run. The fix is one word — warning, not info — and the tests below pin
it so no future observe-mode line repeats the mistake.

Then the pacing itself: one note at 80% is too late to change a plan and says
nothing about the rate the run is burning. Three notes, each carrying the
measured seconds-per-iteration and how many steps that pace leaves.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest


def _watchdog(elapsed: float, hard: float = 1200.0) -> Any:
    """The two attributes the runner reads off the live stall watchdog."""
    return SimpleNamespace(elapsed_seconds=elapsed, _hard_timeout=hard)


# ── Item 0: the note fires, and the log line can be seen ────────────────────


class TestItemZeroTheNoteFires:
    def test_a_watchdog_past_eighty_percent_produces_the_note(self) -> None:
        from robothor.engine.run_pacing import DeadlinePacer

        pacer = DeadlinePacer(mode="off")
        note = pacer.note_for(_watchdog(960.0), iteration=40, task_text="", workspace=None)
        assert note is not None
        assert "960s used of 1200s" in note

    def test_below_the_threshold_nothing_is_said(self) -> None:
        from robothor.engine.run_pacing import DeadlinePacer

        pacer = DeadlinePacer(mode="off")
        assert pacer.note_for(_watchdog(600.0), iteration=40, task_text="", workspace=None) is None

    def test_no_watchdog_means_no_ceiling_and_no_note(self) -> None:
        from robothor.engine.run_pacing import DeadlinePacer

        pacer = DeadlinePacer(mode="enforce")
        assert pacer.note_for(None, iteration=40, task_text="", workspace=None) is None

    def test_a_zero_ceiling_means_no_note(self) -> None:
        """`timeout_seconds: 0` is the manifest sentinel for 'no cap'."""
        from robothor.engine.run_pacing import DeadlinePacer

        pacer = DeadlinePacer(mode="enforce")
        wd = _watchdog(9999.0, hard=0.0)
        assert pacer.note_for(wd, iteration=40, task_text="", workspace=None) is None

    def test_the_log_line_is_warning_not_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """INFO is invisible in the container this control exists for.

        On the LADDER. `off` keeps main's INFO line verbatim, because `off` has
        to be the engine that shipped — see test_step_efficiency_off_is_main.py.
        `observe` is the default every existing install gets, so the fix is live
        without the baseline moving.
        """
        from robothor.engine.run_pacing import DeadlinePacer

        caplog.set_level(logging.WARNING, logger="robothor.engine.run_pacing")
        DeadlinePacer(mode="observe").note_for(
            _watchdog(960.0), iteration=40, task_text="", workspace=None, run_id="run-1"
        )
        issued = [r for r in caplog.records if "Deadline warning issued" in r.getMessage()]
        assert issued, "the deadline note announced itself at a level nothing captures"
        assert issued[0].levelno == logging.WARNING
        assert "run-1" in issued[0].getMessage()

    def test_pythons_fallback_handler_drops_info(self) -> None:
        """The evidence behind item 0, pinned so the reasoning is checkable.

        A process that never configures logging gets ``logging.lastResort``,
        whose level is WARNING. Any control that announces itself below that
        level is unobservable in such a process.
        """
        assert logging.lastResort is not None
        assert logging.lastResort.level >= logging.WARNING

    def test_the_benchmark_container_configures_no_logging(self) -> None:
        """So the fallback above is what `agent.log` actually gets."""
        repo = Path(__file__).resolve().parents[3]
        entry = (repo / "bench" / "wildclaw" / "run_one.py").read_text(encoding="utf-8")
        assert "basicConfig" not in entry
        assert "dictConfig" not in entry

    def test_the_benchmark_ceiling_puts_the_note_inside_the_run(self) -> None:
        """The other half of item 0: 80% of the ceiling must be reachable.

        The bench hands the task's own budget to ``timeout_seconds``. A tempo
        factor that inflated the ceiling past it would push the note beyond the
        point the container is killed — the note would be correct and would
        never arrive.
        """
        from robothor.engine.watchdog_budgets import watchdog_budgets_for

        cfg = SimpleNamespace(
            model_primary="openrouter/example/model-under-test",
            model_fallbacks=[],
            timeout_seconds=1200,
            stall_timeout_seconds=0,
            early_stall_timeout_seconds=0,
        )
        budgets = watchdog_budgets_for(cfg)
        assert budgets.hard == 1200
        assert budgets.hard * 0.8 < 1200


# ── Pace: how fast is this run burning, and what does that leave ────────────


class TestThePacePhrase:
    def test_it_names_the_rate_and_the_steps_that_rate_leaves(self) -> None:
        from robothor.engine.run_pacing import pace_phrase

        # 60 iterations in 600s is 10s each; 240s left is about 24 more.
        phrase = pace_phrase(elapsed=600.0, remaining=240.0, iteration=60)
        assert "60 steps" in phrase
        assert "10s" in phrase
        assert "about 24 more steps" in phrase

    def test_iteration_zero_has_no_measured_pace(self) -> None:
        from robothor.engine.run_pacing import pace_phrase

        assert pace_phrase(elapsed=600.0, remaining=240.0, iteration=0) == ""

    def test_a_pace_that_leaves_no_steps_says_so(self) -> None:
        from robothor.engine.run_pacing import pace_phrase

        phrase = pace_phrase(elapsed=1140.0, remaining=60.0, iteration=10)
        assert "about 0 more steps" in phrase


class TestTheThreeNotes:
    def _pacer(self) -> Any:
        from robothor.engine.run_pacing import DeadlinePacer

        return DeadlinePacer(mode="enforce")

    def test_each_rung_fires_exactly_once(self) -> None:
        pacer = self._pacer()
        fired = []
        for elapsed in range(0, 1200, 12):  # 100 samples across the budget
            note = pacer.note_for(
                _watchdog(float(elapsed)), iteration=elapsed // 12, task_text="", workspace=None
            )
            if note:
                fired.append(elapsed)
        assert len(fired) == 3, fired
        assert 600 <= fired[0] < 612
        assert 960 <= fired[1] < 972
        assert 1140 <= fired[2] < 1152

    def test_never_more_than_three_notes_however_long_the_loop_runs(self) -> None:
        pacer = self._pacer()
        notes = [
            pacer.note_for(_watchdog(float(e)), iteration=e, task_text="", workspace=None)
            for e in range(1, 4000)
        ]
        assert len([n for n in notes if n]) <= 3

    def test_no_note_at_iteration_zero(self) -> None:
        """Nothing useful can be said about a pace nobody has set yet."""
        pacer = self._pacer()
        assert pacer.note_for(_watchdog(1100.0), iteration=0, task_text="", workspace=None) is None

    def test_a_lower_rung_never_fires_after_a_higher_one(self) -> None:
        """A run's clock only moves forward; its notes must too."""
        pacer = self._pacer()
        first = pacer.note_for(_watchdog(1150.0), iteration=50, task_text="", workspace=None)
        assert first is not None and "LAST" in first
        later = pacer.note_for(_watchdog(1160.0), iteration=51, task_text="", workspace=None)
        assert later is None

    def test_every_note_carries_elapsed_remaining_and_pace(self) -> None:
        pacer = self._pacer()
        for elapsed in (600.0, 960.0, 1140.0):
            note = pacer.note_for(_watchdog(elapsed), iteration=60, task_text="", workspace=None)
            assert note is not None, elapsed
            assert f"{int(elapsed)}s used of 1200s" in note
            assert "more steps" in note

    def test_the_eighty_and_ninetyfive_notes_keep_the_write_first_wording(self) -> None:
        pacer = self._pacer()
        pacer.note_for(_watchdog(600.0), iteration=60, task_text="", workspace=None)
        eighty = pacer.note_for(_watchdog(960.0), iteration=60, task_text="", workspace=None)
        ninetyfive = pacer.note_for(_watchdog(1140.0), iteration=70, task_text="", workspace=None)
        assert eighty is not None and ninetyfive is not None
        assert "FIRST write" in eighty
        assert "write" in ninetyfive.lower()

    def test_the_eighty_percent_note_names_the_missing_deliverable(self, tmp_path: Path) -> None:
        pacer = self._pacer()
        task = f"Save the result to {tmp_path}/results/result.png when you are done."
        pacer.note_for(_watchdog(600.0), iteration=60, task_text=task, workspace=tmp_path)
        note = pacer.note_for(_watchdog(960.0), iteration=60, task_text=task, workspace=tmp_path)
        assert note is not None
        assert "result.png" in note

    def test_the_ninetyfive_percent_note_names_it_too(self, tmp_path: Path) -> None:
        pacer = self._pacer()
        task = f"Save the result to {tmp_path}/results/result.png when you are done."
        note = pacer.note_for(_watchdog(1150.0), iteration=70, task_text=task, workspace=tmp_path)
        assert note is not None
        assert "result.png" in note

    def test_a_deliverable_that_exists_is_not_nagged_about(self, tmp_path: Path) -> None:
        (tmp_path / "out.md").write_text("done", encoding="utf-8")
        pacer = self._pacer()
        task = f"Write the summary to {tmp_path}/out.md"
        note = pacer.note_for(_watchdog(960.0), iteration=60, task_text=task, workspace=tmp_path)
        assert note is not None
        assert "do not exist yet" not in note


class TestTheLadder:
    def test_off_keeps_exactly_the_one_note_that_already_shipped(self) -> None:
        from robothor.engine.run_pacing import DeadlinePacer

        pacer = DeadlinePacer(mode="off")
        fired = [
            e
            for e in range(0, 1200, 12)
            if pacer.note_for(_watchdog(float(e)), iteration=e // 12, task_text="", workspace=None)
        ]
        assert len(fired) == 1
        assert 960 <= fired[0] < 972

    def test_observe_injects_only_the_shipped_note(self) -> None:
        from robothor.engine.run_pacing import DeadlinePacer

        pacer = DeadlinePacer(mode="observe")
        fired = [
            e
            for e in range(0, 1200, 12)
            if pacer.note_for(_watchdog(float(e)), iteration=e // 12, task_text="", workspace=None)
        ]
        assert len(fired) == 1
        assert 960 <= fired[0] < 972

    def test_observe_logs_the_rungs_it_withholds_at_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from robothor.engine.run_pacing import DeadlinePacer

        caplog.set_level(logging.WARNING, logger="robothor.engine.run_pacing")
        pacer = DeadlinePacer(mode="observe")
        pacer.note_for(_watchdog(600.0), iteration=60, task_text="", workspace=None, run_id="run-7")
        shadow = [r for r in caplog.records if "step-efficiency observe" in r.getMessage()]
        assert shadow, "observe withheld a note and said nothing about it"
        assert shadow[0].levelno == logging.WARNING
        assert "run-7" in shadow[0].getMessage()
        assert "50%" in shadow[0].getMessage()


class TestTheRunnerWiring:
    def test_the_runner_delegates_to_the_pacer(self) -> None:
        """Pin the wiring by what it READS, not by which symbols appear.

        `messages[0]` is the SYSTEM prompt; reading it here once shipped the
        deliverable note inert, because the text it was handed named no paths.
        """
        import robothor.engine.runner as m

        body = Path(m.__file__).read_text(encoding="utf-8")
        start = body.index("_pacer.note_for(")
        block = body[start : body.index("if _safety_cap > 0", start)]
        assert "task_text_for_run(session)" in block
        assert "session.messages[0]" not in block, (
            "messages[0] is the SYSTEM prompt — the note would read the wrong text"
        )

    def test_the_pacer_is_built_from_the_runs_cached_rung(self) -> None:
        """`mode_for_run` resolves the flag once and caches it on the session,
        so the clamp — called from `exec`, 41 times in the profiled run — does
        not repeat a DB-backed flag read on the event loop."""
        import robothor.engine.runner as m

        body = Path(m.__file__).read_text(encoding="utf-8")
        # One read, kept in `_mode` and handed to every control that needs the
        # rung — the pacer, the repeat guard's cache, and the budget stop.
        assert "_mode = mode_for_run(session.run_id)" in body
        assert "DeadlinePacer(mode=_mode)" in body
        assert body.count("mode_for_run(") == 1, "the rung is resolved once per run"
        assert "step_efficiency_mode()" not in body, (
            "the runner resolves the rung through mode_for_run, which seeds the cache"
        )
