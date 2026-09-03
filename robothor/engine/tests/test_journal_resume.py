"""Whether a run is told where it left off.

Extracted from `execute`. The trigger gate is the invariant: only CRON, HOOK
and WORKFLOW runs resume from a journal. An interactive run already has a human
telling it what to do, and prepending "here is where you left off" would answer
a question nobody asked — and could steer the agent back to yesterday's task.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from robothor.engine.journal_resume import maybe_prepend_journal_resume
from robothor.engine.models import TriggerType


def _config(resume=True, journal="journal.json"):
    return SimpleNamespace(resume_on_start=resume, journal_file=journal)


def _state():
    return SimpleNamespace(experiment_id="exp-1", iteration=4, next_action="measure again")


def _call(**kw):
    return maybe_prepend_journal_resume(
        kw.pop("message", "do the next step"),
        agent_id=kw.pop("agent_id", "auto-researcher"),
        agent_config=kw.pop("agent_config", _config()),
        trigger_type=kw.pop("trigger_type", TriggerType.CRON),
        workspace=kw.pop("workspace", "/ws"),
    )


# ── Who resumes ───────────────────────────────────────────────────────


@pytest.mark.parametrize("trigger", [TriggerType.CRON, TriggerType.HOOK, TriggerType.WORKFLOW])
def test_a_scheduled_run_is_told_where_it_left_off(trigger):
    with patch("robothor.engine.journal.JournalManager") as jm:
        jm.load.return_value = _state()
        jm.format_resume_preamble.return_value = "RESUMING: iteration 4"
        out = _call(trigger_type=trigger)

    assert out.startswith("RESUMING: iteration 4")
    assert out.endswith("do the next step")


@pytest.mark.parametrize("trigger", [TriggerType.TELEGRAM, TriggerType.WEBCHAT, TriggerType.MANUAL])
def test_an_interactive_run_is_not(trigger):
    """The human is already saying what they want."""
    with patch("robothor.engine.journal.JournalManager") as jm:
        jm.load.return_value = _state()
        out = _call(trigger_type=trigger)

    assert out == "do the next step"
    jm.load.assert_not_called()


# ── Opting in ─────────────────────────────────────────────────────────


def test_an_agent_that_did_not_ask_to_resume_does_not():
    with patch("robothor.engine.journal.JournalManager") as jm:
        out = _call(agent_config=_config(resume=False))

    assert out == "do the next step"
    jm.load.assert_not_called()


def test_an_agent_with_no_journal_file_does_not():
    with patch("robothor.engine.journal.JournalManager") as jm:
        out = _call(agent_config=_config(journal=""))

    assert out == "do the next step"
    jm.load.assert_not_called()


# ── When the journal is not there ─────────────────────────────────────


def test_an_empty_journal_leaves_the_message_alone():
    with patch("robothor.engine.journal.JournalManager") as jm:
        jm.load.return_value = None
        assert _call() == "do the next step"


def test_a_broken_journal_costs_continuity_not_the_run():
    with patch("robothor.engine.journal.JournalManager") as jm:
        jm.load.side_effect = RuntimeError("corrupt json")
        assert _call() == "do the next step"


def test_a_failing_preamble_renderer_is_also_survivable():
    with patch("robothor.engine.journal.JournalManager") as jm:
        jm.load.return_value = _state()
        jm.format_resume_preamble.side_effect = RuntimeError("bad state")
        assert _call() == "do the next step"


# ── The message itself ────────────────────────────────────────────────


def test_the_original_message_is_never_lost():
    """The journal is context, not a replacement for the instruction."""
    with patch("robothor.engine.journal.JournalManager") as jm:
        jm.load.return_value = _state()
        jm.format_resume_preamble.return_value = "RESUMING"
        out = _call(message="URGENT: stop the experiment")

    assert "URGENT: stop the experiment" in out
