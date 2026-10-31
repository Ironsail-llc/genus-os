"""Deferred tool loading is the default (Rip 16), with a per-agent core.

A broad agent used to receive every allowed schema on every LLM call — for
``main`` on a stock checkout that is over two hundred schemas, roughly 32k
tokens by the engine's own estimator, before any plugin adds its tools. With
deferral on by default the model sees the agent's always-loaded core (the
static ``CORE_TOOLS`` plus the tools it actually called in its recent runs)
and the three meta-tools; everything else stays reachable through
``tool_search`` → ``tool_call``. ``ROBOTHOR_RIP_16_ENABLED=0`` restores the
old full-schema behaviour.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.tools.constants import CORE_TOOLS, TOOLSEARCH_TOOLS
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import toolsearch
from robothor.engine.tools.registry import ToolRegistry, get_registry


def _names(schemas: list[dict]) -> set[str]:
    return {s["function"]["name"] for s in schemas}


def _cfg(agent_id: str = "broad", allowed: list[str] | None = None):
    from robothor.engine.models import AgentConfig, DeliveryMode

    return AgentConfig(
        id=agent_id,
        name=agent_id,
        description="",
        model_primary="openrouter/x/model",
        model_fallbacks=[],
        cron_expr="0 * * * *",
        timezone="UTC",
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        tools_allowed=allowed or [],
        tools_denied=[],
        instruction_file="",
        bootstrap_files=[],
    )


@pytest.fixture
def default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing set: the defaults are what is under test."""
    for name in (
        "ROBOTHOR_RIP_16_ENABLED",
        "ROBOTHOR_DISABLE_ALL_RIPS",
        "ROBOTHOR_DEFERRED_TOOLS_THRESHOLD",
        "ROBOTHOR_DEFERRED_TOOLS_RECENT_RUNS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


def _broad_allowed(registry: ToolRegistry, count: int = 150) -> list[str]:
    """``count`` registered tool names, deliberately including non-core ones."""
    names = [n for n in sorted(registry._schemas) if n not in TOOLSEARCH_TOOLS]
    assert len(names) >= count, "the stock registry should hold 150+ tools"
    return names[:count]


# ── The flag ──────────────────────────────────────────────────────────


class TestTheFlag:
    def test_deferral_is_on_when_nothing_is_set(self, default_env) -> None:
        from robothor.engine.feature_flags import deferred_tools_enabled

        assert deferred_tools_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "off", "no"])
    def test_it_can_still_be_switched_off(self, default_env, monkeypatch, value) -> None:
        from robothor.engine.feature_flags import deferred_tools_enabled

        monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", value)
        assert deferred_tools_enabled() is False

    def test_the_panic_switch_still_wins(self, default_env, monkeypatch) -> None:
        from robothor.engine.feature_flags import deferred_tools_enabled

        monkeypatch.setenv("ROBOTHOR_DISABLE_ALL_RIPS", "1")
        assert deferred_tools_enabled() is False

    def test_the_setting_is_declared_with_the_same_default(self) -> None:
        from robothor.settings.registry import field_index

        fields = {env: record["default"] for env, record in field_index().items()}
        assert fields.get("ROBOTHOR_RIP_16_ENABLED") is True
        assert fields.get("ROBOTHOR_DEFERRED_TOOLS_RECENT_RUNS") == 10


# ── What a 150-tool agent is shown ────────────────────────────────────


