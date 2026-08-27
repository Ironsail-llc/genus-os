"""Memory is one backend. Hermes ships eight, and Hermes is the harness ahead.

`search_memory` reads Postgres and nothing else. An instance wanting a
knowledge graph, a vector service, or a second corpus alongside its own had
to patch the engine.

Hermes Agent — the harness leading three of the four published
WildClawBench models — ships eight pluggable memory providers, and its
design decision is the one worth copying: **external providers run
ALONGSIDE built-in memory, never replacing it.** A package that could
displace the operator's own recall would be a takeover of the one
subsystem this platform is measurably ahead on.

So contributed results are appended after the built-in rows, never before,
and each carries its provider's name so the agent knows what it is reading.
A provider that raises or hangs is dropped and the built-in answer stands:
memory is the last thing that should fail closed on a third-party bug.
"""

from __future__ import annotations

import pytest

from robothor.plugins import reload_plugins


class _MemEP:
    group = "genus.memory"

    def __init__(self, providers: dict, name: str = "testmem"):
        self.name = name
        self._providers = providers

    def load(self):
        return {"genus_contract_version": "1.0", "providers": self._providers}


def _provider(rows=None, boom: bool = False):
    async def search(query: str, limit: int = 10):
        if boom:
            raise RuntimeError("provider exploded")
        return list(rows or ["a fact from elsewhere"])

    return {"search": search}


@pytest.fixture
def install(monkeypatch):
    def _install(providers: dict):
        from robothor.plugins import loader

        monkeypatch.setattr(loader, "_discover", lambda: [_MemEP(providers)])
        reload_plugins()

    yield _install
    from robothor.plugins import loader

    monkeypatch.setattr(loader, "_discover", list)
    reload_plugins()


class TestTheGroupExists:
    def test_the_loader_declares_it(self):
        from robothor.plugins.loader import _GROUPS

        assert _GROUPS.get("genus.memory") == "providers"

    def test_the_pluginset_carries_it(self, install):
        from robothor.plugins import load_plugins

        install({"graph": _provider()})
        assert "graph" in (load_plugins(reserved_names=set()).memory or {})


class TestProvidersAugmentNeverReplace:
    @pytest.mark.asyncio
    async def test_contributed_rows_come_after_the_builtin_ones(self, install):
        from robothor.engine.tools.handlers.memory import merge_plugin_memory

        install({"graph": _provider(["from the graph"])})
        merged = await merge_plugin_memory("who runs helios", ["builtin one"], limit=10)
        assert merged[0] == "builtin one", "a plugin displaced the operator's own memory"
        assert any("from the graph" in m for m in merged[1:])

    @pytest.mark.asyncio
    async def test_each_contributed_row_names_its_provider(self, install):
        from robothor.engine.tools.handlers.memory import merge_plugin_memory

        install({"graph": _provider(["orphan fact"])})
        merged = await merge_plugin_memory("q", [], limit=10)
        assert any("graph" in m for m in merged), f"unattributed provider rows: {merged}"

    @pytest.mark.asyncio
    async def test_no_providers_changes_nothing(self):
        from robothor.engine.tools.handlers.memory import merge_plugin_memory

        assert await merge_plugin_memory("q", ["builtin"], limit=10) == ["builtin"]


class TestAProviderCannotBreakRecall:
    @pytest.mark.asyncio
    async def test_a_raising_provider_leaves_the_builtin_answer_standing(self, install):
        from robothor.engine.tools.handlers.memory import merge_plugin_memory

        install({"bad": _provider(boom=True)})
        assert await merge_plugin_memory("q", ["builtin"], limit=10) == ["builtin"]

    @pytest.mark.asyncio
    async def test_one_bad_provider_does_not_lose_a_good_one(self, install):
        from robothor.engine.tools.handlers.memory import merge_plugin_memory

        install({"bad": _provider(boom=True), "good": _provider(["kept"])})
        merged = await merge_plugin_memory("q", [], limit=10)
        assert any("kept" in m for m in merged)

    @pytest.mark.asyncio
    async def test_a_provider_returning_junk_is_ignored(self, install):
        from robothor.engine.tools.handlers.memory import merge_plugin_memory

        async def search(query, limit=10):
            return "not a list"

        install({"junk": {"search": search}})
        assert await merge_plugin_memory("q", ["builtin"], limit=10) == ["builtin"]


class TestItIsReachable:
    def test_search_memory_merges_plugin_results(self):
        import inspect

        from robothor.engine.tools.handlers import memory

        src = inspect.getsource(memory._search_memory)
        assert "merge_plugin_memory" in src, (
            "search_memory never merges provider results — the seam is unreachable"
        )
