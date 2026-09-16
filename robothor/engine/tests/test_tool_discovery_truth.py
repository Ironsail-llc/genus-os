"""What the agent is told about how to reach a tool it cannot see.

Under deferred loading (Rip 16) ``main`` is advertised 18 schemas out of 98.
None of them is a Gmail or Calendar tool; ``exec`` is one of them. The instance
documentation says "REPLYING? → gws_gmail_reply(thread_id, body)". Nothing —
not ``prompts.py``, not the instruction contract, not the engine preamble —
told the agent that its toolset was deferred or that ``gws_gmail_reply`` is
reached through ``tool_call``. So the cheapest way to obey the documentation
with the schemas in front of it was ``exec("gog gmail send …")``: the one path
the same documentation bans.

The planner had the mirror-image problem: ``toolset_prep`` handed it all 98
names while the executor got the 18 schemas, so it wrote plans naming tools
the executing turn could not see.
"""

from __future__ import annotations

import pytest

from robothor.engine.models import AgentConfig
from robothor.engine.tools.constants import CORE_TOOLS, GWS_TOOLS
from robothor.engine.tools.registry import ToolRegistry
from robothor.engine.toolset_prep import prepare_toolset


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return ToolRegistry()


def _broad_config(registry: ToolRegistry) -> AgentConfig:
    """An agent broad enough to trip the deferral threshold."""
    names = sorted(set(registry._schemas))[:80]
    names = sorted({*names, *GWS_TOOLS, *CORE_TOOLS} & set(registry._schemas))
    return AgentConfig(id="broad-fixture", name="Broad", tools_allowed=names)


def _narrow_config() -> AgentConfig:
    return AgentConfig(
        id="narrow-fixture", name="Narrow", tools_allowed=["read_file", "write_file", "exec"]
    )


@pytest.fixture
def deferring(monkeypatch: pytest.MonkeyPatch) -> None:
    import robothor.engine.feature_flags as flags

    monkeypatch.setattr(flags, "deferred_tools_enabled", lambda: True)
    monkeypatch.setattr(flags, "deferred_tools_threshold", lambda: 40)


# ── The preamble sentence ─────────────────────────────────────────────


class TestDeferredToolsetNote:
    @pytest.mark.asyncio
    async def test_a_deferred_run_is_told_how_to_reach_the_rest(
        self, registry: ToolRegistry, deferring: None
    ) -> None:
        config = _broad_config(registry)
        prepared = await prepare_toolset(
            registry,
            config,
            agent_id=config.id,
            system_prompt="",
            readonly_mode=False,
            deep_plan=False,
        )

        visible = len(prepared.tool_schemas)
        reachable = len(prepared.reachable_names)
        assert reachable > 0
        assert prepared.discovery_note == (
            f"{visible} tools are in your toolset; {reachable} more are reachable: "
            "call tool_search(query) then tool_call(name, arguments)"
        )

    @pytest.mark.asyncio
    async def test_the_reachable_set_is_what_is_missing_from_the_toolset(
        self, registry: ToolRegistry, deferring: None
    ) -> None:
        config = _broad_config(registry)
        prepared = await prepare_toolset(
            registry,
            config,
            agent_id=config.id,
            system_prompt="",
            readonly_mode=False,
            deep_plan=False,
        )
        advertised = {s["function"]["name"] for s in prepared.tool_schemas}

        assert set(prepared.reachable_names).isdisjoint(advertised)
        assert set(prepared.reachable_names) | advertised >= set(prepared.tool_names)
        assert "gws_gmail_reply" in prepared.reachable_names

    @pytest.mark.asyncio
    async def test_a_run_that_is_not_deferred_is_told_nothing(
        self, registry: ToolRegistry, deferring: None
    ) -> None:
        """A note about a mechanism that is not running is a lie with tokens."""
        prepared = await prepare_toolset(
            registry,
            _narrow_config(),
            agent_id="narrow-fixture",
            system_prompt="",
            readonly_mode=False,
            deep_plan=False,
        )
        assert prepared.discovery_note == ""
        assert prepared.reachable_names == ()


# ── Who the two principals are ────────────────────────────────────────


