"""The ``list_tasks`` tool carries the case filters to the DAL.

``overdue`` and ``unowned`` are documented in ``docs/CRM_DATA_FABRIC.md``
("Cases"); the DAL side is tested in ``robothor/crm/tests/test_dal_cases.py``.
Both dispatchers are covered: the engine handler and the MCP surface.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestListTasksToolForwardsTheFilters:
    @pytest.mark.asyncio
    async def test_engine_handler_forwards_overdue_and_unowned(self) -> None:
        from robothor.engine.tools.dispatch import _execute_tool

        with patch("robothor.crm.dal.list_tasks") as mock_list:
            mock_list.return_value = []
            await _execute_tool(
                "list_tasks",
                {"overdue": True, "unowned": True},
                tenant_id="tenant-a",
                user_id="user-1",
                user_role="owner",
            )
        assert mock_list.call_args.kwargs.get("overdue") is True
        assert mock_list.call_args.kwargs.get("unowned") is True

    @pytest.mark.asyncio
    async def test_mcp_handler_forwards_overdue_and_unowned(self) -> None:
        from robothor.api.mcp import handle_tool_call

        with patch("robothor.crm.dal.list_tasks") as mock_list:
            mock_list.return_value = []
            await handle_tool_call("list_tasks", {"overdue": True, "unowned": True})
        assert mock_list.call_args.kwargs.get("overdue") is True
        assert mock_list.call_args.kwargs.get("unowned") is True

    def test_the_tool_schema_declares_both_filters(self) -> None:
        from robothor.api.mcp import get_tool_definitions

        schema = next(t for t in get_tool_definitions() if t["name"] == "list_tasks")
        properties = schema["inputSchema"]["properties"]
        assert properties["overdue"]["type"] == "boolean"
        assert properties["unowned"]["type"] == "boolean"
