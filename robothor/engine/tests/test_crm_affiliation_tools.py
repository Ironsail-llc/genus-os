"""Agent tools for company hierarchy, outside ids and affiliations.

The DAL is mocked: these pin the tool surface (schemas, allow-listing, the
camelCase-to-DAL mapping, the tenant from the run's context, id validation
and how refusals come back) — the DAL's own behavior is covered against a
real database in tests/test_dal_crm_affiliations.py.
"""

from __future__ import annotations

from functools import partial
from unittest.mock import patch

import pytest

from robothor.engine.models import AgentConfig
from robothor.engine.tools import ToolRegistry
from robothor.engine.tools import _execute_tool as _execute_tool_impl

_execute_tool = partial(_execute_tool_impl, user_role="service")

PID = "33333333-3333-4333-8333-333333333333"
CID = "55555555-5555-4555-8555-555555555555"
NEW_TOOLS = (
    "set_affiliation",
    "list_affiliations",
    "list_company_children",
    "find_company_by_identifier",
    "find_person_by_identifier",
)
READ_TOOLS = NEW_TOOLS[1:]


def _registry() -> ToolRegistry:
    with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
        return ToolRegistry()


class TestSchemas:
    @pytest.mark.parametrize("name", NEW_TOOLS)
    def test_registered_with_camel_case_arguments(self, name):
        schema = _registry()._schemas[name]["function"]
        assert schema["name"] == name
        props = schema["parameters"]["properties"]
        assert all("_" not in p for p in props), props

    def test_set_affiliation_requires_person_company_and_role(self):
        params = _registry()._schemas["set_affiliation"]["function"]["parameters"]
        assert params["required"] == ["personId", "companyId", "role"]
        assert {"attributes", "isActive"} <= set(params["properties"])

    def test_get_contact_360_also_takes_a_company(self):
        props = _registry()._schemas["get_contact_360"]["function"]["parameters"]["properties"]
        assert "companyId" in props
        assert "id" in props and "identifier" in props  # existing arguments kept

    def test_only_agents_allowed_the_tools_get_them(self):
        r = _registry()
        allowed = r.build_for_agent(
            AgentConfig(id="a", name="a", tools_allowed=["set_affiliation", "list_affiliations"])
        )
        names = {t["function"]["name"] for t in allowed}
        assert {"set_affiliation", "list_affiliations"} <= names
        other = r.build_for_agent(AgentConfig(id="b", name="b", tools_allowed=["list_people"]))
        assert not set(NEW_TOOLS) & {t["function"]["name"] for t in other}

    def test_reads_are_read_only_and_the_write_is_not(self):
        from robothor.engine.tools.constants import READONLY_TOOLS

        assert set(READ_TOOLS) <= READONLY_TOOLS
        assert "set_affiliation" not in READONLY_TOOLS

    def test_the_write_is_refused_in_a_benchmark_run(self):
        from robothor.engine.tools.handlers.crm import _CRM_MUTATING_TOOLS

        assert "set_affiliation" in _CRM_MUTATING_TOOLS


class TestSetAffiliation:
    @pytest.mark.asyncio
    async def test_maps_arguments_and_uses_the_run_tenant(self):
        out = {"id": "a1", "role": "owner", "created": True, "changed": True}
        with patch("robothor.crm.dal.set_affiliation", return_value=out) as mock:
            result = await _execute_tool(
                "set_affiliation",
                {
                    "personId": PID,
                    "companyId": CID,
                    "role": "owner",
                    "attributes": {"k": 1},
                    "isActive": False,
                },
                tenant_id="tenant-a",
            )
        assert result == out
        mock.assert_called_once_with(
            PID, CID, "owner", attributes={"k": 1}, is_active=False, tenant_id="tenant-a"
        )

    @pytest.mark.asyncio
    async def test_defaults_to_active_and_keeps_attributes(self):
        with patch("robothor.crm.dal.set_affiliation", return_value={"id": "a1"}) as mock:
            await _execute_tool(
                "set_affiliation",
                {"personId": PID, "companyId": CID, "role": "owner"},
                tenant_id="t",
            )
        assert mock.call_args.kwargs == {"attributes": None, "is_active": True, "tenant_id": "t"}

    @pytest.mark.asyncio
    async def test_a_refusal_comes_back_as_an_error(self):
        with patch(
            "robothor.crm.dal.set_affiliation", side_effect=ValueError("company x not found")
        ):
            result = await _execute_tool(
                "set_affiliation",
                {"personId": PID, "companyId": CID, "role": "owner"},
                tenant_id="t",
            )
        assert result == {"error": "company x not found"}

    @pytest.mark.asyncio
    async def test_a_made_up_id_is_refused_before_the_dal(self):
        with patch("robothor.crm.dal.set_affiliation") as mock:
            result = await _execute_tool(
                "set_affiliation",
                {"personId": "alice@example.com", "companyId": CID, "role": "owner"},
                tenant_id="t",
            )
        assert "error" in result and "not a valid id" in result["error"]
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_missing_company_is_refused_before_the_dal(self):
        with patch("robothor.crm.dal.set_affiliation") as mock:
            result = await _execute_tool(
                "set_affiliation", {"personId": PID, "role": "owner"}, tenant_id="t"
            )
        assert "error" in result
        mock.assert_not_called()


