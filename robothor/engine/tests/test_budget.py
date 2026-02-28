"""Tests for token/cost budget controls."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, AgentRun, RunStatus, TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.session import AgentSession


class TestSessionBudgetCheck:
    def test_ok_when_no_budget(self):
        session = AgentSession("t")
        assert session.check_budget(0, 0.0) == "ok"

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

    def test_ok_when_under_cost_budget(self):
        session = AgentSession("t")
        session.run.total_cost_usd = 0.001
        assert session.check_budget(cost_budget_usd=0.01) == "ok"

    def test_warning_when_approaching_cost_budget(self):
        session = AgentSession("t")
        session.run.total_cost_usd = 0.0085  # 85% of 0.01
        assert session.check_budget(cost_budget_usd=0.01) == "warning"

    def test_exhausted_when_over_cost_budget(self):
        session = AgentSession("t")
        session.run.total_cost_usd = 0.015  # > 0.01
        assert session.check_budget(cost_budget_usd=0.01) == "exhausted"

    def test_token_exhausted_takes_priority(self):
        """Token budget exhaustion is checked first."""
        session = AgentSession("t")
        session.run.input_tokens = 2000
        session.run.output_tokens = 0
        session.run.total_cost_usd = 0.001
        assert session.check_budget(token_budget=1000, cost_budget_usd=0.01) == "exhausted"


class TestBudgetAgentConfig:
    def test_defaults_are_zero(self):
        config = AgentConfig(id="x", name="x")
        assert config.token_budget == 0
        assert config.cost_budget_usd == 0.0

    def test_custom_values(self):
        config = AgentConfig(
            id="x", name="x",
            token_budget=50000,
            cost_budget_usd=0.50,
        )
        assert config.token_budget == 50000
        assert config.cost_budget_usd == 0.50


class TestBudgetAgentRun:
    def test_defaults(self):
        run = AgentRun()
        assert run.token_budget == 0
        assert run.cost_budget_usd == 0.0
        assert run.budget_exhausted is False
