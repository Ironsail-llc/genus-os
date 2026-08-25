"""The per-minute tool cap is a throttle on real work.

`DEFAULT_RATE_LIMIT = 30` was a module constant with no way to change it, and
it blocks the tool call rather than slowing it: the agent is told "rate limit
exceeded" and, in a run measured on WildClawBench, gave up and reported that
it could not continue.

Measured on this instance, 2026-08-24:

* 127 `rate_limit` block events in production over 30 days, most recent that
  same day. This is not hypothetical — it fires on the fleet.
* Across 900 real runs of more than five tool calls, the mean rate is 7.2
  calls/minute and the peak is 36.8. So the cap sits just under the top of
  the legitimate distribution, which is the worst possible place for it: too
  high to catch anything quickly, low enough to punish the tail of normal
  behaviour.

Runaway protection has three other owners — `max_iterations`, `safety_cap`,
and the runaway-token guard — all of which bound a loop that has genuinely
escaped. This one bounds a burst, and bursts are what reading ten files looks
like.
"""

from __future__ import annotations

from robothor.engine.guardrails import DEFAULT_RATE_LIMIT, GuardrailEngine


def _engine(**kw) -> GuardrailEngine:
    return GuardrailEngine(enabled_policies=["rate_limit"], **kw)


def _burst(engine: GuardrailEngine, n: int, agent_id: str = "a") -> int:
    """Fire n calls, return how many were allowed."""
    allowed = 0
    for _ in range(n):
        if engine.check_pre_execution("read_file", {}, agent_id=agent_id).allowed:
            allowed += 1
    return allowed


class TestTheDefaultIsUnchanged:
    """Deliberately not raised — see the constant's docstring. The evidence
    says it should be; the fallout says that is a soak, not a drive-by."""

    def test_the_default_still_applies_when_nothing_is_configured(self):
        assert _burst(_engine(), DEFAULT_RATE_LIMIT + 5) == DEFAULT_RATE_LIMIT


class TestItIsConfigurable:
    def test_an_agent_can_lower_it(self):
        engine = _engine(rate_limit_per_minute=5)
        assert _burst(engine, 10) == 5

    def test_an_agent_can_raise_it(self):
        engine = _engine(rate_limit_per_minute=200)
        assert _burst(engine, 150) == 150

    def test_zero_means_use_the_platform_default(self):
        """0 is 'unset', not 'block everything' — a config that silently
        disabled every tool would be the worst reading of an empty field."""
        engine = _engine(rate_limit_per_minute=0)
        assert _burst(engine, 10) == 10


class TestItStillCatchesARunaway:
    def test_a_loop_far_past_the_limit_is_stopped(self):
        engine = _engine(rate_limit_per_minute=10)
        assert _burst(engine, 100) == 10

    def test_the_block_names_the_limit_it_applied(self):
        """A message quoting a number the agent is not actually held to sends
        the reader to the wrong knob."""
        engine = _engine(rate_limit_per_minute=3)
        _burst(engine, 3)
        result = engine.check_pre_execution("read_file", {}, agent_id="a")
        assert not result.allowed
        assert "3" in result.reason

    def test_the_budget_is_per_agent(self):
        engine = _engine(rate_limit_per_minute=5)
        _burst(engine, 5, agent_id="noisy")
        assert engine.check_pre_execution("read_file", {}, agent_id="quiet").allowed
