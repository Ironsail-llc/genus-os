"""Tests for the Codex subscription-backed provider path."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import codex_provider
from robothor.engine.codex_provider import (
    CodexProviderError,
    codex_model_name,
    is_codex_model,
)
from robothor.engine.runner import AgentRunner

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig


def _response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.choices[0].message.tool_calls = None
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    return resp


def test_codex_model_detection_and_mapping() -> None:
    assert is_codex_model("codex/gpt-5.5")
    assert not is_codex_model("openai/gpt-5.5")
    assert not is_codex_model("openrouter/openai/gpt-5.5")
    assert codex_model_name("codex/gpt-5.5") == "gpt-5.5"


def test_codex_env_removes_usage_based_openai_api_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-test")
    monkeypatch.setenv("OPENAI_PROJECT", "proj-test")
    monkeypatch.setenv("ROBOTHOR_CODEX_HOME", "/tmp/genus-codex-home")

    env = codex_provider._codex_env()

    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env
    assert "OPENAI_ORG_ID" not in env
    assert "OPENAI_PROJECT" not in env
    assert env["CODEX_HOME"] == "/tmp/genus-codex-home"


@pytest.mark.asyncio
async def test_ensure_chatgpt_login_accepts_subscription_status() -> None:
    with patch(
        "robothor.engine.codex_provider.login_status", return_value="Logged in using ChatGPT"
    ):
        await codex_provider.ensure_chatgpt_login()


@pytest.mark.asyncio
async def test_ensure_chatgpt_login_rejects_non_subscription_status() -> None:
    with patch(
        "robothor.engine.codex_provider.login_status", return_value="Logged in using API key"
    ):
        with pytest.raises(CodexProviderError, match="ChatGPT subscription auth"):
            await codex_provider.ensure_chatgpt_login()


@pytest.mark.asyncio
async def test_codex_provider_returns_openai_style_tool_calls() -> None:
    with (
        patch("robothor.engine.codex_provider.ensure_chatgpt_login", new=AsyncMock()),
        patch(
            "robothor.engine.codex_provider._run_codex_exec",
            new=AsyncMock(
                return_value=(
                    '{"type":"tool_calls","content":"","tool_calls":['
                    '{"name":"search_memory","arguments_json":"{\\"query\\":\\"hello\\"}"}]}'
                )
            ),
        ) as run_codex,
    ):
        response = await codex_provider.acompletion(
            model="codex/gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "search_memory",
                        "description": "Search memory",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )

    tool_call = response.choices[0].message.tool_calls[0]
    assert response.choices[0].finish_reason == "tool_calls"
    assert tool_call.id == "codex_tool_1"
    assert tool_call.function.name == "search_memory"
    assert tool_call.function.arguments == '{"query":"hello"}'
    assert run_codex.call_args.kwargs["output_schema"]["required"] == [
        "type",
        "content",
        "tool_calls",
    ]


@pytest.mark.asyncio
async def test_runner_uses_codex_for_tool_turns(
    engine_config: EngineConfig,
) -> None:
    runner = AgentRunner(engine_config)
    messages = [{"role": "user", "content": "Use a tool"}]
    tools = [{"type": "function", "function": {"name": "search_memory"}}]
    codex_response = _response("")
    codex_response.choices[0].message.tool_calls = [
        SimpleNamespace(
            id="codex_tool_1",
            function=SimpleNamespace(name="search_memory", arguments='{"query":"hello"}'),
        )
    ]

    with (
        patch(
            "robothor.engine.runner.codex_acompletion",
            new=AsyncMock(return_value=codex_response),
        ) as codex_call,
        patch("litellm.acompletion", new=AsyncMock()) as litellm_call,
        patch.object(runner, "_prepare_llm_call", new=AsyncMock(return_value=100)),
    ):
        result = await runner._call_llm(
            messages,
            ["codex/gpt-5.5", "openrouter/xiaomi/mimo-v2.5-pro"],
            tools,
            broken_models=set(),
        )

    assert result is codex_response
    codex_call.assert_awaited_once()
    litellm_call.assert_not_called()


@pytest.mark.asyncio
async def test_runner_uses_codex_for_text_only_turns(engine_config: EngineConfig) -> None:
    runner = AgentRunner(engine_config)
    codex_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="codex ok", tool_calls=None),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
    )

    with (
        patch(
            "robothor.engine.runner.codex_acompletion", new=AsyncMock(return_value=codex_response)
        ) as codex_call,
        patch("litellm.acompletion", new=AsyncMock()) as litellm_call,
        patch.object(runner, "_prepare_llm_call", new=AsyncMock(return_value=100)),
    ):
        result = await runner._call_llm(
            [{"role": "user", "content": "hi"}],
            ["codex/gpt-5.5"],
            [],
            broken_models=set(),
        )

    assert result is codex_response
    codex_call.assert_awaited_once()
    litellm_call.assert_not_called()
