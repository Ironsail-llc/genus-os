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


class TestOrgOverviewIsFoundBySearch:
    @pytest.mark.parametrize(
        "word",
        ["tenant", "company", "organisation", "organization", "business", "portfolio", "fleet"],
    )
    def test_the_words_an_owner_uses_are_in_its_vocabulary(self, word: str) -> None:
        hint = TOOL_HINTS.get("org_overview")
        assert hint is not None, "org_overview has no search vocabulary"
        assert word in hint.keywords

    def test_it_outranks_its_siblings_for_a_company_check_in(self) -> None:
        hint = TOOL_HINTS["org_overview"]
        assert hint.rank_bias > 0


class TestTheOperatorFacingAgentHoldsIt:
    def test_main_holds_org_overview_without_searching(self) -> None:
        manifest = yaml.safe_load((_MAIN_TEMPLATE / "manifest.template.yaml").read_text())
        assert "org_overview" in manifest["tools_eager"]

    def test_mains_instructions_name_it_for_a_company_check_in(self) -> None:
        text = (_MAIN_TEMPLATE / "instructions.template.md").read_text()
        assert "org_overview" in text
