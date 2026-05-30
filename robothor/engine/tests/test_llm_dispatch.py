"""Characterization + unit tests for the extracted LLM dispatch/cost layer.

Slice 0 pinned the *current* behavior of these methods while they still lived
on ``AgentRunner``. Slice 1 extracted them into ``llm_client.LLMClient`` — this
file now targets the class directly; the assertions are unchanged, proving the
move is behavior-preserving.

(The sibling ``test_llm_client.py`` covers the older module-level ``llm_call*``
helpers used by auxiliary callers — a separate code path.)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.codex_provider import CodexProviderError
from robothor.engine.llm_client import (
    LLM_REQUEST_TIMEOUT,
    LLM_REQUEST_TIMEOUT_OLLAMA,
    LLMClient,
)


@pytest.fixture
def client() -> LLMClient:
    """A bare LLMClient — stateless across runs, no construction deps."""
    return LLMClient()


def _limits(**overrides):
    """A ModelLimits-like stub for patching get_model_limits."""
    m = MagicMock()
    m.max_input_tokens = overrides.get("max_input_tokens", 200_000)
    m.cache_write_cost_per_token = overrides.get("cache_write_cost_per_token", 0.0)
    m.cache_read_cost_per_token = overrides.get("cache_read_cost_per_token", 0.0)
    m.supports_thinking = overrides.get("supports_thinking", False)
    return m


# ─────────────────────────── cost ───────────────────────────


class TestCalculateCost:
    def test_cache_aware_arithmetic(self, client):
        """Pins the exact cache-aware cost formula."""
        rates = {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}
        with (
            patch.dict("litellm.model_cost", {"test/model": rates}, clear=False),
            patch(
                "robothor.engine.model_registry.get_model_limits",
                return_value=_limits(
                    cache_write_cost_per_token=3e-6,
                    cache_read_cost_per_token=0.5e-6,
                ),
            ),
        ):
            cost = client._calculate_cost(
                "test/model",
                input_tokens=1000,
                output_tokens=500,
                cache_creation_tokens=200,
                cache_read_tokens=100,
            )
        # regular_input = 1000-100=900
        # 900*1e-6 + 500*2e-6 + 200*3e-6 + 100*0.5e-6
        assert cost == pytest.approx(0.00255)

    def test_unknown_model_is_free_without_registry_rates(self, client):
        with (
            patch.dict("litellm.model_cost", {}, clear=False),
            patch(
                "robothor.engine.model_registry.get_model_limits",
                return_value=_limits(),
            ),
        ):
            cost = client._calculate_cost("nope/model", 1000, 500)
        assert cost == 0.0


class TestResponseCost:
    def test_codex_prices_via_registry_with_model_used(self, client):
        """codex/* never hits litellm.completion_cost; priced from registry."""
        with (
            patch("robothor.engine.llm_client.is_codex_model", return_value=True),
            patch.object(client, "_calculate_cost", return_value=0.0) as calc,
        ):
            cost = client._response_cost(
                response=MagicMock(),
                model_used="codex/gpt-5.5",
                models=["codex/gpt-5.5"],
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            )
        assert cost == 0.0
        # Priced against the model that actually answered.
        assert calc.call_args.args[0] == "codex/gpt-5.5"

    def test_non_codex_returns_litellm_cost_when_positive(self, client):
        with (
            patch("robothor.engine.llm_client.is_codex_model", return_value=False),
            patch("robothor.engine.llm_client.litellm.completion_cost", return_value=0.42),
        ):
            cost = client._response_cost(
                response=MagicMock(),
                model_used="openrouter/x/model",
                models=["openrouter/x/model"],
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            )
        assert cost == 0.42

    def test_falls_back_to_calculate_cost_when_litellm_raises(self, client):
        with (
            patch("robothor.engine.llm_client.is_codex_model", return_value=False),
            patch(
                "robothor.engine.llm_client.litellm.completion_cost",
                side_effect=RuntimeError("no pricing"),
            ),
            patch.object(client, "_calculate_cost", return_value=0.99) as calc,
        ):
            cost = client._response_cost(
                response=MagicMock(),
                model_used="openrouter/x/model",
                models=["openrouter/x/model"],
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            )
        assert cost == 0.99
        assert calc.called


# ─────────────────────────── kwargs builder ───────────────────────────


class TestBuildLLMKwargs:
    def _build(self, model, messages, tools=None, *, thinking=False):
        with (
            patch(
                "robothor.engine.model_registry.get_model_limits",
                return_value=_limits(supports_thinking=thinking),
            ),
            patch("robothor.engine.model_registry.get_output_tokens", return_value=4096),
        ):
            return LLMClient._build_llm_kwargs(
                model, messages, tools or [], input_est=1000, temperature=0.3
            )

    def test_anthropic_direct_splits_system_on_time_marker(self):
        sys = "STATIC RULES\n\n---\n\nCurrent time: 2026-05-30"
        kwargs = self._build("anthropic/claude-opus-4-8", [{"role": "system", "content": sys}])
        content = kwargs["messages"][0]["content"]
        assert isinstance(content, list)
        assert len(content) == 2
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        assert content[0]["text"] == "STATIC RULES"
        assert "cache_control" not in content[1]

    def test_anthropic_direct_no_marker_caches_whole_system(self):
        kwargs = self._build("anthropic/claude-opus-4-8", [{"role": "system", "content": "ALL"}])
        content = kwargs["messages"][0]["content"]
        assert isinstance(content, list)
        assert len(content) == 1
        assert content[0]["cache_control"] == {"type": "ephemeral"}

    def test_openrouter_anthropic_pins_provider_and_keeps_string_system(self):
        kwargs = self._build(
            "openrouter/anthropic/claude-sonnet-4-6",
            [{"role": "system", "content": "ALL"}],
        )
        # No content-block conversion for the OpenRouter path.
        assert kwargs["messages"][0]["content"] == "ALL"
        assert kwargs["extra_body"]["provider"]["order"] == ["Anthropic"]
        assert kwargs["extra_body"]["provider"]["allow_fallbacks"] is False

    def test_ollama_uses_long_timeout(self):
        kwargs = self._build("ollama_chat/llama3", [{"role": "user", "content": "hi"}])
        assert kwargs["timeout"] == LLM_REQUEST_TIMEOUT_OLLAMA

    def test_non_ollama_uses_standard_timeout(self):
        kwargs = self._build("openrouter/x/model", [{"role": "user", "content": "hi"}])
        assert kwargs["timeout"] == LLM_REQUEST_TIMEOUT

    def test_tools_set_tool_choice_auto(self):
        kwargs = self._build(
            "openrouter/x/model",
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "t"}}],
        )
        assert kwargs["tool_choice"] == "auto"

    def test_thinking_model_forces_temperature_and_budget(self):
        kwargs = self._build(
            "anthropic/claude-opus-4-8",
            [{"role": "user", "content": "hi"}],
            thinking=True,
        )
        assert kwargs["temperature"] == 1.0
        assert kwargs["thinking"]["type"] == "enabled"


class TestMessageHygiene:
    def test_validate_tool_pairs_drops_orphan_tool_result(self):
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            {"role": "tool", "tool_call_id": "orphan", "content": "lost"},
        ]
        cleaned = LLMClient._validate_tool_pairs(messages)
        ids = [m.get("tool_call_id") for m in cleaned if m["role"] == "tool"]
        assert ids == ["call_1"]

    def test_validate_tool_pairs_noop_without_tool_calls(self):
        messages = [{"role": "user", "content": "hi"}]
        assert LLMClient._validate_tool_pairs(messages) is messages

    def test_guard_trailing_assistant_drops_trailing_assistant(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "orphan"},
        ]
        guarded = LLMClient._guard_trailing_assistant(messages)
        assert len(guarded) == 1
        assert guarded[-1]["role"] == "user"

    def test_guard_trailing_assistant_noop_when_user_last(self):
        messages = [{"role": "user", "content": "hi"}]
        assert LLMClient._guard_trailing_assistant(messages) is messages


# ─────────────────────────── model error handling ───────────────────────────


class TestHandleModelError:
    @pytest.mark.parametrize("status", [401, 402, 403, 429, 500, 502, 503, 504])
    def test_http_errors_mark_broken(self, status):
        broken: set[str] = set()
        err = Exception("boom")
        err.status_code = status
        LLMClient._handle_model_error(err, "m1", broken)
        assert "m1" in broken

    def test_timeout_marks_broken(self):
        broken: set[str] = set()
        LLMClient._handle_model_error(TimeoutError("slow"), "m1", broken)
        assert "m1" in broken

    def test_codex_provider_error_marks_broken(self):
        broken: set[str] = set()
        LLMClient._handle_model_error(CodexProviderError("no cli"), "codex/gpt-5.5", broken)
        assert "codex/gpt-5.5" in broken

    def test_file_not_found_marks_broken_for_codex_only(self):
        broken: set[str] = set()
        LLMClient._handle_model_error(FileNotFoundError(), "codex/gpt-5.5", broken)
        assert "codex/gpt-5.5" in broken

    def test_file_not_found_does_not_mark_broken_for_non_codex(self):
        broken: set[str] = set()
        LLMClient._handle_model_error(FileNotFoundError(), "openrouter/x/model", broken)
        assert "openrouter/x/model" not in broken

    def test_unlisted_status_does_not_mark_broken(self):
        broken: set[str] = set()
        err = Exception("bad request")
        err.status_code = 400
        LLMClient._handle_model_error(err, "m1", broken)
        assert "m1" not in broken

    def test_primary_failure_logs_error(self, caplog):
        broken: set[str] = set()
        with caplog.at_level("ERROR"):
            LLMClient._handle_model_error(TimeoutError(), "primary", broken)
        assert any("PRIMARY model" in r.message for r in caplog.records)


# ─────────────────────────── fallback dispatch ───────────────────────────


class TestCallLLMFallback:
    @pytest.mark.asyncio
    async def test_skips_broken_and_returns_first_success(self, client):
        ok = MagicMock()
        acompletion = AsyncMock(return_value=ok)
        with (
            patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
            patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
        ):
            result = await client._call_llm(
                [{"role": "user", "content": "hi"}],
                ["openrouter/broken", "openrouter/good"],
                [],
                broken_models={"openrouter/broken"},
            )
        assert result is ok
        # Only the non-broken model was actually called.
        assert acompletion.call_count == 1
        assert acompletion.call_args.kwargs["model"] == "openrouter/good"

    @pytest.mark.asyncio
    async def test_returns_none_when_all_models_fail(self, client):
        with (
            patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
            patch(
                "robothor.engine.llm_client.litellm.acompletion",
                new=AsyncMock(side_effect=RuntimeError("down")),
            ),
        ):
            result = await client._call_llm(
                [{"role": "user", "content": "hi"}],
                ["openrouter/a", "openrouter/b"],
                [],
                broken_models=set(),
            )
        assert result is None
