"""The grading layer goes dark in exactly the outage it should be measuring.

`judge_agent_run` and Buddy's review both call `llm_call` with a single
hardcoded model and swallow the failure into `None`. When the instance's
OpenRouter key hit its cap, that produced 90 judge failures and 59 Buddy
failures in one hour, silently: `goal_achievement` carries weight 3.0 in
main's manifest and simply had no data.

The agent loop, compaction and the planner were all given the chain during
the 2026-08-26 sweep. These two were missed — same defect, same family:
built, correct, and unable to reach the tier that was still answering.

`llm_call` is single-model by contract, so the chain has to go through it.
Only two call sites exist, both fixed here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.llm_client import chain_with_last_resort, llm_call


def _response(text: str = "ok"):
    class _Msg:
        content = text

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]
        usage = None
        model = "stub"

    return _Resp()


class TestChainWithLastResort:
    def test_appends_the_offline_tier(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/qwen3.8:27b")
        assert chain_with_last_resort("openrouter/x/y") == [
            "openrouter/x/y",
            "ollama_chat/qwen3.8:27b",
        ]

    def test_unset_changes_nothing(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_LAST_RESORT_MODEL", raising=False)
        assert chain_with_last_resort("openrouter/x/y") == ["openrouter/x/y"]

    def test_never_duplicates_the_model(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/q")
        assert chain_with_last_resort("ollama_chat/q") == ["ollama_chat/q"]


class TestLlmCallWalksAChain:
    @pytest.mark.asyncio
    async def test_a_single_model_string_is_unchanged(self):
        with patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(return_value=_response()),
        ) as ac:
            await llm_call([{"role": "user", "content": "hi"}], model="m1", max_retries=1)
        assert ac.await_args.kwargs["model"] == "m1"

    @pytest.mark.asyncio
    async def test_walks_to_the_next_model_when_the_first_fails(self):
        calls: list[str] = []

        async def _fake(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "dead":
                raise RuntimeError("Key limit exceeded")
            return _response()

        with patch("robothor.engine.llm_client.litellm.acompletion", new=_fake):
            resp = await llm_call(
                [{"role": "user", "content": "hi"}], model=["dead", "alive"], max_retries=1
            )
        assert calls == ["dead", "alive"]
        assert resp.choices[0].message.content == "ok"

    @pytest.mark.asyncio
    async def test_raises_the_last_failure_when_every_model_fails(self):
        async def _fake(**kwargs):
            raise RuntimeError(f"no: {kwargs['model']}")

        with (
            patch("robothor.engine.llm_client.litellm.acompletion", new=_fake),
            pytest.raises(RuntimeError, match="no: b"),
        ):
            await llm_call([{"role": "user", "content": "hi"}], model=["a", "b"], max_retries=1)

    @pytest.mark.asyncio
    async def test_an_empty_chain_is_an_error_not_a_silent_none(self):
        with pytest.raises(ValueError, match="no model"):
            await llm_call([{"role": "user", "content": "hi"}], model=[])


class TestJudgeReachesTheOfflineTier:
    @pytest.mark.asyncio
    async def test_judge_falls_back_instead_of_abstaining(self, monkeypatch):
        """The incident: the primary is capped, the local tier is answering."""
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/local")
        seen: list[str] = []

        async def _fake(**kwargs):
            seen.append(kwargs["model"])
            if kwargs["model"].startswith("openrouter/"):
                raise RuntimeError("Key limit exceeded")
            return _response('{"score": 3, "rationale": "fine"}')

        from robothor.engine import judge

        with patch("robothor.engine.llm_client.litellm.acompletion", new=_fake):
            chain = judge.judge_chain()
        assert chain[0].startswith("openrouter/")
        assert chain[-1] == "ollama_chat/local"

    def test_buddy_review_model_carries_the_chain(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/local")
        from robothor.engine import buddy_critic

        chain = buddy_critic.review_chain()
        assert isinstance(chain, list)
        assert chain[-1] == "ollama_chat/local"
