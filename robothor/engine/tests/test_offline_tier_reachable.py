"""Every path that calls a model must be able to reach the offline tier.

On 2026-08-26 a capped OpenRouter key took the fleet down while a local Qwen
sat at the end of every chain, up and answering. #415 fixed the main call
path. These are the paths it did not fix — each one picks a model without
walking the chain, so during the outage the tier exists for, they still call
the dead primary and quietly degrade.

Quietly is the operative word: compaction swallows its failure and returns
empty (the agent's context is replaced by a placeholder with zero retained
facts) and the planner logs at DEBUG and returns None.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import compaction

CHAIN = ["openrouter/dead-primary", "ollama_chat/qwen3.8:27b"]


def _fail_then_succeed(payload: str):
    """First model raises as a capped provider does; the local tier answers."""
    calls: list[str] = []

    async def side_effect(**kw):
        calls.append(kw["model"])
        if kw["model"] != "ollama_chat/qwen3.8:27b":
            raise Exception("Key limit exceeded (weekly limit)")

        class _Msg:
            content = payload

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    return side_effect, calls


@pytest.mark.asyncio
async def test_fact_extraction_walks_to_the_local_tier():
    """Otherwise every extraction returns [] and the context loses its facts."""
    side_effect, calls = _fail_then_succeed(
        '{"facts":[{"text":"a fact","category":"context","priority":3}]}'
    )
    with patch("litellm.acompletion", AsyncMock(side_effect=side_effect)):
        facts = await compaction.extract_facts(
            [{"role": "user", "content": "hello there"}], model=CHAIN
        )

    assert calls == CHAIN, "the chain was not walked"
    assert [f.text for f in facts] == ["a fact"]


@pytest.mark.asyncio
async def test_segment_summary_walks_to_the_local_tier():
    """Otherwise the segment becomes a bare '[Segment: N messages]' placeholder."""
    side_effect, calls = _fail_then_succeed("a real summary")
    with patch("litellm.acompletion", AsyncMock(side_effect=side_effect)):
        summary = await compaction.summarize_segment(
            [{"role": "user", "content": "hello there"}], model=CHAIN
        )

    assert calls == CHAIN
    assert summary == "a real summary"


@pytest.mark.asyncio
async def test_a_single_model_string_still_works():
    """The existing callers pass one model; they must keep working."""
    side_effect, calls = _fail_then_succeed("summary")
    with patch("litellm.acompletion", AsyncMock(side_effect=side_effect)):
        out = await compaction.summarize_segment(
            [{"role": "user", "content": "hi there"}], model="ollama_chat/qwen3.8:27b"
        )
    assert out == "summary"
    assert calls == ["ollama_chat/qwen3.8:27b"]


@pytest.mark.asyncio
async def test_compact_hands_the_whole_chain_down_not_just_the_primary():
    """compact() picked models[0] with no awareness of the rest."""
    seen: dict[str, object] = {}

    async def fake_extract(messages, model):
        seen["model"] = model
        return []

    with (
        patch.object(compaction, "extract_facts", fake_extract),
        patch.object(compaction, "summarize_segment", AsyncMock(return_value="s")),
    ):
        await compaction.compact(
            [{"role": "user", "content": "x " * 200} for _ in range(40)],
            models=CHAIN,
            threshold=10,
            drain_to=5,
        )

    assert seen.get("model") == CHAIN, f"compaction got {seen.get('model')!r}"


@pytest.mark.asyncio
async def test_the_planner_is_given_the_whole_remaining_chain():
    """A one-element fallback can never reach the tier terminating the chain.

    Asserted on the argument actually passed, not on the source text — an
    earlier version of this test grepped for a slice literal and matched the
    explanatory comment beside the fix.
    """
    from types import SimpleNamespace

    from robothor.engine.run_lifecycle import RunLifecycleMixin

    chain = ["openrouter/a", "openrouter/b", "openrouter/c", "ollama_chat/qwen3.8:27b"]
    seen: dict[str, object] = {}

    async def fake_generate_plan(message, tool_names, model, fallback_models=None):
        seen["primary"] = model
        seen["fallbacks"] = fallback_models
        return

    cfg = SimpleNamespace(planning_model=None)
    with patch("robothor.engine.planner.generate_plan", fake_generate_plan):
        await RunLifecycleMixin._run_planner(SimpleNamespace(), cfg, "do a thing", [], chain)

    assert seen["fallbacks"] == chain[1:], seen
    assert "ollama_chat/qwen3.8:27b" in (seen["fallbacks"] or []), (
        "the planner still cannot reach the offline tier"
    )


def test_the_last_resort_model_is_validated(monkeypatch):
    """The one model the whole fleet's offline tier depends on was unchecked.

    _with_last_resort appends it AFTER validate_manifest has run, so a typo in
    robothor.env produced a fleet-wide chain ending in fiction, with zero
    warnings on every agent.
    """
    from robothor.engine.config_schema import validate_manifest

    monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/does-not-exist:999b")
    warnings = validate_manifest({"id": "x", "model": {"primary": "ollama_chat/qwen3.8:27b"}})
    assert any("does-not-exist" in w for w in warnings), warnings


def test_a_real_last_resort_model_is_not_flagged(monkeypatch):
    from robothor.engine.config_schema import validate_manifest

    monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/qwen3.8:27b")
    warnings = validate_manifest({"id": "x", "model": {"primary": "ollama_chat/qwen3.8:27b"}})
    assert not [w for w in warnings if "last-resort" in w.lower()], warnings
