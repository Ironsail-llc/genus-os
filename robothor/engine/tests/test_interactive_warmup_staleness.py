"""The operator's conversations must load memory, not just the first one ever.

`runner.execute` gated interactive warmup on `not conversation_history`:

    elif trigger_type in (TriggerType.TELEGRAM, TriggerType.WEBCHAT):
        # Only warmup on first message of a session — follow-ups already
        # have memory blocks and entity context in conversation history.
        if not conversation_history:
            warmup_kind = "interactive"

`main.yaml` sets `session_target: persistent`, and that session holds 5,560
messages. History is therefore never empty, so the branch never fires.

Measured on production over 30 days, warmup sections actually executed:

    cron           239 runs   2635 sections   11.0 per run
    telegram        51 runs      0 sections    0.0 per run
    sub_agent      331 runs      0 sections    0.0 per run
    channel_event    4 runs      0 sections    0.0 per run

Every scheduled run loads eleven sections — memory blocks, preferences,
breadcrumbs, entity context, open tasks. Every conversation with the operator
loads none.

The comment's justification is false. `runner.py` prepends the preamble to a
LOCAL variable; `telegram.py` appends only raw user text to the session, and
`session.py` rebuilds from history plus the user message. The preamble is never
persisted, so it cannot reach a follow-up turn — there is nothing in history for
the follow-up to inherit.

Note the trap this sets for anyone measuring it: `warmup_preamble_build` is
recorded on all 51 telegram runs even when it builds nothing, so "runs with a
warmup step" reads 51/51. Only the section count tells the truth.

Replaced with a staleness clock: warm the first turn, then again once the
preamble is older than the interval. A conversation that has been going for
hours gets fresh memory; a rapid back-and-forth does not pay for it every turn.
"""

from __future__ import annotations

from robothor.engine.runner import INTERACTIVE_WARMUP_MAX_AGE_S, should_warm_interactive


class TestFirstTurn:
    def test_an_empty_history_still_warms(self) -> None:
        """The pre-existing behaviour must survive."""
        assert should_warm_interactive(history_len=0, seconds_since_warmup=None) is True


class TestPersistentSession:
    def test_a_follow_up_on_a_cold_session_warms(self) -> None:
        """The defect: 5,560 messages of history meant this never fired."""
        assert should_warm_interactive(history_len=5560, seconds_since_warmup=None) is True

    def test_a_stale_session_re_warms(self) -> None:
        assert (
            should_warm_interactive(
                history_len=5560, seconds_since_warmup=INTERACTIVE_WARMUP_MAX_AGE_S + 1
            )
            is True
        )

    def test_a_rapid_follow_up_does_not_re_warm(self) -> None:
        """Back-to-back turns must not pay for warmup every time."""
        assert should_warm_interactive(history_len=5560, seconds_since_warmup=5) is False

    def test_exactly_at_the_boundary_does_not_re_warm(self) -> None:
        assert (
            should_warm_interactive(
                history_len=5560, seconds_since_warmup=INTERACTIVE_WARMUP_MAX_AGE_S
            )
            is False
        )


class TestTheIntervalIsSane:
    def test_not_so_short_that_every_turn_warms(self) -> None:
        assert INTERACTIVE_WARMUP_MAX_AGE_S >= 300

    def test_not_so_long_that_a_working_session_never_refreshes(self) -> None:
        assert INTERACTIVE_WARMUP_MAX_AGE_S <= 3600
