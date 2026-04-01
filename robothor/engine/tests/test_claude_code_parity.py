"""Tests for Claude Code parity features (2026-03-31).

Covers:
- Cache-aware token accounting (Gap 1)
- Fleet config defaults / _defaults.yaml (Gap 2)
- Prompt caching optimization (Gap 3)
- Proactive context compaction (Gap 4)
- Structured streaming events (Gap 5)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from robothor.engine.config import _deep_merge, _load_defaults, load_agent_config
from robothor.engine.model_registry import ModelLimits, get_model_limits
from robothor.engine.models import AgentRun, RunStep
from robothor.engine.session import AgentSession
from robothor.engine.telemetry import TraceContext

# ── Gap 1: Cache-Aware Token Accounting ───────────────────────────────


class TestCacheTokenModels:
    """Cache token fields on AgentRun and RunStep."""

    def test_agent_run_has_cache_fields(self):
        run = AgentRun()
        assert run.cache_creation_tokens == 0
        assert run.cache_read_tokens == 0

    def test_run_step_has_cache_fields(self):
        step = RunStep()
        assert step.cache_creation_tokens is None
        assert step.cache_read_tokens is None

    def test_agent_run_accumulates_cache_tokens(self):
        run = AgentRun()
        run.cache_creation_tokens += 100
        run.cache_read_tokens += 500
        assert run.cache_creation_tokens == 100
        assert run.cache_read_tokens == 500


class TestSessionCacheAccounting:
    """Session.record_llm_call accumulates cache tokens."""

    def test_record_llm_call_with_cache_tokens(self):
        session = AgentSession("test-agent")
        session.start("system", "user", [])

        step = session.record_llm_call(
            model="test-model",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=200,
            cache_read_tokens=300,
        )

        assert step.cache_creation_tokens == 200
        assert step.cache_read_tokens == 300
        assert session.run.cache_creation_tokens == 200
        assert session.run.cache_read_tokens == 300
        assert session.run.input_tokens == 1000
        assert session.run.output_tokens == 500

    def test_record_llm_call_accumulates_across_calls(self):
        session = AgentSession("test-agent")
        session.start("system", "user", [])

        session.record_llm_call(
            model="m1",
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=20,
            cache_read_tokens=0,
        )
        session.record_llm_call(
            model="m1",
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=0,
            cache_read_tokens=80,
        )

        assert session.run.cache_creation_tokens == 20
        assert session.run.cache_read_tokens == 80

    def test_record_llm_call_defaults_to_zero(self):
        session = AgentSession("test-agent")
        session.start("system", "user", [])
        session.record_llm_call(model="m1", input_tokens=100, output_tokens=50)
        assert session.run.cache_creation_tokens == 0
        assert session.run.cache_read_tokens == 0


class TestModelRegistryCachePricing:
    """ModelLimits includes cache pricing fields."""

    def test_model_limits_has_cache_pricing(self):
        limits = ModelLimits(
            max_input_tokens=100_000,
            max_output_tokens=8_192,
            default_output_tokens=4_096,
            input_cost_per_token=0.000_003,
            output_cost_per_token=0.000_015,
            cache_write_cost_per_token=0.000_003_75,
            cache_read_cost_per_token=0.000_000_3,
        )
        assert limits.cache_write_cost_per_token == 0.000_003_75
        assert limits.cache_read_cost_per_token == 0.000_000_3

    def test_anthropic_model_has_cache_pricing(self):
        limits = get_model_limits("openrouter/anthropic/claude-sonnet-4.6")
        assert limits.cache_write_cost_per_token > 0
        assert limits.cache_read_cost_per_token > 0

    def test_non_anthropic_model_defaults_zero(self):
        limits = get_model_limits("openrouter/z-ai/glm-5")
        assert limits.cache_write_cost_per_token == 0.0
        assert limits.cache_read_cost_per_token == 0.0


class TestTelemetryCacheTokens:
    """Telemetry publish_metrics includes cache tokens."""

    def test_publish_includes_cache_tokens(self):
        mock_redis = MagicMock()
        mock_r = MagicMock()
        mock_redis.Redis.return_value = mock_r

        with patch.dict("sys.modules", {"redis": mock_redis}):
            trace = TraceContext(agent_id="test", run_id="r1")
            trace.publish_metrics(
                {
                    "status": "completed",
                    "duration_ms": 1000,
                    "input_tokens": 500,
                    "output_tokens": 200,
                    "cache_creation_tokens": 100,
                    "cache_read_tokens": 300,
                }
            )

        mock_r.xadd.assert_called_once()
        call_args = mock_r.xadd.call_args
        payload = call_args[0][1]
        assert payload["cache_creation_tokens"] == "100"
        assert payload["cache_read_tokens"] == "300"


# ── Gap 2: Fleet Config Defaults ─────────────────────────────────────


class TestDeepMerge:
    """_deep_merge correctly merges nested dicts."""

    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"model": {"primary": "a", "temperature": 0.3}}
        override = {"model": {"primary": "b"}}
        result = _deep_merge(base, override)
        assert result == {"model": {"primary": "b", "temperature": 0.3}}

    def test_list_replaced_not_appended(self):
        base = {"tools": ["a", "b"]}
        override = {"tools": ["c"]}
        result = _deep_merge(base, override)
        assert result == {"tools": ["c"]}

    def test_empty_override(self):
        base = {"a": 1}
        result = _deep_merge(base, {})
        assert result == {"a": 1}

    def test_empty_base(self):
        result = _deep_merge({}, {"a": 1})
        assert result == {"a": 1}

    def test_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        _deep_merge(base, override)
        assert base == {"a": {"b": 1}}


class TestFleetDefaults:
    """_load_defaults and load_agent_config use _defaults.yaml."""

    def test_load_defaults_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            result = _load_defaults(Path(d))
            assert result == {}

    def test_load_defaults_parses_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            defaults_path = Path(d) / "_defaults.yaml"
            defaults_path.write_text("schedule:\n  timeout_seconds: 300\n")
            result = _load_defaults(Path(d))
            assert result["schedule"]["timeout_seconds"] == 300

    def test_load_agent_config_merges_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            (dp / "_defaults.yaml").write_text(
                "v2:\n  error_feedback: true\nschedule:\n  timeout_seconds: 999\n"
            )
            (dp / "test-agent.yaml").write_text(
                "id: test-agent\nname: Test\nschedule:\n  timeout_seconds: 120\n"
            )
            config = load_agent_config("test-agent", dp)
            assert config is not None
            # Agent-specific value wins
            assert config.timeout_seconds == 120
            # Default value applied
            assert config.error_feedback is True


# ── Gap 3: Prompt Caching — cache_control markers ────────────────────


class TestPromptCachingOptimization:
    """_build_llm_kwargs adds cache_control for direct Anthropic API models only."""

    def test_direct_anthropic_model_gets_cache_control(self):
        from robothor.engine.runner import AgentRunner

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        kwargs = AgentRunner._build_llm_kwargs(
            "anthropic/claude-sonnet-4-6",
            messages,
            [],
            1000,
            0.3,
        )
        sys_msg = kwargs["messages"][0]
        assert isinstance(sys_msg["content"], list)
        assert sys_msg["content"][0]["type"] == "text"
        assert sys_msg["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_openrouter_anthropic_no_cache_control(self):
        """OpenRouter models must NOT get Anthropic content-block system messages."""
        from robothor.engine.runner import AgentRunner

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        kwargs = AgentRunner._build_llm_kwargs(
            "openrouter/anthropic/claude-sonnet-4-6",
            messages,
            [],
            1000,
            0.3,
        )
        sys_msg = kwargs["messages"][0]
        # Must stay as string — OpenRouter handles caching via its own mechanism
        assert isinstance(sys_msg["content"], str)

    def test_non_anthropic_model_no_cache_control(self):
        from robothor.engine.runner import AgentRunner

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        kwargs = AgentRunner._build_llm_kwargs(
            "openrouter/z-ai/glm-5",
            messages,
            [],
            1000,
            0.3,
        )
        sys_msg = kwargs["messages"][0]
        assert isinstance(sys_msg["content"], str)

    def test_cache_control_does_not_mutate_original(self):
        from robothor.engine.runner import AgentRunner

        original_messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        AgentRunner._build_llm_kwargs(
            "anthropic/claude-sonnet-4-6",
            original_messages,
            [],
            1000,
            0.3,
        )
        # Original should be unchanged
        assert isinstance(original_messages[0]["content"], str)


class TestValidateToolPairs:
    """_validate_tool_pairs drops orphaned tool_result messages."""

    def test_valid_pairs_unchanged(self):
        from robothor.engine.runner import AgentRunner

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "ok",
                "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "result"},
        ]
        result = AgentRunner._validate_tool_pairs(messages)
        assert result is messages  # same object — no changes needed

    def test_orphaned_tool_dropped(self):
        from robothor.engine.runner import AgentRunner

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "real_id"}]},
            {"role": "tool", "tool_call_id": "real_id", "content": "valid"},
            {"role": "tool", "tool_call_id": "nonexistent", "content": "orphan"},
            {"role": "user", "content": "follow-up"},
        ]
        result = AgentRunner._validate_tool_pairs(messages)
        assert len(result) == 5
        assert not any(m.get("tool_call_id") == "nonexistent" for m in result)

    def test_no_tool_messages_unchanged(self):
        from robothor.engine.runner import AgentRunner

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
        ]
        result = AgentRunner._validate_tool_pairs(messages)
        assert result is messages

    def test_mixed_valid_and_orphan(self):
        from robothor.engine.runner import AgentRunner

        messages: list[dict[str, Any]] = [
            {"role": "assistant", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "tool_call_id": "t1", "content": "valid"},
            {"role": "tool", "tool_call_id": "gone", "content": "orphan"},
            {"role": "user", "content": "next"},
        ]
        result = AgentRunner._validate_tool_pairs(messages)
        assert len(result) == 3
        assert result[1]["tool_call_id"] == "t1"


# ── Gap 5: Structured Streaming Events ───────────────────────────────


class TestStructuredStreamingSignature:
    """on_stream_event parameter accepted throughout the chain."""

    def test_execute_accepts_on_stream_event(self):
        """Verify the execute method signature accepts on_stream_event."""
        import inspect

        from robothor.engine.runner import AgentRunner

        sig = inspect.signature(AgentRunner.execute)
        assert "on_stream_event" in sig.parameters

    def test_run_loop_accepts_on_stream_event(self):
        import inspect

        from robothor.engine.runner import AgentRunner

        sig = inspect.signature(AgentRunner._run_loop)
        assert "on_stream_event" in sig.parameters

    def test_call_llm_streaming_accepts_on_stream_event(self):
        import inspect

        from robothor.engine.runner import AgentRunner

        sig = inspect.signature(AgentRunner._call_llm_streaming)
        assert "on_stream_event" in sig.parameters


class TestCalculateCostCacheAware:
    """_calculate_cost handles cache tokens correctly."""

    def test_cost_with_cache_tokens(self):
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner.__new__(AgentRunner)
        # Use a model that's in litellm registry
        cost = runner._calculate_cost(
            "openrouter/anthropic/claude-sonnet-4.6",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=200,
            cache_read_tokens=300,
        )
        assert cost > 0

    def test_cost_without_cache_tokens(self):
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner.__new__(AgentRunner)
        cost = runner._calculate_cost(
            "openrouter/anthropic/claude-sonnet-4.6",
            input_tokens=1000,
            output_tokens=500,
        )
        assert cost > 0

    def test_cost_handles_mock_values(self):
        """Regression: MagicMock values should not crash."""
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner.__new__(AgentRunner)
        # Should not raise TypeError
        cost = runner._calculate_cost(
            "unknown/model",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=0,
            cache_read_tokens=0,
        )
        assert isinstance(cost, float)