class TestReads:
    @pytest.mark.asyncio
    async def test_list_affiliations(self):
        rows = [{"id": "a1"}]
        page = {"affiliations": rows, "total": 1, "offset": 0, "limit": 200, "hasMore": False}
        with patch("robothor.crm.dal.list_affiliations_page", return_value=page) as mock:
            result = await _execute_tool(
                "list_affiliations",
                {"companyId": CID, "role": "provider", "activeOnly": False},
                tenant_id="t",
            )
        assert result["affiliations"] == rows and result["count"] == 1
        assert mock.call_args.kwargs == {
            "person_id": None,
            "company_id": CID,
            "role": "provider",
            "active_only": False,
            "limit": 200,
            "offset": 0,
            "tenant_id": "t",
        }

    @pytest.mark.asyncio
    async def test_list_affiliations_needs_a_person_or_company(self):
        with patch("robothor.crm.dal.list_affiliations_page") as mock:
            result = await _execute_tool("list_affiliations", {}, tenant_id="t")
        assert "error" in result
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_company_children(self):
        with patch("robothor.crm.dal.list_company_children", return_value=[{"id": "c2"}]) as mock:
            result = await _execute_tool("list_company_children", {"companyId": CID}, tenant_id="t")
        assert result == {"companyId": CID, "children": [{"id": "c2"}], "count": 1}
        mock.assert_called_once_with(CID, tenant_id="t")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool,dal,key",
        [
            ("find_company_by_identifier", "get_company_by_identifier", "company"),
            ("find_person_by_identifier", "get_person_by_identifier", "person"),
        ],
    )
    async def test_find_by_identifier(self, tool, dal, key):
        with patch(f"robothor.crm.dal.{dal}", return_value={"id": "x"}) as mock:
            found = await _execute_tool(
                tool, {"system": "test-system", "identifier": "x-1"}, tenant_id="t"
            )
        assert found == {"found": True, key: {"id": "x"}}
        mock.assert_called_once_with("test-system", "x-1", tenant_id="t")
        with patch(f"robothor.crm.dal.{dal}", return_value=None):
            missing = await _execute_tool(
                tool, {"system": "test-system", "identifier": "nope"}, tenant_id="t"
            )
        assert missing == {"found": False, key: None}
        with patch(f"robothor.crm.dal.{dal}") as mock:
            bad = await _execute_tool(tool, {"system": "test-system"}, tenant_id="t")
        assert "error" in bad
        mock.assert_not_called()


class TestContact360:
    @pytest.mark.asyncio
    async def test_a_company_returns_its_structure(self):
        view = {"company": {"id": CID}, "parent": None, "children": [], "affiliations": []}
        with patch("robothor.crm.dal.get_company_view", return_value=view) as mock:
            result = await _execute_tool("get_contact_360", {"companyId": CID}, tenant_id="t")
        assert result == view
        mock.assert_called_once_with(CID, tenant_id="t")

    @pytest.mark.asyncio
    async def test_an_unknown_company_is_an_error(self):
        with patch("robothor.crm.dal.get_company_view", return_value=None):
            result = await _execute_tool("get_contact_360", {"companyId": CID}, tenant_id="t")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_a_person_still_goes_to_the_person_view(self):
        with (
            patch("robothor.crm.dal.get_contact_360", return_value={"person": {}}) as person,
            patch("robothor.crm.dal.get_company_view") as company,
        ):
            await _execute_tool("get_contact_360", {"id": PID}, tenant_id="t")
        person.assert_called_once()
        company.assert_not_called()


class TestPaging:
    @pytest.mark.asyncio
    async def test_list_affiliations_pages_and_reports_the_total(self):
        page = {
            "affiliations": [{"id": "a1", "personId": PID}],
            "total": 600,
            "offset": 100,
            "limit": 1,
            "hasMore": True,
        }
        with patch("robothor.crm.dal.list_affiliations_page", return_value=page) as mock:
            result = await _execute_tool(
                "list_affiliations",
                {"companyId": CID, "limit": 1, "offset": 100},
                tenant_id="t",
            )
        assert result == {
            "affiliations": page["affiliations"],
            "count": 1,
            "total": 600,
            "offset": 100,
            "limit": 1,
            "hasMore": True,
        }
        assert mock.call_args.kwargs["offset"] == 100
        assert mock.call_args.kwargs["limit"] == 1

    def test_the_schema_offers_offset(self):
        props = _registry()._schemas["list_affiliations"]["function"]["parameters"]["properties"]
        assert "offset" in props
