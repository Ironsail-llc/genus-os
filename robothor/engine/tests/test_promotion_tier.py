"""A search-promoted tool an owner's tier grants reaches the wire.

``publish_toolset`` publishes the deferred allow-set WIDENED by the run's
capability tier (an owner's interactive turn may run opt-in tools), and
``with_promoted`` rechecks a promotion against that published set. This drives
the real path, tier decision and all, rather than publishing a set by hand.
"""

from __future__ import annotations

from typing import Any

from robothor.engine.capability_tier import resolve_tier
from robothor.engine.config import AgentConfig, manifest_to_agent_config
from robothor.engine.tools.constants import OPT_IN_TOOLS
from robothor.engine.tools.registry import ToolRegistry
from robothor.engine.toolset_prep import publish_toolset, with_promoted, withdraw_toolset

OPT_IN = "web_render"


def _broad(registry: ToolRegistry) -> AgentConfig:
    """An agent with no tool list: everything but the opt-in families, which an
    owner's tier adds (an explicit ``tools_allowed`` is the boundary instead)."""
    return manifest_to_agent_config({"id": "broad", "name": "broad"})


def _names(schemas: list[dict[str, Any]]) -> set[str]:
    return {s["function"]["name"] for s in schemas if "function" in s}


def _promoted(capability: Any) -> set[str]:
    registry = ToolRegistry()
    cfg = _broad(registry)
    base = registry.build_for_agent(cfg, recent=frozenset())
    tokens = publish_toolset(
        registry, cfg, registry.get_tool_names(cfg), _names(base), capability=capability
    )
    try:
        return _names(with_promoted(registry, cfg, base, {OPT_IN}))
    finally:
        withdraw_toolset(tokens)


def test_the_preconditions_hold() -> None:
    registry = ToolRegistry()
    cfg = _broad(registry)
    assert OPT_IN in OPT_IN_TOOLS
    assert registry.should_defer(cfg)
    assert OPT_IN not in registry.deferred_whitelist(cfg)
    assert OPT_IN in registry.deferred_whitelist(cfg, tier="unrestricted")


def test_an_owners_widened_set_lets_the_promotion_through() -> None:
    owner = resolve_tier("owner", interactive=True, identity_verified=True)
    assert owner.tier, "an owner's interactive turn is a widening tier"
    assert OPT_IN in _promoted(owner)


def test_without_a_widening_tier_the_promotion_is_refused() -> None:
    assert OPT_IN not in _promoted(None)
