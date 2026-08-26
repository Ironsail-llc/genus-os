"""The last declared-but-unconsumed plugin group.

#411 declared four entry-point groups. #421 made `genus.tools` reachable —
plugin tools were loaded but never advertised to the model. #424 made
`genus.guardrails` reachable — they had no consumer at all. `genus.hooks` was
the last one: declared in the loader, documented, and registered nowhere, so a
plugin could not run anything on a lifecycle event.

A competitive audit rated this platform "far behind" on extensibility, partly
because OpenClaw registers tools, skills, channels, model providers, CLI and
hooks while three of our four declared groups did nothing.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from robothor.engine.hook_registry import HookRegistry, register_plugin_hooks
from robothor.plugins.loader import PluginSet


def test_a_plugin_hook_is_registered():
    def notify(*a, **kw):
        return {"ok": True}

    reg = HookRegistry()
    with patch(
        "robothor.plugins.load_plugins",
        return_value=PluginSet(hooks={"acme.notify": notify}),
    ):
        count = register_plugin_hooks(reg)

    assert count == 1, "the plugin hook was never registered"
    assert reg._python_handlers.get("acme.notify") is notify


def test_a_plugin_cannot_replace_a_builtin_handler(caplog):
    """channel_bus.* handlers are engine internals; a package must not claim one."""

    def hijack(*a, **kw):
        return {"hijacked": True}

    def original(*a, **kw):
        return {"ok": True}

    reg = HookRegistry()
    reg.register_python_handler("channel_bus.surface", original)

    caplog.set_level(logging.WARNING)
    with patch(
        "robothor.plugins.load_plugins",
        return_value=PluginSet(hooks={"channel_bus.surface": hijack}),
    ):
        register_plugin_hooks(reg)

    assert reg._python_handlers["channel_bus.surface"] is original, (
        "a plugin overwrote an engine hook"
    )
    assert "channel_bus.surface" in caplog.text


def test_no_plugins_is_a_no_op():
    reg = HookRegistry()
    with patch("robothor.plugins.load_plugins", return_value=PluginSet()):
        assert register_plugin_hooks(reg) == 0


def test_a_broken_plugin_does_not_stop_the_engine():
    reg = HookRegistry()
    with patch("robothor.plugins.load_plugins", side_effect=RuntimeError("boom")):
        assert register_plugin_hooks(reg) == 0


def test_the_daemon_actually_registers_them():
    """An unwired registration is the defect class this whole series is about.

    Asserted against the daemon's source, so deleting the call fails here.
    """
    import inspect

    from robothor.engine import daemon

    assert "register_plugin_hooks(" in inspect.getsource(daemon), (
        "plugin hooks are loaded but the daemon never registers them"
    )
