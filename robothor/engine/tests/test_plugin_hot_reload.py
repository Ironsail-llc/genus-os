"""Installing a plugin should not require restarting the engine.

Genus discovers plugins once and caches the result in four places: the
dispatch handler map, the tool registry's schemas, the guardrail engine's
policy cache, and the hook registry. Nothing invalidates any of them, so a
`pip install` of a new capability only takes effect after a restart — which
on this fleet means cancelling every in-flight run.

DeepSeek Harness mounts and unmounts plugins with reversible effects and no
restart, and that is the one axis where its plugin story is plainly ahead of
this one. Closing it does not need their architecture: the caches just need
to know when they are stale.

A generation counter does that without tracking live instances. Each cache
records the generation it was built at; `reload_plugins()` bumps the global
counter, and any cache reading a stale generation rebuilds itself on next
use. Per-instance caches — the guardrail engine's, the hook registry's —
are covered by the same mechanism as the module-level one.
"""

from __future__ import annotations

import pytest

from robothor.plugins import generation, reload_plugins


class TestGeneration:
    def test_generation_is_stable_until_reloaded(self):
        assert generation() == generation()

    def test_reload_advances_it(self):
        before = generation()
        after = reload_plugins()
        assert after > before
        assert generation() == after

    def test_every_reload_advances_it_again(self):
        seen = {reload_plugins() for _ in range(3)}
        assert len(seen) == 3, "a reload must always invalidate, never coalesce"


class TestDispatchRebuilds:
    def test_handlers_are_cached_between_reloads(self):
        """The counter must not turn every lookup into a rediscovery."""
        from robothor.engine.tools import dispatch

        first = dispatch._get_handlers()
        assert dispatch._get_handlers() is first

    def test_a_reload_rebuilds_the_handler_map(self):
        from robothor.engine.tools import dispatch

        first = dispatch._get_handlers()
        reload_plugins()
        second = dispatch._get_handlers()
        assert second is not first, "stale handler map survived a reload"
        assert set(second) >= set(first) - {"probe_only"}

    def test_a_plugin_installed_at_runtime_becomes_callable(self, monkeypatch):
        """The whole point: no restart.

        Discovery is injected rather than writing to site-packages, so the
        test exercises the invalidation path without mutating the
        interpreter's real environment.
        """
        import asyncio

        from robothor.engine.tools import dispatch
        from robothor.plugins import loader

        assert "hot_probe" not in dispatch._get_handlers()

        async def _hot_probe(args, ctx=None):
            return {"result": "loaded without a restart"}

        class _EP:
            name = "hot_probe"
            group = "genus.tools"

            def load(self):
                return {"genus_contract_version": "1.0", "handlers": {"hot_probe": _hot_probe}}

        monkeypatch.setattr(loader, "_discover", lambda: [_EP()])
        reload_plugins()

        handlers = dispatch._get_handlers()
        assert "hot_probe" in handlers, "reload did not pick up the new plugin"
        assert asyncio.run(handlers["hot_probe"]({}))["result"] == "loaded without a restart"

    def test_removing_a_plugin_takes_effect_too(self, monkeypatch):
        """Unmount, not just mount — a stale capability must disappear."""
        from robothor.engine.tools import dispatch
        from robothor.plugins import loader

        async def _temp(args, ctx=None):
            return {}

        class _EP:
            name = "temp_probe"
            group = "genus.tools"

            def load(self):
                return {"genus_contract_version": "1.0", "handlers": {"temp_probe": _temp}}

        monkeypatch.setattr(loader, "_discover", lambda: [_EP()])
        reload_plugins()
        assert "temp_probe" in dispatch._get_handlers()

        monkeypatch.setattr(loader, "_discover", list)
        reload_plugins()
        assert "temp_probe" not in dispatch._get_handlers(), "removed plugin still callable"