class TestABroadAgentSeesItsCore:
    def test_150_tools_become_core_plus_the_meta_tools(self, default_env, registry) -> None:
        allowed = _broad_allowed(registry)
        cfg = _cfg(allowed=allowed)

        advertised = _names(registry.build_for_agent(cfg))

        core = (CORE_TOOLS & set(registry.get_tool_names(cfg))) | TOOLSEARCH_TOOLS
        assert advertised <= core
        assert advertised >= TOOLSEARCH_TOOLS
        assert len(advertised) <= len(core)
        assert len(advertised) < 40

    def test_recently_used_tools_join_the_core(self, default_env, registry) -> None:
        allowed = _broad_allowed(registry)
        recent_tool = next(n for n in allowed if n not in CORE_TOOLS)
        cfg = _cfg(allowed=allowed)

        advertised = _names(registry.build_for_agent(cfg, recent=frozenset({recent_tool})))

        assert recent_tool in advertised
        core = (CORE_TOOLS & set(allowed)) | {recent_tool} | TOOLSEARCH_TOOLS
        assert advertised <= core | set(registry.get_tool_names(cfg)) - set(allowed)
        assert len(advertised) <= len(core) + 5  # + GOAL_TOOLS the filter force-adds

    def test_a_recent_tool_the_agent_may_not_use_is_not_advertised(
        self, default_env, registry
    ) -> None:
        cfg = _cfg(allowed=_broad_allowed(registry, 60))
        advertised = _names(registry.build_for_agent(cfg, recent=frozenset({"no_such_tool"})))
        assert "no_such_tool" not in advertised

    def test_flag_off_keeps_todays_full_set(self, default_env, registry, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", "0")
        cfg = _cfg(allowed=_broad_allowed(registry))

        advertised = registry.build_for_agent(cfg, recent=frozenset({"read_file"}))

        assert [s["function"]["name"] for s in advertised] == registry.get_tool_names(cfg)
        assert not (_names(advertised) & TOOLSEARCH_TOOLS)

    def test_a_curated_agent_below_the_threshold_keeps_everything(
        self, default_env, registry
    ) -> None:
        cfg = _cfg(allowed=["read_file", "gws_gmail_send", "search_memory"])
        assert _names(registry.build_for_agent(cfg)) >= {"gws_gmail_send"}
        assert not (_names(registry.build_for_agent(cfg)) & TOOLSEARCH_TOOLS)


# ── A deferred tool resolves through search and executes ──────────────


class TestADeferredToolIsReachable:
    @pytest.mark.asyncio
    async def test_search_finds_it_and_tool_call_executes_it(self, default_env) -> None:
        from robothor.engine.toolset_prep import publish_toolset, withdraw_toolset

        registry = get_registry()
        cfg = _cfg(allowed=_broad_allowed(registry))
        schemas = registry.build_for_agent(cfg)
        target = "gws_gmail_search"
        assert target in registry.get_tool_names(cfg)
        assert target not in _names(schemas), "precondition: the tool is deferred"

        tokens = publish_toolset(registry, cfg, registry.get_tool_names(cfg), _names(schemas))
        try:
            found = await toolsearch._tool_search(
                {"query": "search my email"}, ToolContext(agent_id=cfg.id)
            )
            hit = next(h for h in found["results"] if h["name"] == target)
            assert hit["in_toolset"] is False

            with patch.object(
                registry, "execute", AsyncMock(return_value={"messages": []})
            ) as execute:
                out = await toolsearch._tool_call(
                    {"name": target, "arguments": {"query": "invoice"}},
                    ToolContext(agent_id=cfg.id, run_id="r1"),
                )
        finally:
            withdraw_toolset(tokens)

        assert out == {"messages": []}
        execute.assert_awaited_once()
        assert execute.await_args.args == (target, {"query": "invoice"})

    @pytest.mark.asyncio
    async def test_a_recent_tool_is_reported_as_already_in_the_toolset(self, default_env) -> None:
        from robothor.engine.toolset_prep import publish_toolset, withdraw_toolset

        registry = get_registry()
        cfg = _cfg(allowed=_broad_allowed(registry))
        target = "gws_gmail_search"
        schemas = registry.build_for_agent(cfg, recent=frozenset({target}))

        tokens = publish_toolset(registry, cfg, registry.get_tool_names(cfg), _names(schemas))
        try:
            found = await toolsearch._tool_search(
                {"query": "search my email"}, ToolContext(agent_id=cfg.id)
            )
        finally:
            withdraw_toolset(tokens)

        hit = next(h for h in found["results"] if h["name"] == target)
        assert hit["in_toolset"] is True


# ── Recent tools: from agent_run_steps, off the loop, cached ──────────


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **kw):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestRecentTools:
    def test_fetch_reads_the_last_n_runs_and_drops_meta_tools(self, default_env) -> None:
        from robothor.engine import recent_tools

        cursor = _Cursor([("gws_gmail_send", 7), ("tool_search", 5), ("read_file", 2)])
        with patch("robothor.engine.recent_tools.get_connection", return_value=_Conn(cursor)):
            names = recent_tools.fetch_recent_tool_names("example-agent", "default", runs=10)

        assert names == ["gws_gmail_send", "read_file"]
        sql, params = cursor.executed[0]
        assert "agent_run_steps" in sql
        assert "example-agent" in params and "default" in params and 10 in params

    @pytest.mark.asyncio
    async def test_lookup_runs_off_the_event_loop_and_is_cached(self, default_env) -> None:
        from robothor.engine import recent_tools

        recent_tools.clear_cache()
        loop_thread = threading.get_ident()
        seen: list[int] = []

        def fake_fetch(agent_id, tenant_id, runs):
            seen.append(threading.get_ident())
            return ["gws_gmail_send"]

        with patch("robothor.engine.recent_tools.fetch_recent_tool_names", fake_fetch):
            first = await recent_tools.recent_tool_names("example-agent", "default")
            second = await recent_tools.recent_tool_names("example-agent", "default")

        assert first == second == frozenset({"gws_gmail_send"})
        assert len(seen) == 1, "the second lookup is served from the cache"
        assert seen[0] != loop_thread, "the DB lookup must not block the event loop"

    @pytest.mark.asyncio
    async def test_a_failing_or_slow_lookup_is_an_empty_set(self, default_env, monkeypatch) -> None:
        from robothor.engine import recent_tools

        recent_tools.clear_cache()

        def boom(*_a):
            raise RuntimeError("database down")

        with patch("robothor.engine.recent_tools.fetch_recent_tool_names", boom):
            assert await recent_tools.recent_tool_names("a1", "default") == frozenset()

        recent_tools.clear_cache()
        monkeypatch.setattr(recent_tools, "_LOOKUP_TIMEOUT_S", 0.05)

        def slow(*_a):
            import time

            time.sleep(0.5)
            return ["exec"]

        with patch("robothor.engine.recent_tools.fetch_recent_tool_names", slow):
            started = asyncio.get_running_loop().time()
            assert await recent_tools.recent_tool_names("a2", "default") == frozenset()
            assert asyncio.get_running_loop().time() - started < 0.4

    @pytest.mark.asyncio
    async def test_zero_runs_means_core_only_and_no_query(self, default_env, monkeypatch) -> None:
        from robothor.engine import recent_tools

        recent_tools.clear_cache()
        monkeypatch.setenv("ROBOTHOR_DEFERRED_TOOLS_RECENT_RUNS", "0")
        with patch("robothor.engine.recent_tools.fetch_recent_tool_names") as fetch:
            assert await recent_tools.recent_tool_names("a3", "default") == frozenset()
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_prepare_toolset_advertises_the_recent_tools(self, default_env) -> None:
        from robothor.engine.toolset_prep import prepare_toolset

        registry = ToolRegistry()
        cfg = _cfg(allowed=_broad_allowed(registry))
        cfg.mcp_servers = []
        with (
            patch("robothor.engine.adapters.get_adapters_for_agent", return_value=[]),
            patch(
                "robothor.engine.recent_tools.recent_tool_names",
                AsyncMock(return_value=frozenset({"gws_gmail_search"})),
            ),
        ):
            prepared = await prepare_toolset(
                registry,
                cfg,
                agent_id=cfg.id,
                system_prompt="IDENTITY",
                readonly_mode=False,
                deep_plan=False,
                tenant_id="default",
            )

        assert "gws_gmail_search" in _names(prepared.tool_schemas)
        assert "gws_gmail_search" not in prepared.reachable_names


