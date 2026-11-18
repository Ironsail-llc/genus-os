"""An agent that can reach child tenants is shown how to look at them.

The operator-facing agent of a parent tenant was asked how its companies were
doing. It searched its tool list for words like "tenant", "organization" and
"portfolio" and found nothing that could answer. Knowing the child tenants
exist is not enough: the agent must be told which tool reports on them, hold
that tool without searching for it, and find it when it does search.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from robothor.engine.org_context import organisation_block
from robothor.engine.tools.keywords import TOOL_HINTS

PARENT = "acme-parent"
CHILD_A = "acme-child-a"
CHILD_B = "acme-child-b"

_TENANTS: dict[str, dict[str, Any]] = {
    PARENT: {"id": PARENT, "displayName": "Acme Holdings", "parentTenantId": None},
    CHILD_A: {"id": CHILD_A, "displayName": "Acme Child A", "parentTenantId": PARENT},
    CHILD_B: {"id": CHILD_B, "displayName": "Acme Child B", "parentTenantId": PARENT},
}

_REPO = Path(__file__).resolve().parents[3]
_MAIN_TEMPLATE = _REPO / "templates" / "agents" / "core" / "main"


@pytest.fixture
def tenant_tree():
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


class TestOrganisationBlockNamesTheTools:
    def test_a_run_that_reaches_children_is_told_to_call_org_overview(self, tenant_tree) -> None:
        block = organisation_block(PARENT, [PARENT, CHILD_A, CHILD_B])
        assert "org_overview" in block

    def test_a_run_that_reaches_children_is_told_about_the_tenant_argument(
        self, tenant_tree
    ) -> None:
        block = organisation_block(PARENT, [PARENT, CHILD_A, CHILD_B])
        assert "`tenant`" in block

    def test_a_run_confined_to_its_tenant_is_not_pointed_at_child_tools(self, tenant_tree) -> None:
        block = organisation_block(PARENT, [PARENT])
        assert "org_overview" not in block
        assert "`tenant`" not in block


@pytest.fixture(scope="module")
def search():
    from robothor.engine.tools.registry import ToolRegistry

    registry = ToolRegistry()
    names = list(registry._schemas)
    return lambda query, n=3: [h["name"] for h in registry.search_tools(names, query, limit=n)]


class TestOrgOverviewIsFoundBySearch:
    """Found by the words for a tenant tree, without taking the CRM's words.

    "Company", "organisation" and "business" belong to the CRM's company
    tools: a tool that also claimed them pushed ``get_company`` out of the
    results for "look up a company by name". An agent with child tenants holds
    this tool directly and is told about it in its context, so search only has
    to find it by the vocabulary that is its own.
    """

    @pytest.mark.parametrize(
        "query",
        [
            "tenant list children organization cross-tenant parent fleet monitoring rollup",
            "organization portfolio list tenants org head rollup cross-tenant",
            "holdings subsidiaries portfolio overview",
        ],
    )
    def test_a_search_in_tenant_tree_words_finds_it_first(self, search, query: str) -> None:
        assert search(query, n=1) == ["org_overview"]

    def test_it_does_not_claim_the_crm_company_vocabulary(self) -> None:
        hint = TOOL_HINTS["org_overview"]
        assert not {"company", "organisation", "organization", "business"} & set(hint.keywords)

    def test_looking_up_a_company_still_finds_the_crm_getter(self, search) -> None:
        assert "get_company" in search("look up a company by name")
        assert "org_overview" not in search("look up a company by name")


class TestTheOperatorFacingAgentHoldsIt:
    def test_main_holds_org_overview_without_searching(self) -> None:
        # The template carries `{{ placeholders }}`, so it is not YAML until
        # rendered: take the one block this test is about.
        text = (_MAIN_TEMPLATE / "manifest.template.yaml").read_text()
        block = text.split("\ntools_eager:\n", 1)[1].split("\n\n", 1)[0]
        assert "org_overview" in yaml.safe_load(block)

    def test_mains_instructions_name_it_for_a_company_check_in(self) -> None:
        text = (_MAIN_TEMPLATE / "instructions.template.md").read_text()
        assert "org_overview" in text
