"""An agent about to run out of time should be told, while it can still act.

Today a run that hits its wall-clock ceiling is simply killed. Whatever it had
done and not yet written is lost — the work happened, the artefact never
existed, and the run is scored as though nothing was attempted.

Measured on WildClawBench's Productivity Flow tasks, 2026-08-25:

    bibtex       budget  900s   ran  925s   completed, nothing written
    pdf_digest   budget  900s   ran 1020s   killed
    arxiv_digest budget 1200s   ran 1320s   killed

All three scored zero. OpenClaw scores 38.8% on that category, and the
difference is not that it finishes — it is that the graders award per
criterion, so partial output earns partial credit while an empty results
directory earns none.

This is not a benchmark quirk. Every scheduled agent on this fleet carries a
timeout, and every one of them currently loses its work wholesale at the
deadline rather than flushing what it has.

The warning fires once, names the numbers, and says what to do. It is
deliberately not a hard stop: the agent decides what "save what you have"
means for the task in front of it.
"""

from __future__ import annotations

from robothor.engine.runner import deadline_warning

HARD_TIMEOUT = 600.0


class TestWhenItFires:
    def test_silent_early_in_the_run(self):
        assert deadline_warning(elapsed=60.0, hard_timeout=HARD_TIMEOUT) is None

    def test_silent_just_below_the_threshold(self):
        assert deadline_warning(elapsed=440.0, hard_timeout=HARD_TIMEOUT) is None

    def test_fires_once_past_the_threshold(self):
        assert deadline_warning(elapsed=480.0, hard_timeout=HARD_TIMEOUT) is not None

    def test_fires_when_nearly_out(self):
        assert deadline_warning(elapsed=580.0, hard_timeout=HARD_TIMEOUT) is not None


class TestWhenThereIsNoDeadline:
    def test_an_unbounded_run_is_never_warned(self):
        """`timeout_seconds: 0` means no ceiling. Warning there would be a
        lie, and a recurring one."""
        assert deadline_warning(elapsed=10_000.0, hard_timeout=0) is None

    def test_a_negative_ceiling_is_treated_as_none(self):
        assert deadline_warning(elapsed=100.0, hard_timeout=-1) is None


class TestWhatItSays:
    def test_it_names_both_numbers(self):
        """'You are running out of time' is not actionable. How long is left,
        against how much, is."""
        message = deadline_warning(elapsed=480.0, hard_timeout=HARD_TIMEOUT)
        assert "480" in message
        assert "600" in message

    def test_it_says_to_save_partial_work(self):
        message = deadline_warning(elapsed=480.0, hard_timeout=HARD_TIMEOUT)
        lowered = message.lower()
        assert "save" in lowered or "write" in lowered
        assert "partial" in lowered

    def test_it_tells_the_agent_not_to_start_something_new(self):
        """The failure mode being fixed is an agent that begins one more
        subtask and gets killed holding all of it."""
        message = deadline_warning(elapsed=480.0, hard_timeout=HARD_TIMEOUT)
        assert "not start" in message.lower() or "do not begin" in message.lower()

    def test_it_is_marked_as_a_system_note(self):
        """Consistent with every other engine-injected message, so an agent
        reads it as instruction rather than as something a user said."""
        assert deadline_warning(elapsed=480.0, hard_timeout=HARD_TIMEOUT).startswith("[SYSTEM]")


class TestTheThresholdIsSane:
    def test_it_leaves_enough_time_to_act_on(self):
        """A warning at 95% is a warning the agent cannot use. At 80% of a
        900s budget there are three minutes left, which is a few tool calls
        at the measured rate of roughly six seconds each."""
        from robothor.engine.runner import DEADLINE_WARNING_FRACTION

        assert 0.6 <= DEADLINE_WARNING_FRACTION <= 0.85


class TestItReachesTheAgent:
    """A warning the model never sees is a log line.

    This is the same failure the credential detector had: the platform knew
    something and told only the journal.
    """

    def test_the_loop_appends_it_to_the_conversation(self):
        from pathlib import Path

        # Bounded by the NEXT statement rather than a character count: a
        # fixed window breaks the moment anything is added to the block,
        # which is a test failing for the wrong reason.
        src = (Path(__file__).resolve().parents[1] / "runner.py").read_text(encoding="utf-8")
        start = src.index("_deadline_warned and self._active_watchdog")
        window = src[start : src.index("if _safety_cap > 0", start)]
        assert "session.messages.append" in window
        assert "ENGINE_CONTEXT_ROLE" in window

    def test_it_is_latched_so_it_does_not_repeat_every_iteration(self):
        """Repeating it each turn would crowd out the work it is asking for."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "runner.py").read_text(encoding="utf-8")
        assert "_deadline_warned = False" in src
        assert "_deadline_warned = True" in src


class TestCheckpointThenContinue:
    """The nudge orders write-first, improve-after.

    On a graded 1200s research run the agent received the warning at 978s and
    spent ~120 more seconds verifying before its FIRST write to the output
    location; a marginally slower finish would have scored zero on
    everything. "Save your partial work" alone lets an agent read it as
    "finish up, then save" — the order matters and the message states it.
    """

    def test_the_message_orders_write_first_then_improve(self):
        from robothor.engine.run_budget import deadline_warning

        note = deadline_warning(80.0, 100.0)
        assert note is not None
        assert "FIRST" in note
        assert "then keep improving" in note
