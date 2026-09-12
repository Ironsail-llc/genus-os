"""A registered reasoning model must be dispatchable through litellm.

litellm only forwards the ``thinking`` parameter for models its bundled table
marks ``supports_reasoning``.  A brand-new model is absent from that table, so a
registry entry with ``supports_thinking=True`` bricked every call
(``UnsupportedParamsError: openrouter does not support parameters: ['thinking']``)
until we publish the capability ourselves — and it must happen with the catalog
flag OFF, which is the state of a fresh install.
"""

from __future__ import annotations

import copy

import litellm
import pytest

from robothor.engine import model_registry
from robothor.engine.model_registry import _MODEL_REGISTRY, register_pricing_with_litellm


@pytest.fixture(autouse=True)
def _restore_litellm_table():
    """``register_pricing_with_litellm`` mutates litellm's process-wide table.

    Other tests (the catalog-drift suite) compare the registry against litellm's
    *bundled* prices, so every entry this module touches is put back afterwards.
    """
    before = copy.deepcopy(litellm.model_cost)
    yield
    litellm.model_cost.clear()
    litellm.model_cost.update(before)


def _thinking_openrouter_ids() -> list[str]:
    return [
        model_id
        for model_id, limits in _MODEL_REGISTRY.items()
        if limits.supports_thinking and model_id.startswith("openrouter/")
    ]


@pytest.mark.parametrize("flag_on", [False, True])
def test_every_thinking_model_is_published_as_reasoning_capable(monkeypatch, flag_on):
    monkeypatch.setenv("ROBOTHOR_RIP_17_ENABLED", "true" if flag_on else "false")
    # Forget anything an earlier test published so this run proves the seeding.
    for model_id in _thinking_openrouter_ids():
        litellm.model_cost.pop(model_id, None)

    register_pricing_with_litellm()

    for model_id in _thinking_openrouter_ids():
        assert litellm.utils.supports_reasoning(model_id), (
            f"{model_id} is registered as a thinking model but litellm does not "
            f"know it can reason (flag_on={flag_on}); every call would fail with "
            "UnsupportedParamsError['thinking']"
        )


def test_the_new_deepseek_flash_accepts_the_thinking_parameter(monkeypatch):
    """End to end against litellm's own parameter table, not our payload."""
    monkeypatch.setenv("ROBOTHOR_RIP_17_ENABLED", "false")
    register_pricing_with_litellm()
    params = litellm.get_supported_openai_params(
        model="deepseek/deepseek-v4.1-flash", custom_llm_provider="openrouter"
    )
    assert params is not None and "thinking" in params


def test_publication_only_widens(monkeypatch):
    """A curated ``supports_thinking=False`` must never take a working parameter away."""
    monkeypatch.setenv("ROBOTHOR_RIP_17_ENABLED", "false")
    probe = "openrouter/z-ai/glm-5.3-flash"  # in litellm's own table as reasoning-capable
    assert litellm.utils.supports_reasoning(probe)
    limits = _MODEL_REGISTRY[probe]
    monkeypatch.setitem(
        _MODEL_REGISTRY,
        probe,
        model_registry.ModelLimits(**{**limits.__dict__, "supports_thinking": False}),
    )
    register_pricing_with_litellm()
    assert litellm.utils.supports_reasoning(probe)