class TestPrincipalsNote:
    """The assistant has its own Google account. Nothing in a run said so.

    On 2026-09-16 the model wrote the operator's itinerary to `primary` — its
    OWN calendar — added the operator as an attendee and reported it done.
    Every step is locally reasonable for an agent that believes "my calendar"
    and "the operator's calendar" name one thing.
    """

    @pytest.fixture
    def identities(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        import yaml

        owner = tmp_path / "owner.yaml"
        owner.write_text(
            yaml.safe_dump(
                {
                    "tenant_id": "fixture",
                    "first_name": "Alice",
                    "last_name": "Example",
                    "email": "alice@example.com",
                }
            )
        )
        monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(owner))
        monkeypatch.setenv("ROBOTHOR_AI_EMAIL", "bot@example.com")

    def test_it_names_both_accounts(self, identities: None) -> None:
        from robothor.engine.toolset_prep import principals_note

        note = principals_note()
        assert "bot@example.com" in note
        assert "alice@example.com" in note
        assert "Alice Example" in note

    def test_it_says_whose_my_calendar_is(self, identities: None) -> None:
        from robothor.engine.toolset_prep import principals_note

        note = principals_note()
        assert "'my calendar'" in note
        assert "landed in their account" in note

    def test_with_nothing_configured_it_says_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from robothor.engine.toolset_prep import principals_note

        monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(tmp_path / "absent.yaml"))
        monkeypatch.delenv("ROBOTHOR_OWNER_EMAIL", raising=False)
        monkeypatch.delenv("ROBOTHOR_AI_EMAIL", raising=False)
        assert principals_note() == ""

    def test_no_address_is_hardcoded(self) -> None:
        import inspect

        from robothor.engine.toolset_prep import principals_note

        assert "@example" not in inspect.getsource(principals_note)

    @pytest.mark.asyncio
    async def test_it_reaches_the_engine_context_turn(
        self, registry: ToolRegistry, deferring: None, identities: None
    ) -> None:
        from robothor.engine.toolset_prep import with_discovery_note

        config = _broad_config(registry)
        prepared = await prepare_toolset(
            registry,
            config,
            agent_id=config.id,
            system_prompt="",
            readonly_mode=False,
            deep_plan=False,
        )
        turn = with_discovery_note("CURRENT USER: ...", prepared)

        assert turn.startswith("CURRENT USER: ...")
        assert "bot@example.com" in turn
        assert prepared.discovery_note in turn

    @pytest.mark.asyncio
    async def test_a_non_deferred_run_still_learns_who_is_who(
        self, registry: ToolRegistry, deferring: None, identities: None
    ) -> None:
        """Identity is not a deferral concern — every run needs it."""
        from robothor.engine.toolset_prep import with_discovery_note

        prepared = await prepare_toolset(
            registry,
            _narrow_config(),
            agent_id="narrow-fixture",
            system_prompt="",
            readonly_mode=False,
            deep_plan=False,
        )
        turn = with_discovery_note("", prepared)

        assert "alice@example.com" in turn
        assert "tool_search" not in turn


# ── The planner and the executor see the same toolset ─────────────────


class TestPlannerAgreement:
    @pytest.mark.asyncio
    async def test_the_planner_is_told_which_names_need_tool_call(
        self, registry: ToolRegistry, deferring: None
    ) -> None:
        """`toolset_prep` handed the planner 98 names and the executor 18
        schemas, so plans named tools the executing turn could not call."""
        from robothor.engine.toolset_prep import planner_tool_names

        config = _broad_config(registry)
        prepared = await prepare_toolset(
            registry,
            config,
            agent_id=config.id,
            system_prompt="",
            readonly_mode=False,
            deep_plan=False,
        )
        names = planner_tool_names(prepared)
        advertised = {s["function"]["name"] for s in prepared.tool_schemas}

        assert set(prepared.tool_names) <= {n.split(" ")[0] for n in names}
        marked = [n for n in names if "tool_call" in n]
        assert marked, names[:5]
        for entry in names:
            base = entry.split(" ")[0]
            if base in advertised:
                assert "tool_call" not in entry, entry
            elif base in prepared.reachable_names:
                assert "tool_call" in entry, entry

    @pytest.mark.asyncio
    async def test_a_non_deferred_planner_list_is_plain_names(
        self, registry: ToolRegistry, deferring: None
    ) -> None:
        from robothor.engine.toolset_prep import planner_tool_names

        prepared = await prepare_toolset(
            registry,
            _narrow_config(),
            agent_id="narrow-fixture",
            system_prompt="",
            readonly_mode=False,
            deep_plan=False,
        )
        assert planner_tool_names(prepared) == prepared.tool_names


