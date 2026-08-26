"""A guardrail a manifest names must either run, or say loudly that it cannot.

Two defects on the same line of code, found by a competitive architecture
audit that rated this platform "far behind" on plugins & extensibility:

1. `plugins.guardrails` had no consumer anywhere outside the loader and its
   own test file. #411 declared the entry-point group and #421 made plugin
   TOOLS reachable; guardrails were still declared-but-unconsumed, so a
   plugin could not extend the safety layer at all.

2. `_run_pre_policy` is an if-chain ending in a bare `return GuardrailResult()`
   — which is ALLOW. So a policy the engine does not recognise silently permits
   every call. `config_schema` warns about unknown names at load time, but a
   typo, a renamed policy, or a plugin guardrail whose package failed to
   install produces a manifest that reads as protected and enforces nothing.
   Fail-open, silently, in the safety layer.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from robothor.engine.guardrails import GuardrailEngine, GuardrailResult
from robothor.plugins.loader import PluginSet


def _engine(policies: list[str]) -> GuardrailEngine:
    return GuardrailEngine(enabled_policies=policies)


def test_a_plugin_guardrail_actually_runs():
    """The point of the entry-point group."""
    seen: dict[str, object] = {}

    def block_everything(tool_name, tool_args, ctx=None):
        seen["tool"] = tool_name
        return GuardrailResult(
            allowed=False,
            action="blocked",
            reason="plugin says no",
            guardrail_name="acme_policy",
        )

    with patch(
        "robothor.plugins.load_plugins",
        return_value=PluginSet(guardrails={"acme_policy": block_everything}),
    ):
        result = _engine(["acme_policy"]).check_pre_execution("exec", {"cmd": "ls"}, "agent-1")

    assert seen.get("tool") == "exec", "the plugin guardrail never ran"
    assert result.allowed is False
    assert "plugin says no" in result.reason


def test_a_plugin_guardrail_can_allow():
    def allow(tool_name, tool_args, ctx=None):
        return None

    with patch(
        "robothor.plugins.load_plugins",
        return_value=PluginSet(guardrails={"acme_policy": allow}),
    ):
        assert _engine(["acme_policy"]).check_pre_execution("exec", {}, "a").allowed is True


def test_an_unknown_policy_is_loud_not_silent(caplog):
    """A manifest naming a guardrail that does not exist enforces nothing.

    It must not do that quietly — that is a safety layer reading as protected
    while permitting everything.
    """
    caplog.set_level(logging.WARNING)
    with patch("robothor.plugins.load_plugins", return_value=PluginSet()):
        result = _engine(["policy_that_does_not_exist"]).check_pre_execution("exec", {}, "a")

    assert result.allowed is True, "unknown policies must not block real work"
    assert "policy_that_does_not_exist" in caplog.text, (
        "an unrecognised guardrail permitted the call with no warning at all"
    )


def test_a_broken_plugin_guardrail_does_not_break_the_call(caplog):
    """One bad package must not take the engine down, but must be reported."""

    def explode(tool_name, tool_args, ctx=None):
        raise RuntimeError("boom")

    caplog.set_level(logging.WARNING)
    with patch(
        "robothor.plugins.load_plugins",
        return_value=PluginSet(guardrails={"acme_policy": explode}),
    ):
        result = _engine(["acme_policy"]).check_pre_execution("exec", {}, "a")

    assert result.allowed is True
    assert "acme_policy" in caplog.text


def test_builtin_policies_are_unchanged():
    """The if-chain must still win for everything it already handles."""
    with patch("robothor.plugins.load_plugins", return_value=PluginSet()):
        eng = _engine(["no_destructive_writes"])
        assert eng.check_pre_execution("read_file", {"path": "/tmp/x"}, "a").allowed is True


def test_a_plugin_cannot_shadow_a_builtin_guardrail():
    """Otherwise a package could quietly neuter no_destructive_writes."""

    def neuter(tool_name, tool_args, ctx=None):
        return None

    with patch(
        "robothor.plugins.load_plugins",
        return_value=PluginSet(guardrails={"no_destructive_writes": neuter}),
    ):
        eng = _engine(["no_destructive_writes"])
        # The built-in must have been consulted, not the plugin's no-op.
        assert eng._plugin_guardrail("no_destructive_writes") is None
