"""A run that reaches a child company is handed its remote tools; others pay nothing.

``org_tools``, ``org_tool_read`` and ``org_tool_write`` are useful only to a
run that can reach another tenant. Advertising them to every run cost every
agent on every instance two schemas and their prompt text on every turn,
child tenants or not. So they are not core: a run whose reach includes another
tenant is offered them up front, the ORGANISATION block (which only such a run
gets) names them, and any other run can still find them through tool search.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from robothor.engine.org_context import organisation_block
from robothor.engine.tools.keywords import TOOL_HINTS

PARENT = "acme-parent"
CHILD_A = "acme-child-a"
CHILD_B = "acme-child-b"
REMOTE = ("org_tools", "org_tool_read", "org_tool_write")

_TENANTS: dict[str, dict[str, Any]] = {
    PARENT: {"id": PARENT, "displayName": "Acme Holdings", "parentTenantId": None},
    CHILD_A: {"id": CHILD_A, "displayName": "Acme Child A", "parentTenantId": PARENT},
    CHILD_B: {"id": CHILD_B, "displayName": "Acme Child B", "parentTenantId": PARENT},
}

_REPO = Path(__file__).resolve().parents[3]
_MAIN_TEMPLATE = _REPO / "templates" / "agents" / "core" / "main"


@pytest.fixture
def tenant_tree():
    from robothor.engine.org_context import clear_cache

    clear_cache()
    with (
        patch("robothor.crm.dal.get_tenant", side_effect=_TENANTS.get),
        patch(
            "robothor.crm.dal.list_tenants",
            side_effect=lambda parent_id=None, active_only=True: [
                t for t in _TENANTS.values() if t["parentTenantId"] == parent_id
            ],
        ),
    ):
        yield
    clear_cache()


class TestOrganisationBlockNamesTheTools:
    def test_a_run_that_reaches_children_is_told_about_their_own_tools(self, tenant_tree) -> None:
        block = organisation_block(PARENT, [PARENT, CHILD_A, CHILD_B])
        for name in REMOTE:
            assert name in block

    def test_a_run_confined_to_its_tenant_is_not_pointed_at_them(self, tenant_tree) -> None:
        block = organisation_block(PARENT, [PARENT])
        for name in REMOTE:
            assert name not in block


@pytest.fixture(scope="module")
def search():
    from robothor.engine.tools.registry import ToolRegistry

    registry = ToolRegistry()
    names = list(registry._schemas)
    return lambda query, n=3: [h["name"] for h in registry.search_tools(names, query, limit=n)]


class TestTheyAreFoundBySearch:
    @pytest.mark.parametrize(
        "query",
        [
            "child tenant connector integration plugin remote catalogue",
            "what connectors does the child tenant have",
        ],
    )
    def test_the_catalogue_is_found_in_connector_words(self, search, query: str) -> None:
        assert "org_tools" in search(query)

    def test_the_read_is_found(self, search) -> None:
        assert "org_tool_read" in search("read through a child tenant connector remote")

    def test_the_write_is_found(self, search) -> None:
        assert "org_tool_write" in search("write change through a child tenant connector remote")

    def test_they_do_not_claim_the_crm_company_vocabulary(self) -> None:
        for name in REMOTE:
            assert not {"company", "organisation", "organization", "business"} & set(
                TOOL_HINTS[name].keywords
            )


class TestNoRunPaysForThemUnlessItReachesAChild:
    def test_they_are_not_core(self) -> None:
        from robothor.engine.tools.constants import CORE_TOOLS

        assert not set(REMOTE) & CORE_TOOLS

    def test_main_does_not_hold_them_eagerly(self) -> None:
        text = (_MAIN_TEMPLATE / "manifest.template.yaml").read_text()
        block = text.split("\ntools_eager:\n", 1)[1].split("\n\n", 1)[0]
        assert not set(REMOTE) & set(yaml.safe_load(block))

    def test_mains_instructions_leave_them_to_the_organisation_block(self) -> None:
        text = (_MAIN_TEMPLATE / "instructions.template.md").read_text()
        for name in REMOTE:
            assert name not in text


def _main():
    from robothor.engine.config import manifest_to_agent_config

    text = (_MAIN_TEMPLATE / "manifest.template.yaml").read_text()
    return manifest_to_agent_config(yaml.safe_load(text.replace("{{ timezone }}", "UTC")))


async def _prepare(accessible: tuple[str, ...] | None) -> Any:
    from robothor.engine.tools.registry import ToolRegistry
    from robothor.engine.toolset_prep import prepare_toolset

    registry = ToolRegistry()
    config = _main()
    assert registry.should_defer(config), "main is a deferred agent"
    with (
        patch("robothor.engine.toolset_prep._autonomy_active", AsyncMock(return_value=False)),
        patch("robothor.engine.toolset_prep._load_adapters", AsyncMock(return_value=None)),
        patch(
            "robothor.engine.recent_tools.recent_tool_names",
            AsyncMock(return_value=frozenset()),
        ),
    ):
        return await prepare_toolset(
            registry,
            config,
            agent_id="main",
            system_prompt="",
            readonly_mode=False,
            deep_plan=False,
            tenant_id=PARENT,
            accessible_tenant_ids=accessible,
        )


def _advertised(prepared: Any) -> set[str]:
    return {str(s["function"]["name"]) for s in prepared.tool_schemas}


@pytest.mark.asyncio
@pytest.mark.parametrize("accessible", [None, (), (PARENT,)])
async def test_a_run_that_reaches_no_other_tenant_is_not_offered_them(accessible) -> None:
    prepared = await _prepare(accessible)

    assert not set(REMOTE) & _advertised(prepared)
    # Still reachable through tool search / tool_call.
    assert set(REMOTE) <= set(prepared.reachable_names)


@pytest.mark.asyncio
async def test_a_run_that_reaches_a_child_is_offered_them_up_front() -> None:
    prepared = await _prepare((PARENT, CHILD_A))

    assert set(REMOTE) <= _advertised(prepared)


@pytest.mark.asyncio
async def test_reaching_a_child_adds_only_the_remote_tools() -> None:
    alone = _advertised(await _prepare((PARENT,)))
    reaching = _advertised(await _prepare((PARENT, CHILD_A)))

    assert reaching - alone == set(REMOTE)
