"""Tests for managed_agents.models — standalone dataclasses."""

from robothor.engine.managed_agents.models import (
    MAAgentConfig,
    MAEnvironmentConfig,
    MARunResult,
    MASessionConfig,
)


class TestMAAgentConfig:
    def test_defaults(self):
        cfg = MAAgentConfig(name="test", model="claude-sonnet-4-6")
        assert cfg.name == "test"
        assert cfg.model == "claude-sonnet-4-6"
        assert cfg.system_prompt == ""
        assert cfg.tools == []
        assert cfg.callable_agents == []

    def test_with_tools(self):
        tools = [{"type": "custom", "name": "foo"}]
        cfg = MAAgentConfig(name="t", model="m", tools=tools)
        assert len(cfg.tools) == 1
        assert cfg.tools[0]["name"] == "foo"


class TestMASessionConfig:
    def test_minimal(self):
        cfg = MASessionConfig(agent_id="agent_123")
        assert cfg.agent_id == "agent_123"
        assert cfg.environment_id == ""
        assert cfg.resources == []

    def test_with_resources(self):
        cfg = MASessionConfig(
            agent_id="a",
            environment_id="e",
            resources=[{"type": "memory_store", "memory_store_id": "ms_1"}],
        )
        assert len(cfg.resources) == 1


class TestMARunResult:
    def test_defaults(self):
        r = MARunResult()
        assert r.session_id == ""
        assert r.output_text == ""
        assert r.input_tokens == 0
        assert r.total_cost_usd == 0.0
        assert r.tool_calls == []
        assert r.outcome_result is None
        assert r.error is None

    def test_populated(self):
        r = MARunResult(
            session_id="s_123",
            output_text="Hello",
            input_tokens=100,
            output_tokens=50,
            outcome_result="satisfied",
        )
        assert r.session_id == "s_123"
        assert r.outcome_result == "satisfied"


class TestMAEnvironmentConfig:
    def test_defaults(self):
        cfg = MAEnvironmentConfig(name="test-env")
        assert cfg.networking == "unrestricted"
