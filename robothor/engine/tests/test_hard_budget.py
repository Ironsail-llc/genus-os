"""Tests for hard budget enforcement (v2.hard_budget, v2.max_cost_usd)."""

from __future__ import annotations

from robothor.engine.session import AgentSession


class TestCheckBudgetCostExhausted:
    def test_cost_exhausted_at_limit(self):
        """Cost at or above max_cost_usd should return 'exhausted'."""
        session = AgentSession("t")
        session.run.total_cost_usd = 5.0
        assert session.check_budget(token_budget=0, max_cost_usd=5.0) == "exhausted"

    def test_cost_exhausted_over_limit(self):
        """Cost above max_cost_usd should return 'exhausted'."""
        session = AgentSession("t")
        session.run.total_cost_usd = 6.0
        assert session.check_budget(token_budget=0, max_cost_usd=5.0) == "exhausted"


class TestCheckBudgetCostWarning:
    def test_cost_warning_at_80_percent(self):
        """Cost at 80%+ of max_cost_usd should return 'warning'."""
        session = AgentSession("t")
        session.run.total_cost_usd = 4.5
        assert session.check_budget(token_budget=0, max_cost_usd=5.0) == "warning"

    def test_cost_warning_exactly_80_percent(self):
        """Cost at exactly 80% threshold should return 'warning'."""
        session = AgentSession("t")
        session.run.total_cost_usd = 4.0  # 80% of 5.0
        assert session.check_budget(token_budget=0, max_cost_usd=5.0) == "warning"


class TestCheckBudgetCostOk:
    def test_cost_ok_under_80_percent(self):
        """Cost under 80% of max_cost_usd should return 'ok'."""
        session = AgentSession("t")
        session.run.total_cost_usd = 2.0
        assert session.check_budget(token_budget=0, max_cost_usd=5.0) == "ok"

    def test_cost_ok_zero(self):
        """Zero cost should always be 'ok'."""
        session = AgentSession("t")
        session.run.total_cost_usd = 0.0
        assert session.check_budget(token_budget=0, max_cost_usd=5.0) == "ok"


class TestCheckBudgetTokenFallback:
    def test_token_budget_when_cost_unlimited(self):
        """With max_cost_usd=0 (unlimited), token-based budget should still work."""
        session = AgentSession("t")
        session.run.input_tokens = 800
        session.run.output_tokens = 300
        assert session.check_budget(token_budget=1000, max_cost_usd=0.0) == "exhausted"

    def test_token_warning_when_cost_unlimited(self):
        """Token warning should fire when cost is unlimited."""
        session = AgentSession("t")
        session.run.input_tokens = 700
        session.run.output_tokens = 200  # 900 = 90% of 1000
        assert session.check_budget(token_budget=1000, max_cost_usd=0.0) == "warning"


class TestProjectNextCallCost:
    def test_empty_returns_zero(self):
        """New session with no recorded costs should project 0.0."""
        session = AgentSession("t")
        assert session.project_next_call_cost() == 0.0

    def test_rolling_average_of_last_three(self):
        """Should return the average of the last 3 step costs."""
        session = AgentSession("t")
        session.record_step_cost(1.0)
        session.record_step_cost(2.0)
        session.record_step_cost(3.0)
        assert session.project_next_call_cost() == 2.0  # avg(1, 2, 3)

    def test_rolling_average_more_than_three(self):
        """When more than 3 costs are recorded, only last 3 are used."""
        session = AgentSession("t")
        session.record_step_cost(10.0)  # dropped from window
        session.record_step_cost(1.0)
        session.record_step_cost(2.0)
        session.record_step_cost(3.0)
        assert session.project_next_call_cost() == 2.0  # avg(1, 2, 3)

    def test_single_cost(self):
        """With one recorded cost, projection should equal that cost."""
        session = AgentSession("t")
        session.record_step_cost(0.5)
        assert session.project_next_call_cost() == 0.5


class TestRecordStepCost:
    def test_costs_accumulate(self):
        """Each record_step_cost call should grow the internal list."""
        session = AgentSession("t")
        session.record_step_cost(0.1)
        session.record_step_cost(0.2)
        session.record_step_cost(0.3)
        assert session._step_costs == [0.1, 0.2, 0.3]

    def test_zero_cost_recorded(self):
        """Zero-cost steps should still be recorded."""
        session = AgentSession("t")
        session.record_step_cost(0.0)
        assert len(session._step_costs) == 1


class TestCheckBudgetCostTakesPrecedence:
    def test_cost_exhausted_overrides_token_ok(self):
        """If cost is exhausted but tokens are fine, should return 'exhausted'."""
        session = AgentSession("t")
        session.run.total_cost_usd = 5.0
        session.run.input_tokens = 100
        session.run.output_tokens = 100
        assert session.check_budget(token_budget=10000, max_cost_usd=5.0) == "exhausted"

    def test_cost_warning_overrides_token_ok(self):
        """If cost is in warning zone but tokens are fine, should return 'warning'."""
        session = AgentSession("t")
        session.run.total_cost_usd = 4.5
        session.run.input_tokens = 100
        session.run.output_tokens = 100
        assert session.check_budget(token_budget=10000, max_cost_usd=5.0) == "warning"
