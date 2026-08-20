"""Handler ↔ schema parity for the engine tool registry.

Defect class this guards against (found live, 2026-08): a handler is added to
``robothor/engine/tools/handlers/`` (and often prompted, manifest-declared, and
unit-tested via ``HANDLERS[...]`` directly) but the matching schema entry in
``robothor/engine/tools/schemas.py`` is never written — so the registry never
advertises it and no agent can ever call it. ``classify_run_failure`` shipped
that way in 2026-04 and stayed unreachable for four months; ``skill_view``
likewise broke the entire Rip-3 load-on-demand skills path.

The sibling defect: a per-task tool whitelist naming tools that do not exist
(``REVIEW_TOOL_WHITELIST`` carried Hermes-upstream names like ``memory_search``
for months, silently denying every real memory call from review forks).

These tests fail the build the moment either defect reappears.
"""

from __future__ import annotations

import pytest

# Tools implemented as dispatch handlers but deliberately NOT advertised to
# agents. Adding a name here is an explicit, reviewed decision — the default
# for a new handler is "must have a schema".
INTERNAL_ONLY: frozenset[str] = frozenset()

# Whitelist entries served by dynamic MCP adapters at runtime (registered via
# ``ToolRegistry.register_adapter_tools``), so they have no static schema.
ADAPTER_PROVIDED: frozenset[str] = frozenset(
    {
        "impetus_list_resources",
        "impetus_list",
        "impetus_get",
        "impetus_search",
    }
)


@pytest.fixture
def schema_names(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Every statically advertisable tool name, with all flag-gated schema
    groups forced ON so parity covers the full surface regardless of the
    environment the test runs in."""
    monkeypatch.delenv("ROBOTHOR_DISABLE_ALL_RIPS", raising=False)
    monkeypatch.setenv("ROBOTHOR_RIP_12_ENABLED", "1")  # memory vault
    monkeypatch.setenv("ROBOTHOR_RIP_13_ENABLED", "1")  # symbolic memory
    monkeypatch.setenv("ROBOTHOR_RIP_14_ENABLED", "1")  # intent memory

    from robothor.api.mcp import get_tool_definitions
    from robothor.engine.tools.schemas import get_engine_schemas

    return {d["name"] for d in get_tool_definitions()} | set(get_engine_schemas())


@pytest.fixture
def handler_names() -> set[str]:
    from robothor.engine.tools.dispatch import _collect_handlers

    return set(_collect_handlers())


def test_every_handler_has_a_schema(handler_names: set[str], schema_names: set[str]) -> None:
    """A dispatch handler without a schema is unreachable by every agent —
    the registry drops names absent from ``_schemas`` (see
    ``ToolRegistry._get_filtered_names``), so 'implemented + green unit tests'
    can coexist with 'no agent can ever call it'."""
    missing = handler_names - schema_names - INTERNAL_ONLY
    assert not missing, (
        f"Handlers with no schema entry in robothor/engine/tools/schemas.py "
        f"(unreachable by any agent): {sorted(missing)}. Register a schema, or "
        f"add the name to INTERNAL_ONLY with a comment explaining why it must "
        f"not be advertised."
    )


def test_every_schema_has_a_handler(handler_names: set[str], schema_names: set[str]) -> None:
    """The reverse direction: an advertised tool the dispatcher cannot route
    fails at call time on every invocation."""
    dangling = schema_names - handler_names
    assert not dangling, f"Advertised schemas with no dispatch handler: {sorted(dangling)}"


def test_internal_only_names_actually_exist(handler_names: set[str]) -> None:
    """Keep the exemption list honest — a stale INTERNAL_ONLY entry would
    silently widen the hole this test exists to close."""
    stale = INTERNAL_ONLY - handler_names
    assert not stale, f"INTERNAL_ONLY names no handler implements: {sorted(stale)}"


def test_review_whitelist_names_are_registered(schema_names: set[str]) -> None:
    """A whitelist of nonexistent names passes every unit test while denying
    everything real at dispatch time (the Hermes-port failure)."""
    from robothor.engine.background_review import REVIEW_TOOL_WHITELIST

    missing = REVIEW_TOOL_WHITELIST - schema_names
    assert not missing, (
        f"REVIEW_TOOL_WHITELIST names with no registered schema "
        f"(review forks can never call them): {sorted(missing)}"
    )


def test_curator_whitelist_names_are_registered(schema_names: set[str]) -> None:
    from robothor.engine.curator import _curator_tool_whitelist

    for dry_run in (True, False):
        missing = _curator_tool_whitelist(dry_run=dry_run) - schema_names
        assert not missing, (
            f"Curator whitelist (dry_run={dry_run}) names with no registered "
            f"schema: {sorted(missing)}"
        )


def test_benchmark_readonly_names_are_registered(schema_names: set[str]) -> None:
    """The benchmark read-only set is intersected with each agent's tools, so
    unknown names are not a runtime denial — but they still rot silently.
    Adapter-served tools are exempt (no static schema by design)."""
    from robothor.engine.tools.handlers.benchmark import _BENCHMARK_READONLY_TOOLS

    missing = _BENCHMARK_READONLY_TOOLS - schema_names - ADAPTER_PROVIDED
    assert not missing, (
        f"_BENCHMARK_READONLY_TOOLS names with no registered schema and no "
        f"ADAPTER_PROVIDED exemption: {sorted(missing)}"
    )