# ── The measurement ───────────────────────────────────────────────────


def _schema_tokens(schemas: list[dict]) -> int:
    from robothor.engine.context import estimate_tokens

    return estimate_tokens([{"role": "system", "content": json.dumps(schemas)}])


def test_main_prompt_tokens_before_and_after(default_env, monkeypatch, capsys) -> None:
    """main declares no tools_allowed, so it is handed every registered tool."""
    registry = ToolRegistry()
    main = _cfg(agent_id="main")

    monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", "0")
    before = registry.build_for_agent(main)
    monkeypatch.delenv("ROBOTHOR_RIP_16_ENABLED")
    after = registry.build_for_agent(main)

    tokens_before, tokens_after = _schema_tokens(before), _schema_tokens(after)
    with capsys.disabled():
        print(
            f"\nmain tool schemas: {len(before)} -> {len(after)}; "
            f"estimated tokens {tokens_before} -> {tokens_after}"
        )
    assert tokens_after <= 9_000
    assert tokens_before >= 3 * tokens_after


def test_unsearchable_schemas_are_named(registry) -> None:
    """The keyword index covers a schema only if it has a searchable word."""
    assert registry.unsearchable_names() == []
    registry._schemas["is_it"] = {
        "type": "function",
        "function": {"name": "is_it", "description": "", "parameters": {}},
    }
    assert registry.unsearchable_names() == ["is_it"]


def test_plugin_schemas_are_searchable_by_their_own_keywords(registry) -> None:
    """A plugin schema may carry ``keywords``; search reads them, the wire never sees them."""
    from robothor.engine.tools.registry import wire_schema

    schema = {
        "type": "function",
        "function": {
            "name": "example_ledger_lookup",
            "description": "Look up an entry in the example ledger.",
            "keywords": ["invoice", "receipt"],
            "parameters": {"type": "object", "properties": {}},
        },
    }
    registry._schemas["example_ledger_lookup"] = schema
    hits = registry.search_tools(sorted(registry._schemas), "find the invoice", limit=3)
    assert hits[0]["name"] == "example_ledger_lookup"
    assert "keywords" not in wire_schema(schema)["function"]


