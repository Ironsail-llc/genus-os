"""What tools this run gets, and how plan-mode wraps the prompt.

Extracted from `execute`, which is 1,132 lines. The ordering below is
load-bearing and was asserted nowhere:

    PLAN_MODE_PREAMBLE + system_prompt + PLAN_MODE_SUFFIX

The constraints go BEFORE the identity prompt and the reminder AFTER — a
sandwich — so plan-mode rules are not buried in the middle of SOUL.md's
directives. Appending both at the end, the obvious simplification, puts the
rules where a long identity prompt drowns them.

Adapter loading is deliberately non-fatal: an external MCP server that is down
must cost the agent that server's tools, not the whole run.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from robothor.engine.toolset_prep import prepare_toolset


class FakeRegistry:
    def __init__(self):
        self.registered = []
        self.register_adapter_tools = AsyncMock()

    def build_for_agent(self, config):
        return [{"name": "full"}]

    def get_tool_names(self, config):
        return ["exec", "read_file"]

    def build_readonly_for_agent(self, config):
        return [{"name": "readonly"}]

    def get_readonly_tool_names(self, config):
        return ["read_file"]


def _config(mcp=None):
    return SimpleNamespace(id="probe", mcp_servers=mcp or [])


async def _prep(**kw):
    return await prepare_toolset(
        kw.pop("registry", FakeRegistry()),
        kw.pop("config", _config()),
        agent_id=kw.pop("agent_id", "probe"),
        system_prompt=kw.pop("system_prompt", "IDENTITY"),
        readonly_mode=kw.pop("readonly_mode", False),
        deep_plan=kw.pop("deep_plan", False),
    )


# ── Normal mode ───────────────────────────────────────────────────────


async def test_a_normal_run_gets_the_full_toolset():
    with patch("robothor.engine.adapters.get_adapters_for_agent", return_value=[]):
        result = await _prep()

    assert result.tool_schemas == [{"name": "full"}]
    assert result.system_prompt == "IDENTITY"


# ── Plan mode: the sandwich ───────────────────────────────────────────


async def test_plan_mode_gets_only_read_only_tools():
    with patch("robothor.engine.adapters.get_adapters_for_agent", return_value=[]):
        result = await _prep(readonly_mode=True)

    assert result.tool_schemas == [{"name": "readonly"}]


async def test_the_plan_constraints_come_before_the_identity_prompt():
    """Appending both at the end lets a long SOUL.md bury the plan rules."""
    with patch("robothor.engine.adapters.get_adapters_for_agent", return_value=[]):
        result = await _prep(readonly_mode=True)

    assert result.system_prompt.index("IDENTITY") > 0, "nothing was prepended"
    assert result.system_prompt.endswith(
        result.system_prompt[result.system_prompt.index("IDENTITY") + len("IDENTITY") :]
    )
    assert result.system_prompt.count("IDENTITY") == 1


async def test_the_available_tools_are_named_in_the_preamble():
    """A plan written against tools the agent does not have is a plan that
    fails at execution time."""
    with patch("robothor.engine.adapters.get_adapters_for_agent", return_value=[]):
        result = await _prep(readonly_mode=True)

    assert "read_file" in result.system_prompt


async def test_deep_plan_uses_its_own_preamble():
    with patch("robothor.engine.adapters.get_adapters_for_agent", return_value=[]):
        deep = await _prep(readonly_mode=True, deep_plan=True)
        shallow = await _prep(readonly_mode=True, deep_plan=False)

    assert deep.system_prompt != shallow.system_prompt


# ── Adapters ──────────────────────────────────────────────────────────


async def test_manifest_mcp_servers_are_configured():
    """`v2.mcp_servers` was dead code until it was wired here."""
    registry = FakeRegistry()
    with (
        patch("robothor.engine.adapters.get_adapters_for_agent", return_value=[]),
        patch("robothor.engine.mcp_client.configure_mcp_servers") as configure,
    ):
        await _prep(registry=registry, config=_config(mcp=[{"name": "srv"}]))

    configure.assert_called_once_with([{"name": "srv"}])


async def test_adapters_are_registered_with_the_tool_registry():
    registry = FakeRegistry()
    with (
        patch("robothor.engine.adapters.get_adapters_for_agent", return_value=["a1"]),
        patch("robothor.engine.mcp_client.register_adapter") as register,
    ):
        await _prep(registry=registry)

    register.assert_called_once_with("a1")
    registry.register_adapter_tools.assert_awaited_once_with(["a1"])


async def test_no_adapters_means_no_registration_call():
    registry = FakeRegistry()
    with patch("robothor.engine.adapters.get_adapters_for_agent", return_value=[]):
        await _prep(registry=registry)

    registry.register_adapter_tools.assert_not_awaited()


async def test_a_dead_adapter_costs_its_tools_not_the_run():
    """An external MCP server being down must not take the agent with it."""
    registry = FakeRegistry()
    with patch(
        "robothor.engine.adapters.get_adapters_for_agent",
        side_effect=RuntimeError("mcp server unreachable"),
    ):
        result = await _prep(registry=registry)

    assert result.tool_schemas == [{"name": "full"}], "a dead adapter lost the whole toolset"


async def test_a_failing_adapter_registration_is_also_survivable():
    registry = FakeRegistry()
    registry.register_adapter_tools = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch("robothor.engine.adapters.get_adapters_for_agent", return_value=["a1"]),
        patch("robothor.engine.mcp_client.register_adapter"),
    ):
        result = await _prep(registry=registry)

    assert result.tool_names == ["exec", "read_file"]