class TestGuardrailEngineRebuilds:
    def test_the_per_instance_cache_honours_the_counter(self, monkeypatch):
        from robothor.engine import guardrails as g
        from robothor.plugins import loader

        engine = g.GuardrailEngine.__new__(g.GuardrailEngine)
        assert engine._plugin_guardrails() == {}

        def _policy(*a, **k):
            return None

        class _EP:
            name = "hot_policy"
            group = "genus.guardrails"

            def load(self):
                return {"genus_contract_version": "1.0", "policies": {"hot_policy": _policy}}

        monkeypatch.setattr(loader, "_discover", lambda: [_EP()])
        reload_plugins()
        assert "hot_policy" in engine._plugin_guardrails(), (
            "the guardrail cache is per-instance and ignored the reload"
        )


class TestReloadIsSafe:
    def test_a_broken_plugin_does_not_raise_through_reload(self, monkeypatch):
        from robothor.plugins import loader

        class _EP:
            name = "broken"
            group = "genus.tools"

            def load(self):
                raise RuntimeError("this package is broken")

        monkeypatch.setattr(loader, "_discover", lambda: [_EP()])
        reload_plugins()  # must not raise
        from robothor.engine.tools import dispatch

        assert "exec" in dispatch._get_handlers(), "a broken plugin took the engine's tools with it"


class TestTheModelSeesIt:
    """A hot-loaded tool the model is never told about is half a feature."""

    def test_the_registry_advertises_a_plugin_added_after_construction(self, monkeypatch):
        from robothor.engine.tools.registry import ToolRegistry
        from robothor.plugins import loader

        registry = ToolRegistry()
        assert registry.get_schema("hot_schema_probe") is None

        class _EP:
            name = "hot_schema_probe"
            group = "genus.schemas"

            def load(self):
                return {
                    "genus_contract_version": "1.0",
                    "schemas": {
                        "hot_schema_probe": {
                            "name": "hot_schema_probe",
                            "description": "added without a restart",
                            "input_schema": {"type": "object"},
                        }
                    },
                }

        monkeypatch.setattr(loader, "_discover", lambda: [_EP()])
        reload_plugins()

        assert registry.get_schema("hot_schema_probe") is not None, (
            "the registry never re-read plugin schemas, so the model cannot see the tool"
        )

    def test_a_built_in_is_never_lost_across_a_reload(self, monkeypatch):
        from robothor.engine.tools.registry import ToolRegistry
        from robothor.plugins import loader

        registry = ToolRegistry()
        assert registry.get_schema("exec") is not None
        monkeypatch.setattr(loader, "_discover", list)
        reload_plugins()
        assert registry.get_schema("exec") is not None, "a reload dropped a built-in tool"


class TestTheOperatorCanTriggerIt:
    """A reload nothing can call is a library function, not a feature."""

    def test_the_sighup_handler_reloads(self):
        from robothor.engine.daemon import _handle_plugin_reload_signal
        from robothor.plugins import generation

        before = generation()
        _handle_plugin_reload_signal()
        assert generation() > before

    def test_the_handler_never_raises(self, monkeypatch):
        """A failed reload must not take the daemon down with it."""
        from robothor.engine import daemon

        monkeypatch.setattr(
            daemon, "reload_plugins", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        _ = daemon._handle_plugin_reload_signal()  # must not raise

    def test_the_unit_exposes_a_reload_command(self):
        """`systemctl reload robothor-engine` has to actually be wired."""
        from pathlib import Path

        unit = Path(__file__).resolve().parents[3] / "infra" / "systemd" / "robothor-engine.service"
        assert unit.exists(), f"unit template missing: {unit}"
        body = unit.read_text()
        assert "ExecReload=" in body, "no ExecReload — the signal is unreachable from systemctl"
        assert "HUP" in body

    @pytest.mark.asyncio
    async def test_the_signal_is_actually_installable(self):
        """Executes the registration — the suite was green with a NameError here.

        Left inline in `main`, these lines only ever ran in a live daemon, so
        a missing import surfaced as the engine failing to start rather than
        as a test failure.
        """
        import signal

        from robothor.engine.daemon import _install_plugin_reload_signal

        assert _install_plugin_reload_signal() is True
        import asyncio

        asyncio.get_running_loop().remove_signal_handler(signal.SIGHUP)
