"""Tests for token/cost budget controls."""

from __future__ import annotations

import pytest

from robothor.engine.models import AgentConfig, AgentRun
from robothor.engine.runtime.budget import SharedBudget
from robothor.engine.session import AgentSession


class TestSessionBudgetCheck:
    def test_ok_when_no_budget(self):
        session = AgentSession("t")
        assert session.check_budget(0) == "ok"

    def test_ok_when_under_token_budget(self):
        session = AgentSession("t")
        session.run.input_tokens = 500
        session.run.output_tokens = 200
        assert session.check_budget(token_budget=1000) == "ok"

    def test_warning_when_approaching_token_budget(self):
        session = AgentSession("t")
        session.run.input_tokens = 700
        session.run.output_tokens = 200  # 900 total, 80% of 1000
        assert session.check_budget(token_budget=1000) == "warning"

    def test_exhausted_when_over_token_budget(self):
        session = AgentSession("t")
        session.run.input_tokens = 800
        session.run.output_tokens = 300  # 1100 > 1000
        assert session.check_budget(token_budget=1000) == "exhausted"

    def test_cost_not_enforced(self):
        """Cost is tracked but not enforced — high cost does not trigger exhaustion."""
        session = AgentSession("t")
        session.run.total_cost_usd = 100.0  # Very high cost
        assert session.check_budget() == "ok"

    def test_token_exhausted_with_high_cost(self):
        """Token budget exhaustion still works even with high cost."""
        session = AgentSession("t")
        session.run.input_tokens = 2000
        session.run.output_tokens = 0
        session.run.total_cost_usd = 0.001
        assert session.check_budget(token_budget=1000) == "exhausted"


class TestBudgetAgentConfig:
    def test_defaults_are_zero(self):
        config = AgentConfig(id="x", name="x")
        assert config.token_budget == 0

    def test_custom_values(self):
        config = AgentConfig(
            id="x",
            name="x",
            token_budget=50000,
        )
        assert config.token_budget == 50000


class TestBudgetAgentRun:
    def test_defaults(self):
        run = AgentRun()
        assert run.token_budget == 0
        assert run.cost_budget_usd == 0.0  # Still on AgentRun for tracking
        assert run.budget_exhausted is False


class TestSharedBudget:
    """One call's overrun must not poison the whole shared envelope.

    Regression for task f227bd47: goals were blocked with "shared budget
    exhausted" while >90% of the token budget was unspent. The bound passed to
    reserve() is a heuristic estimate (context_fit.estimate_for), so a real
    provider tokenizer can legitimately exceed it; that must be recorded, not
    latched into a permanent refusal.
    """

    def test_reserve_charges_and_refuses_when_limit_would_be_exceeded(self):
        budget = SharedBudget(100)
        budget.reserve("a", 60)
        assert budget.charged == 60
        with pytest.raises(ValueError, match="shared budget exhausted"):
            budget.reserve("b", 50)
        assert budget.charged == 60

    def test_overrun_is_recorded_but_does_not_poison_the_budget(self):
        budget = SharedBudget(100)
        budget.reserve("a", 60)
        budget.settle("a", 70)  # provider reports more than the estimate
        assert budget.charged == 70
        budget.reserve("b", 30)  # 70 + 30 == 100, still affordable
        assert budget.charged == 100
        with pytest.raises(ValueError, match="shared budget exhausted"):
            budget.reserve("c", 1)

    def test_genuine_exhaustion_still_stops(self):
        budget = SharedBudget(100)
        budget.reserve("a", 100)
        budget.settle("a", 100)
        with pytest.raises(ValueError, match="shared budget exhausted"):
            budget.reserve("b", 1)

    def test_unknown_usage_stays_charged(self):
        budget = SharedBudget(100)
        budget.reserve("a", 60)
        budget.settle("a", None)
        assert budget.charged == 60

    def test_duplicate_reservation_is_refused(self):
        budget = SharedBudget(100)
        budget.reserve("a", 10)
        with pytest.raises(ValueError, match="already reserved"):
            budget.reserve("a", 10)
