"""Tests for the ToolRegistry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robothor.engine.models import AgentConfig
from robothor.engine.tools import IMPETUS_TOOLS, ToolRegistry, _execute_tool, get_registry


class TestToolRegistry:
    def test_registry_initializes(self):
        """Registry loads tool schemas from MCP definitions."""
        with patch("robothor.api.mcp.get_tool_definitions") as mock_defs:
            mock_defs.return_value = [
                {
                    "name": "list_tasks",
                    "description": "List tasks",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "create_task",
                    "description": "Create a task",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]
            registry = ToolRegistry()

        # Should have MCP tools + extra tools (exec, read_file, write_file, web_fetch, web_search)
        assert "list_tasks" in registry._schemas
        assert "create_task" in registry._schemas
        assert "exec" in registry._schemas
        assert "read_file" in registry._schemas
        assert "write_file" in registry._schemas

    def test_build_for_agent_with_allowed(self):
        """Filters tools by tools_allowed list."""
        with patch("robothor.api.mcp.get_tool_definitions") as mock_defs:
            mock_defs.return_value = [
                {
                    "name": "list_tasks",
                    "description": "t1",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "create_task",
                    "description": "t2",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "delete_task",
                    "description": "t3",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]
            registry = ToolRegistry()

        config = AgentConfig(
            id="test",
            name="test",
            tools_allowed=["list_tasks", "create_task"],
            tools_denied=[],
        )
        tools = registry.build_for_agent(config)
        names = [t["function"]["name"] for t in tools]
        assert "list_tasks" in names
        assert "create_task" in names
        assert "delete_task" not in names

    def test_build_for_agent_with_denied(self):
        """Filters out tools in tools_denied list."""
        with patch("robothor.api.mcp.get_tool_definitions") as mock_defs:
            mock_defs.return_value = [
                {
                    "name": "list_tasks",
                    "description": "t1",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "message",
                    "description": "t2",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]
            registry = ToolRegistry()

        config = AgentConfig(
            id="test",
            name="test",
            tools_allowed=[],  # all allowed
            tools_denied=["message"],
        )
        tools = registry.build_for_agent(config)
        names = [t["function"]["name"] for t in tools]
        assert "message" not in names
        assert "list_tasks" in names

    def test_get_tool_names(self):
        """Returns just the filtered names."""
        with patch("robothor.api.mcp.get_tool_definitions") as mock_defs:
            mock_defs.return_value = [
                {
                    "name": "list_tasks",
                    "description": "t1",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]
            registry = ToolRegistry()

        config = AgentConfig(id="test", name="test", tools_allowed=["list_tasks"])
        names = registry.get_tool_names(config)
        assert names == ["list_tasks"]

    def test_schema_format(self):
        """Tool schemas are in OpenAI function-calling format."""
        with patch("robothor.api.mcp.get_tool_definitions") as mock_defs:
            mock_defs.return_value = [
                {
                    "name": "list_tasks",
                    "description": "List tasks",
                    "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}},
                },
            ]
            registry = ToolRegistry()

        config = AgentConfig(id="test", name="test", tools_allowed=["list_tasks"])
        schemas = registry.build_for_agent(config)
        assert len(schemas) == 1
        schema = schemas[0]
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "list_tasks"
        assert schema["function"]["description"] == "List tasks"
        assert "parameters" in schema["function"]


class TestToolExecution:
    @pytest.mark.asyncio
    async def test_exec_tool(self):
        """Shell exec tool runs commands."""
        result = await _execute_tool("exec", {"command": "echo hello"})
        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]

    @pytest.mark.asyncio
    async def test_exec_tool_timeout(self):
        """Shell exec respects timeout."""
        result = await _execute_tool("exec", {"command": "sleep 60"})
        assert "error" in result
        assert "timed out" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_exec_tool_empty_command(self):
        """Empty command returns error."""
        result = await _execute_tool("exec", {"command": ""})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_read_file(self, tmp_path):
        """Read file tool reads file contents."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("file contents here")
        result = await _execute_tool(
            "read_file",
            {"path": str(test_file)},
            workspace=str(tmp_path),
        )
        assert result["content"] == "file contents here"

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path):
        """Read file returns error for missing file."""
        result = await _execute_tool(
            "read_file",
            {"path": str(tmp_path / "nonexistent.txt")},
            workspace=str(tmp_path),
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_write_file(self, tmp_path):
        """Write file tool creates files."""
        result = await _execute_tool(
            "write_file",
            {"path": str(tmp_path / "output.txt"), "content": "written"},
            workspace=str(tmp_path),
        )
        assert result["success"] is True
        assert (tmp_path / "output.txt").read_text() == "written"

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        """Unknown tool returns error."""
        result = await _execute_tool("nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    @pytest.mark.asyncio
    async def test_crm_tool_direct_dal(self):
        """CRM tools call DAL directly."""
        with patch("robothor.crm.dal.list_tasks") as mock_list:
            mock_list.return_value = [{"id": "t1", "title": "Test task"}]
            result = await _execute_tool(
                "list_tasks",
                {"status": "TODO"},
                agent_id="test-agent",
                tenant_id="test-tenant",
            )
        assert result["count"] == 1
        mock_list.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_task_injects_agent_id(self):
        """create_task auto-fills createdByAgent from agent context."""
        with patch("robothor.crm.dal.create_task") as mock_create:
            mock_create.return_value = "task-uuid-123"
            result = await _execute_tool(
                "create_task",
                {"title": "Test task"},
                agent_id="email-classifier",
                tenant_id="test-tenant",
            )
        assert result["id"] == "task-uuid-123"
        # Verify agent_id was passed as created_by_agent
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["created_by_agent"] == "email-classifier"


class TestObservabilityTools:
    @pytest.mark.asyncio
    async def test_list_agent_runs(self):
        """list_agent_runs returns summarized run data."""
        mock_runs = [
            {
                "id": "run-1",
                "agent_id": "vision-monitor",
                "status": "completed",
                "trigger_type": "cron",
                "model_used": "kimi-k2.5",
                "duration_ms": 25000,
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_cost_usd": None,
                "started_at": "2026-02-27 10:00:00",
                "completed_at": "2026-02-27 10:00:25",
                "error_message": None,
            },
        ]
        with patch("robothor.engine.tracking.list_runs", return_value=mock_runs):
            result = await _execute_tool(
                "list_agent_runs", {"agent_id": "vision-monitor", "limit": 5}
            )
        assert result["count"] == 1
        assert result["runs"][0]["agent_id"] == "vision-monitor"
        assert result["runs"][0]["status"] == "completed"
        assert result["runs"][0]["duration_ms"] == 25000

    @pytest.mark.asyncio
    async def test_list_agent_runs_empty(self):
        """list_agent_runs with no results."""
        with patch("robothor.engine.tracking.list_runs", return_value=[]):
            result = await _execute_tool("list_agent_runs", {})
        assert result["count"] == 0
        assert result["runs"] == []

    @pytest.mark.asyncio
    async def test_get_agent_run_with_steps(self):
        """get_agent_run returns run details and step audit trail."""
        mock_run = {
            "id": "run-1",
            "agent_id": "email-classifier",
            "status": "completed",
            "trigger_type": "cron",
            "trigger_detail": None,
            "model_used": "kimi-k2.5",
            "models_attempted": ["kimi-k2.5"],
            "duration_ms": 13000,
            "input_tokens": 800,
            "output_tokens": 300,
            "total_cost_usd": None,
            "started_at": "2026-02-27 10:00:00",
            "completed_at": "2026-02-27 10:00:13",
            "error_message": None,
            "delivery_status": "delivered",
        }
        mock_steps = [
            {
                "step_number": 1,
                "step_type": "tool_call",
                "tool_name": "read_file",
                "duration_ms": 50,
                "error_message": None,
            },
            {
                "step_number": 2,
                "step_type": "tool_call",
                "tool_name": "create_task",
                "duration_ms": 120,
                "error_message": None,
            },
        ]
        with (
            patch("robothor.engine.tracking.get_run", return_value=mock_run),
            patch("robothor.engine.tracking.list_steps", return_value=mock_steps),
        ):
            result = await _execute_tool("get_agent_run", {"run_id": "run-1"})
        assert result["run"]["agent_id"] == "email-classifier"
        assert result["step_count"] == 2
        assert result["steps"][0]["tool_name"] == "read_file"
        assert result["steps"][1]["tool_name"] == "create_task"

    @pytest.mark.asyncio
    async def test_get_agent_run_not_found(self):
        """get_agent_run returns error for unknown run ID."""
        with patch("robothor.engine.tracking.get_run", return_value=None):
            result = await _execute_tool("get_agent_run", {"run_id": "nonexistent"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_list_agent_schedules(self):
        """list_agent_schedules returns schedule data."""
        mock_schedules = [
            {
                "agent_id": "email-classifier",
                "enabled": True,
                "cron_expr": "0 6-22/2 * * *",
                "timezone": "America/New_York",
                "timeout_seconds": 480,
                "model_primary": "kimi-k2.5",
                "last_run_at": "2026-02-27 10:00:00",
                "last_status": "completed",
                "last_duration_ms": 13000,
                "next_run_at": "2026-02-27 12:00:00",
                "consecutive_errors": 0,
            },
        ]
        with patch("robothor.engine.tracking.list_schedules", return_value=mock_schedules):
            result = await _execute_tool("list_agent_schedules", {})
        assert result["count"] == 1
        assert result["schedules"][0]["agent_id"] == "email-classifier"
        assert result["schedules"][0]["cron_expr"] == "0 6-22/2 * * *"

    @pytest.mark.asyncio
    async def test_get_agent_stats(self):
        """get_agent_stats returns aggregated stats."""
        from decimal import Decimal

        mock_stats = {
            "total_runs": 12,
            "completed": 10,
            "failed": 1,
            "timeouts": 1,
            "avg_duration_ms": Decimal("25000.5"),
            "total_input_tokens": 12000,
            "total_output_tokens": 6000,
            "total_cost_usd": Decimal("0.045"),
        }
        with patch("robothor.engine.tracking.get_agent_stats", return_value=mock_stats):
            result = await _execute_tool("get_agent_stats", {"agent_id": "vision-monitor"})
        assert result["agent_id"] == "vision-monitor"
        assert result["total_runs"] == 12
        assert result["failed"] == 1
        assert result["avg_duration_ms"] == 25000  # rounded (banker's rounding)
        assert result["total_cost_usd"] == 0.045

    @pytest.mark.asyncio
    async def test_get_agent_stats_empty(self):
        """get_agent_stats handles empty stats."""
        mock_stats = {
            "total_runs": 0,
            "completed": 0,
            "failed": 0,
            "timeouts": 0,
            "avg_duration_ms": None,
            "total_input_tokens": None,
            "total_output_tokens": None,
            "total_cost_usd": None,
        }
        with patch("robothor.engine.tracking.get_agent_stats", return_value=mock_stats):
            result = await _execute_tool("get_agent_stats", {"agent_id": "nonexistent"})
        assert result["total_runs"] == 0
        assert result["avg_duration_ms"] is None
        assert result["total_cost_usd"] is None

    def test_observability_schemas_registered(self):
        """All 4 observability tool schemas are registered."""
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            registry = ToolRegistry()
        assert "list_agent_runs" in registry._schemas
        assert "get_agent_run" in registry._schemas
        assert "list_agent_schedules" in registry._schemas
        assert "get_agent_stats" in registry._schemas

    def test_observability_tools_in_agent_allowlist(self):
        """Observability tools are included when in tools_allowed."""
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            registry = ToolRegistry()
        config = AgentConfig(
            id="main",
            name="main",
            tools_allowed=["list_agent_runs", "get_agent_stats", "exec"],
        )
        names = registry.get_tool_names(config)
        assert "list_agent_runs" in names
        assert "get_agent_stats" in names
        assert "exec" in names


class TestImpetusTool:
    def test_impetus_tools_frozenset_has_all_13(self):
        """IMPETUS_TOOLS frozenset contains exactly 13 tools."""
        assert len(IMPETUS_TOOLS) == 13
        assert "search_patients" in IMPETUS_TOOLS
        assert "get_patient_details" in IMPETUS_TOOLS
        assert "get_patient_clinical_notes" in IMPETUS_TOOLS
        assert "get_patient_prescriptions" in IMPETUS_TOOLS
        assert "search_prescriptions" in IMPETUS_TOOLS
        assert "get_prescription_status" in IMPETUS_TOOLS
        assert "search_medications" in IMPETUS_TOOLS
        assert "search_pharmacies" in IMPETUS_TOOLS
        assert "get_appointments" in IMPETUS_TOOLS
        assert "list_actable_providers" in IMPETUS_TOOLS
        assert "create_prescription_draft" in IMPETUS_TOOLS
        assert "schedule_appointment" in IMPETUS_TOOLS
        assert "transmit_prescription" in IMPETUS_TOOLS

    def test_all_impetus_schemas_registered(self):
        """All 13 Impetus tool schemas are registered in ToolRegistry."""
        from robothor.api.mcp import get_tool_definitions

        real_defs = get_tool_definitions()
        with patch("robothor.api.mcp.get_tool_definitions", return_value=real_defs):
            registry = ToolRegistry()
        for tool_name in IMPETUS_TOOLS:
            assert tool_name in registry._schemas, f"{tool_name} not in registry"

    @pytest.mark.asyncio
    async def test_search_patients_routes_to_bridge(self):
        """search_patients routes through Bridge MCP passthrough."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"patients": [{"id": "p1", "name": "Smith"}]}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("robothor.engine.tools.httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _execute_tool("search_patients", {"query": "Smith"})

        assert result == {"patients": [{"id": "p1", "name": "Smith"}]}
        mock_client.post.assert_called_once_with(
            "http://127.0.0.1:9100/api/impetus/tools/call",
            json={"name": "search_patients", "arguments": {"query": "Smith"}},
        )

    @pytest.mark.asyncio
    async def test_transmit_prescription_routes_to_bridge(self):
        """transmit_prescription (write tool) routes through Bridge."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "pending_confirmation", "confirmationId": "c1"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("robothor.engine.tools.httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _execute_tool("transmit_prescription", {"prescriptionId": "rx-1"})

        assert result["confirmationId"] == "c1"
        mock_client.post.assert_called_once_with(
            "http://127.0.0.1:9100/api/impetus/tools/call",
            json={"name": "transmit_prescription", "arguments": {"prescriptionId": "rx-1"}},
        )

    @pytest.mark.asyncio
    async def test_bridge_error_returns_error_dict(self):
        """Bridge HTTP errors are caught and returned as error dicts."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.HTTPStatusError(
            "502 Bad Gateway",
            request=MagicMock(),
            response=MagicMock(status_code=502),
        )

        with patch("robothor.engine.tools.httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            # _execute_tool raises, but ToolRegistry.execute catches it
            with pytest.raises(httpx.HTTPStatusError):
                await _execute_tool("search_patients", {"query": "test"})

    def test_impetus_tools_in_main_agent_allowlist(self):
        """Impetus tools are available to the main agent when in tools_allowed."""
        from robothor.api.mcp import get_tool_definitions

        real_defs = get_tool_definitions()
        with patch("robothor.api.mcp.get_tool_definitions", return_value=real_defs):
            registry = ToolRegistry()

        config = AgentConfig(
            id="main",
            name="main",
            tools_allowed=list(IMPETUS_TOOLS) + ["exec"],
        )
        names = registry.get_tool_names(config)
        for tool_name in IMPETUS_TOOLS:
            assert tool_name in names, f"{tool_name} not accessible to main agent"


class TestMergeAndAliasTools:
    """Tests for merge_people, merge_contacts, merge_companies, list_my_tasks schemas."""

    def _make_registry(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            return ToolRegistry()

    def test_list_my_tasks_schema_registered(self):
        r = self._make_registry()
        assert "list_my_tasks" in r._schemas
        params = r._schemas["list_my_tasks"]["function"]["parameters"]
        assert "status" in params["properties"]
        assert "limit" in params["properties"]

    def test_merge_people_schema_registered(self):
        r = self._make_registry()
        assert "merge_people" in r._schemas
        params = r._schemas["merge_people"]["function"]["parameters"]
        assert "keeperId" in params["properties"]
        assert "loserId" in params["properties"]
        assert params["required"] == ["keeperId", "loserId"]

    def test_merge_contacts_schema_registered(self):
        r = self._make_registry()
        assert "merge_contacts" in r._schemas
        params = r._schemas["merge_contacts"]["function"]["parameters"]
        assert "keeperId" in params["properties"]
        assert "loserId" in params["properties"]

    def test_merge_companies_schema_registered(self):
        r = self._make_registry()
        assert "merge_companies" in r._schemas
        params = r._schemas["merge_companies"]["function"]["parameters"]
        assert "keeperId" in params["properties"]
        assert "loserId" in params["properties"]

    def test_list_my_tasks_in_agent_allowlist(self):
        """Agent with list_my_tasks in tools_allowed gets the schema."""
        r = self._make_registry()
        config = AgentConfig(
            id="test",
            name="test",
            tools_allowed=["list_my_tasks", "exec"],
        )
        tools = r.build_for_agent(config)
        names = [t["function"]["name"] for t in tools]
        assert "list_my_tasks" in names
        assert "exec" in names
        assert len(names) == 2

    def test_merge_contacts_in_agent_allowlist(self):
        """Agent with merge_contacts in tools_allowed gets the schema."""
        r = self._make_registry()
        config = AgentConfig(
            id="test",
            name="test",
            tools_allowed=["merge_contacts", "merge_companies"],
        )
        tools = r.build_for_agent(config)
        names = [t["function"]["name"] for t in tools]
        assert "merge_contacts" in names
        assert "merge_companies" in names

    @pytest.mark.asyncio
    async def test_list_my_tasks_executor(self):
        """list_my_tasks calls list_agent_tasks with the current agent ID."""
        with patch("robothor.crm.dal.list_agent_tasks") as mock_lat:
            mock_lat.return_value = [{"id": "t1", "title": "Test"}]
            result = await _execute_tool(
                "list_my_tasks",
                {"status": "TODO", "limit": 10},
                agent_id="email-classifier",
                tenant_id="test",
            )
        assert result["count"] == 1
        mock_lat.assert_called_once_with(
            agent_id="email-classifier",
            include_unassigned=False,
            status="TODO",
            limit=10,
            tenant_id="test",
        )

    @pytest.mark.asyncio
    async def test_merge_people_executor(self):
        """merge_people calls dal.merge_people."""
        with patch("robothor.crm.dal.merge_people") as mock_merge:
            mock_merge.return_value = {"id": "keeper-1", "first_name": "Philip"}
            result = await _execute_tool(
                "merge_people",
                {"keeperId": "keeper-1", "loserId": "loser-1"},
                tenant_id="test",
            )
        assert result["success"] is True
        mock_merge.assert_called_once_with(
            keeper_id="keeper-1",
            loser_id="loser-1",
            tenant_id="test",
        )

    @pytest.mark.asyncio
    async def test_merge_contacts_executor(self):
        """merge_contacts is an alias for merge_people."""
        with patch("robothor.crm.dal.merge_people") as mock_merge:
            mock_merge.return_value = {"id": "keeper-1"}
            result = await _execute_tool(
                "merge_contacts",
                {"keeperId": "keeper-1", "loserId": "loser-1"},
                tenant_id="test",
            )
        assert result["success"] is True
        mock_merge.assert_called_once()

    @pytest.mark.asyncio
    async def test_merge_companies_executor(self):
        """merge_companies calls dal.merge_companies."""
        with patch("robothor.crm.dal.merge_companies") as mock_merge:
            mock_merge.return_value = {"id": "keeper-co"}
            result = await _execute_tool(
                "merge_companies",
                {"keeperId": "keeper-co", "loserId": "loser-co"},
                tenant_id="test",
            )
        assert result["success"] is True
        mock_merge.assert_called_once_with(
            keeper_id="keeper-co",
            loser_id="loser-co",
            tenant_id="test",
        )

    @pytest.mark.asyncio
    async def test_merge_people_not_found(self):
        """merge_people returns error when IDs not found."""
        with patch("robothor.crm.dal.merge_people") as mock_merge:
            mock_merge.return_value = None
            result = await _execute_tool(
                "merge_people",
                {"keeperId": "bad", "loserId": "bad"},
                tenant_id="test",
            )
        assert "error" in result


class TestRegistrySingleton:
    def test_singleton(self):
        """get_registry returns the same instance."""
        import robothor.engine.tools as tools_mod

        tools_mod._registry = None  # Reset

        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            r1 = get_registry()
            r2 = get_registry()
        assert r1 is r2
        tools_mod._registry = None  # Cleanup
