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


class _Answer:
    """The shape ``_call_llm`` returns on success — enough for the caller."""

    def __init__(self, text: str = "still here") -> None:
        message = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None)
        self.choices = [SimpleNamespace(message=message, finish_reason="stop")]
        self.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=2)
        self.model = LOCAL


@pytest.fixture
def local_server(monkeypatch):
    """The local server carries the chain's model. Nothing else is faked."""
    monkeypatch.setattr(last_resort, "local_base_url", lambda: "http://local")
    monkeypatch.setattr(last_resort, "list_local_models", _async_return({"qwen3.8:27b"}))


@pytest.fixture
def dead_local_server(monkeypatch):
    """`/api/tags` answers nothing: the server is down or has not pulled it."""
    monkeypatch.setattr(last_resort, "local_base_url", lambda: "http://local")
    monkeypatch.setattr(last_resort, "list_local_models", _async_return(set()))


@pytest.fixture
def open_breaker(monkeypatch):
    """The local model's breaker is open — three failures and a 600s cooldown.

    THE configuration the last resort exists for, and the one its first tests
    could not see: they drove a hand-written client whose ``_call_llm``
    answered unconditionally, so the real admission path — ``_skip_model_reason``
    and the breaker — was never on the route. With the breaker open the control
    made ZERO calls while logging and telling the operator that it had tried.
    """
    from robothor.engine import llm_client, model_breaker

    breaker = model_breaker.ModelBreaker(on_open=None)
    for _ in range(5):
        breaker.record_failure(LOCAL, "probe")
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: breaker)
    monkeypatch.setattr(model_breaker, "get_model_breaker", lambda: breaker)
    assert breaker.is_open(LOCAL)
    return breaker


@pytest.fixture
def dialled(monkeypatch):
    """Records every request that actually reaches the provider."""
    from unittest.mock import AsyncMock, patch

    calls: list[dict] = []

    async def _acompletion(**kwargs):
        calls.append(kwargs)
        return _Answer()

    patcher = patch(
        "robothor.engine.llm_client.litellm.acompletion", AsyncMock(side_effect=_acompletion)
    )
    patcher.start()
    monkeypatch.setattr("robothor.engine.llm_client.asyncio.sleep", AsyncMock())
    yield calls
    patcher.stop()


def _client():
    from robothor.engine.llm_client import LLMClient

    return LLMClient()


class TestTheLastMinimalAttempt:
    async def test_an_open_breaker_does_not_stop_it(self, local_server, open_breaker, dialled):
        """It is by definition the last thing tried; a cooldown is advice."""
        session = SimpleNamespace(messages=_conversation())

        result = await last_resort_attempt(_client(), session, [CLOUD, LOCAL])

        assert dialled, "the last minimal attempt never dialled the model"
        assert dialled[0]["model"] == LOCAL
        assert result is not None
        assert last_resort.local_state() == "answered"

    async def test_it_asks_the_short_question(self, local_server, dialled):
        session = SimpleNamespace(messages=_conversation())

        await last_resort_attempt(_client(), session, [CLOUD, LOCAL])

        sent = dialled[0]["messages"]
        assert not any(m.get("role") == "tool" for m in sent)
        assert sent[-1]["role"] == "user"

    async def test_the_breaker_still_guards_the_ordinary_path(self, open_breaker, dialled):
        """The bypass is for the last resort only, not a fleet-wide amnesty."""
        result = await _client()._call_llm(
            [{"role": "user", "content": "hi"}], [LOCAL], [], broken_models=set()
        )

        assert result is None
        assert dialled == [], "an open breaker must still skip a model on the normal path"

    async def test_a_server_that_does_not_carry_it_is_not_an_attempt(
        self, dead_local_server, dialled
    ):
        """ "Unreachable" and "tried and failed" have opposite remedies."""
        result = await last_resort_attempt(_client(), SimpleNamespace(messages=[]), [LOCAL])

        assert result is None
        assert dialled == []
        assert last_resort.local_state() == "unreachable"
        assert "did not answer" in str(last_resort.all_models_failed_error([LOCAL], set()))

    async def test_a_dialled_model_that_answers_nothing_says_so(self, local_server, monkeypatch):
        from unittest.mock import AsyncMock, patch

        monkeypatch.setattr("robothor.engine.llm_client.asyncio.sleep", AsyncMock())
        boom = AsyncMock(side_effect=RuntimeError("the model fell over"))
        with patch("robothor.engine.llm_client.litellm.acompletion", boom):
            result = await last_resort_attempt(_client(), SimpleNamespace(messages=[]), [LOCAL])

        assert result is None
        assert boom.called, "the attempt has to be real for the sentence to be true"
        assert last_resort.local_state() == "answered_none"

    async def test_it_does_not_dial_anything_when_there_is_no_local_tier(self, dialled):
        result = await last_resort_attempt(_client(), SimpleNamespace(messages=[]), [CLOUD])

        assert result is None and dialled == []
        assert last_resort.local_state() == "absent"

    async def test_a_benchmark_child_does_not_queue_a_generation(
        self, local_server, dialled, monkeypatch
    ):
        """A grader is waiting, not a person — and N failing cases at once
        would queue N generations on a tier that serves a few at a time."""
        monkeypatch.setattr("robothor.engine.run_context.in_benchmark_run", lambda: True)

        result = await last_resort_attempt(_client(), SimpleNamespace(messages=[]), [LOCAL])

        assert result is None and dialled == []

    async def test_it_never_raises(self, local_server, monkeypatch):
        """This runs while the caller is already handling a failure."""

        class _Broken:
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