# ── tool_search on a run that is not deferred ─────────────────────────


class TestToolSearchOutsideDeferral:
    @pytest.mark.asyncio
    async def test_it_answers_with_the_agents_own_tools(self) -> None:
        """ "only available on deferred runs" was returned on 18 of main's runs
        in a week. The agent keeps calling it because it worked last time, and
        the refusal teaches it nothing about what it does have."""
        from robothor.engine.tools.dispatch import (
            ToolContext,
            clear_agent_toolset,
            set_agent_toolset,
        )
        from robothor.engine.tools.handlers.toolsearch import HANDLERS

        token = set_agent_toolset(frozenset({"gws_gmail_search", "read_file", "write_file"}))
        try:
            out = await HANDLERS["tool_search"]({"query": "check my email"}, ToolContext())
        finally:
            clear_agent_toolset(token)

        assert "error" not in out
        assert out["results"][0]["name"] == "gws_gmail_search"
        assert out["results"][0]["in_toolset"] is True
        assert "already in your toolset" in out["hint"]

    @pytest.mark.asyncio
    async def test_a_deferred_run_still_says_how_to_call_what_it_found(self) -> None:
        from robothor.engine.tools.dispatch import (
            ToolContext,
            clear_deferred_allowed,
            set_deferred_allowed,
        )
        from robothor.engine.tools.handlers.toolsearch import HANDLERS

        token = set_deferred_allowed(frozenset({"gws_gmail_search", "read_file"}))
        try:
            out = await HANDLERS["tool_search"]({"query": "check my email"}, ToolContext())
        finally:
            clear_deferred_allowed(token)

        assert out["results"][0]["name"] == "gws_gmail_search"
        assert out["results"][0]["in_toolset"] is False
        assert "tool_describe" in out["hint"]

    @pytest.mark.asyncio
    async def test_a_run_with_no_published_toolset_is_not_an_error(self) -> None:
        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers.toolsearch import HANDLERS

        out = await HANDLERS["tool_search"]({"query": "check my email"}, ToolContext())
        assert "error" not in out
        assert out["results"] == []

    @pytest.mark.asyncio
    async def test_an_absent_capability_is_named_rather_than_approximated(self) -> None:
        from robothor.engine.tools.dispatch import (
            ToolContext,
            clear_agent_toolset,
            set_agent_toolset,
        )
        from robothor.engine.tools.handlers.toolsearch import HANDLERS

        token = set_agent_toolset(frozenset({"exec", "read_file", "list_my_tasks"}))
        try:
            out = await HANDLERS["tool_search"]({"query": "my google tasks"}, ToolContext())
        finally:
            clear_agent_toolset(token)

        assert "no Google Tasks tool" in out["note"]


# ── A wrong name gets the right ones back ─────────────────────────────


class TestUnknownNameSuggestions:
    @pytest.mark.asyncio
    async def test_tool_describe_suggests_the_closest_names(self) -> None:
        from robothor.engine.tools.dispatch import (
            ToolContext,
            clear_deferred_allowed,
            set_deferred_allowed,
        )
        from robothor.engine.tools.handlers.toolsearch import HANDLERS

        token = set_deferred_allowed(frozenset({*GWS_TOOLS, "read_file"}))
        try:
            out = await HANDLERS["tool_describe"]({"name": "gws_gmail_draft"}, ToolContext())
        finally:
            clear_deferred_allowed(token)

        assert "did_you_mean" in out
        assert len(out["did_you_mean"]) <= 3
        assert any(n.startswith("gws_gmail") for n in out["did_you_mean"]), out

    @pytest.mark.asyncio
    async def test_tool_call_suggests_the_closest_names(self) -> None:
        from robothor.engine.tools.dispatch import (
            ToolContext,
            clear_deferred_allowed,
            set_deferred_allowed,
        )
        from robothor.engine.tools.handlers.toolsearch import HANDLERS

        token = set_deferred_allowed(frozenset({*GWS_TOOLS, "read_file"}))
        try:
            out = await HANDLERS["tool_call"](
                {"name": "gws_calendar_update", "arguments": {}}, ToolContext()
            )
        finally:
            clear_deferred_allowed(token)

        assert "did_you_mean" in out
        assert any(n.startswith("gws_calendar") for n in out["did_you_mean"]), out
