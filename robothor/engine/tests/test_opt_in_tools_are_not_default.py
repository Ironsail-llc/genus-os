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
    from robothor.sales.tool_schemas import SALES_SCHEMAS

    assert set(SALES_SCHEMAS) < OPT_IN_TOOLS
    assert OPT_IN_TOOLS - set(SALES_SCHEMAS) == {"web_render"}


def test_every_opt_in_tool_has_a_registered_schema(registry):
    """An opt-in name with no schema is a manifest key that silently does nothing."""
    assert OPT_IN_TOOLS <= set(registry._schemas)
