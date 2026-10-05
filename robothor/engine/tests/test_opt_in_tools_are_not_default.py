"""Ten sales tools and a headless browser were advertised to every agent.

``get_engine_schemas`` ends ``return schemas | SALES_SCHEMAS`` unconditionally,
and ``_get_filtered_names`` hands an agent with no ``tools_allowed`` everything
the registry holds. So a default agent on an instance with no sales deployment
was advertised all ten ``sales_*`` tools plus ``web_render`` — measured at 195
tools / 113,924 characters, of which 6,984 characters are these eleven.

RBAC did not stop the calls either: ``__default__|service|*|allow`` and
``__default__|user|*|allow`` mean migration 134's ``web_render`` row for
``sales_research_agent`` is an additive GRANT, not a gate, despite its comment
claiming "only the bounded research role gains this permission". Neither
``sales_discover`` nor ``sales_propose_email`` sends anything, but both are
unbounded CRM writes.

Two halves, so two gates: not advertised unless a manifest asks for it
(here), and denied by default for the broad roles (migration 138, covered by
``robothor/tests/test_migration_138_opt_in_denies.py``).
"""

from __future__ import annotations

import pytest

from robothor.engine.models import AgentConfig
from robothor.engine.tools.constants import OPT_IN_TOOLS
from robothor.engine.tools.registry import ToolRegistry


@pytest.fixture(scope="module")
def registry():
    return ToolRegistry()


def test_a_default_agent_is_advertised_no_sales_tool_and_no_browser(registry):
    names = set(registry.get_tool_names(AgentConfig(id="probe", name="Probe")))

    assert not names & OPT_IN_TOOLS
    assert not {n for n in names if n.startswith("sales_")}
    assert "web_render" not in names


def test_a_manifest_that_asks_for_them_still_gets_them(registry):
    """The sales fleet declares an explicit bounded tools_allowed; that must work."""
    config = AgentConfig(
        id="sales-research-worker",
        name="Research worker",
        tools_allowed=["web_fetch", "web_render", "sales_get_prospect", "sales_get_context"],
    )

    names = set(registry.get_tool_names(config))

    assert {"web_render", "sales_get_prospect", "sales_get_context"} <= names


def test_tools_denied_still_beats_an_explicit_request(registry):
    config = AgentConfig(
        id="probe",
        name="Probe",
        tools_allowed=["web_fetch", "web_render"],
        tools_denied=["web_render"],
    )

    assert "web_render" not in set(registry.get_tool_names(config))


def test_the_opt_in_list_cannot_drift_from_the_sales_package():
    """A hand-maintained name list drifted once already (2026-08-22)."""
    from robothor.engine.tools.constants import CLAUDE_CODE_TOOLS
    from robothor.sales.tool_schemas import SALES_SCHEMAS

    assert set(SALES_SCHEMAS) < OPT_IN_TOOLS
    assert OPT_IN_TOOLS - set(SALES_SCHEMAS) == {"web_render"} | CLAUDE_CODE_TOOLS


def test_every_opt_in_tool_has_a_registered_schema(registry):
    """An opt-in name with no schema is a manifest key that silently does nothing."""
    assert set(registry._schemas) >= OPT_IN_TOOLS


def test_tools_opt_in_adds_an_opt_in_tool_to_the_default_set(registry):
    """`tools_opt_in` is additive: the default set, plus the named opt-in tools.

    The main template declares no `tools_allowed` (it gets the default set) and
    still has to be able to drive Claude Code, which is opt-in.
    """
    from robothor.engine.tools.constants import CLAUDE_CODE_TOOLS

    default = set(registry.get_tool_names(AgentConfig(id="probe", name="Probe")))
    config = AgentConfig(id="probe", name="Probe", tools_opt_in=sorted(CLAUDE_CODE_TOOLS))

    names = set(registry.get_tool_names(config))

    assert names == default | CLAUDE_CODE_TOOLS
    assert not names & (OPT_IN_TOOLS - CLAUDE_CODE_TOOLS)


def test_tools_opt_in_cannot_resurrect_a_denied_tool(registry):
    config = AgentConfig(
        id="probe",
        name="Probe",
        tools_opt_in=["claude_code_start"],
        tools_denied=["claude_code_*"],
    )
    assert "claude_code_start" not in set(registry.get_tool_names(config))
