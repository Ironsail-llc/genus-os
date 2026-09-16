"""The last thing tried, and the last thing said.

On 2026-09-16 a run ended with ``Failure: error. All models failed to
respond.`` in the operator's chat while a local model that had answered twelve
steps of that same run sat idle on the same box. Two separate failures:
nothing tried the local tier one more time with a conversation small enough to
fit it, and the sentence the operator read was a Python repr.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from robothor.engine import last_resort
from robothor.engine.last_resort import (
    AllModelsFailedError,
    all_models_failed,
    all_models_failed_error,
    bare_model_name,
    last_resort_attempt,
    minimal_messages,
    reachable_local_model,
)

LOCAL = "ollama_chat/qwen3.8:27b"
CLOUD = "openrouter/deepseek/deepseek-v4.1-flash"


def _conversation() -> list[dict]:
    return [
        {"role": "system", "content": "You are the main agent."},
        {"role": "user", "content": "File tomorrow's tasks."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 400_000},
        {"role": "user", "content": "Well?"},
        {"role": "developer", "content": "[SYSTEM] check-in"},
    ]


class TestTheMinimalConversation:
    def test_it_keeps_the_head_and_the_last_user_turn(self):
        minimal = minimal_messages(_conversation())
        assert minimal[0]["content"] == "You are the main agent."
        assert minimal[-1]["content"] == "Well?"

    def test_it_leaves_the_tool_traffic_behind(self):
        assert not any(m.get("role") == "tool" for m in minimal_messages(_conversation()))

    def test_it_always_ends_with_a_user_turn(self):
        """Ollama's refusal IS a conversation with no user turn in it."""
        minimal = minimal_messages([{"role": "system", "content": "s"}])
        assert minimal[-1]["role"] == "user"

    def test_an_empty_conversation_is_empty(self):
        assert minimal_messages([]) == []


class TestFindingTheLocalModel:
    def test_the_bare_name_is_what_the_server_calls_it(self):
        assert bare_model_name(LOCAL) == "qwen3.8:27b"

    async def test_a_chain_with_no_local_model_finds_nothing(self):
        assert await reachable_local_model([CLOUD]) is None

    async def test_a_server_that_does_not_carry_it_finds_nothing(self, monkeypatch):
        """The 2026-08-24 defect: a chain naming a model nobody pulled."""
        monkeypatch.setattr(last_resort, "local_base_url", lambda: "http://local")
        monkeypatch.setattr(
            last_resort, "list_local_models", _async_return({"some-other-model:latest"})
        )
        assert await reachable_local_model([CLOUD, LOCAL]) is None

    async def test_a_down_server_finds_nothing(self, monkeypatch):
        monkeypatch.setattr(last_resort, "local_base_url", lambda: "http://local")
        monkeypatch.setattr(last_resort, "list_local_models", _async_return(set()))
        assert await reachable_local_model([CLOUD, LOCAL]) is None

    async def test_a_carried_model_is_found(self, monkeypatch):
        monkeypatch.setattr(last_resort, "local_base_url", lambda: "http://local")
        monkeypatch.setattr(last_resort, "list_local_models", _async_return({"qwen3.8:27b"}))
        assert await reachable_local_model([CLOUD, LOCAL]) == LOCAL


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value

    return _fn


class _Client:
    """Just enough LLMClient to see what the last resort asks for."""

    def __init__(self, answer=None):
        self.answer = answer
        self.seen: list[list[dict]] = []

    async def _call_llm(self, messages, models, tools, broken_models=None, **kwargs):
        self.seen.append(messages)
        self.models = models
        return self.answer


class TestTheLastMinimalAttempt:
    async def test_it_asks_the_local_model_the_short_question(self, monkeypatch):
        monkeypatch.setattr(last_resort, "local_base_url", lambda: "http://local")
        monkeypatch.setattr(last_resort, "list_local_models", _async_return({"qwen3.8:27b"}))
        client = _Client(answer="an answer")
        session = SimpleNamespace(messages=_conversation())

        result = await last_resort_attempt(client, session, [CLOUD, LOCAL])

        assert result == "an answer"
        assert client.models == [LOCAL]
        assert not any(m.get("role") == "tool" for m in client.seen[0])

    async def test_it_does_not_dial_anything_when_there_is_no_local_tier(self):
        client = _Client()
        result = await last_resort_attempt(client, SimpleNamespace(messages=[]), [CLOUD])
        assert result is None and client.seen == []

    async def test_it_never_raises(self, monkeypatch):
        """This runs while the caller is already handling a failure."""
        monkeypatch.setattr(last_resort, "local_base_url", lambda: "http://local")
        monkeypatch.setattr(last_resort, "list_local_models", _async_return({"qwen3.8:27b"}))

        class _Broken(_Client):
            async def _call_llm(self, *args, **kwargs):
                raise RuntimeError("the server died mid-sentence")

        assert await last_resort_attempt(_Broken(), SimpleNamespace(messages=[]), [LOCAL]) is None


class TestWhatTheOperatorReads:
    @pytest.fixture
    def spent_pool(self, monkeypatch):
        from robothor.engine import key_pool

        pool = key_pool.KeyPool(["sk-test"])
        pool.retire("sk-test", key_pool.Retirement.QUOTA_EXHAUSTED_PERIODIC)
        monkeypatch.setattr(key_pool, "_SHARED", {"OPENROUTER_API_KEY": pool})

    def test_it_is_never_the_old_repr(self, spent_pool):
        text = str(all_models_failed_error([CLOUD, LOCAL], set(), local_state="answered_none"))
        assert "All models failed to respond" not in text

    def test_it_names_the_provider_and_the_remedy(self, spent_pool):
        text = str(all_models_failed_error([CLOUD, LOCAL], set(), local_state="answered_none"))
        assert CLOUD in text
        assert "genus secrets reload" in text, "a page without a remedy is noise"

    def test_it_says_what_the_local_tier_did(self, spent_pool):
        reachable = str(all_models_failed_error([CLOUD, LOCAL], set(), local_state="unreachable"))
        assert "local fallback is configured" in reachable
        absent = str(all_models_failed_error([CLOUD], set(), local_state="absent"))
        assert "no local fallback" in absent

    def test_a_model_broken_in_this_run_says_so(self):
        text = str(all_models_failed_error([CLOUD], {CLOUD}, local_state="absent"))
        assert "failed earlier in this run" in text

    def test_it_records_the_error_on_the_run(self):
        recorded: list[str] = []
        session = SimpleNamespace(record_error=recorded.append)
        error = all_models_failed(session, [CLOUD], set())
        assert recorded == ["All models failed"]
        assert isinstance(error, AllModelsFailedError)

    def test_it_is_still_a_runtime_error(self):
        """Callers catch RuntimeError around a run; that must keep working."""
        assert issubclass(AllModelsFailedError, RuntimeError)
