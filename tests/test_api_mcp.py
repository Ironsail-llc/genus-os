"""Tests for robothor.api.mcp — MCP tool definitions and handlers."""

import pytest

from robothor.api.mcp import get_tool_definitions, handle_tool_call

# ─── Tool Definitions ────────────────────────────────────────────────


class TestToolDefinitions:
    def test_returns_list(self):
        tools = get_tool_definitions()
        assert isinstance(tools, list)

    def test_has_61_tools(self):
        """61 MCP tools: CRM/memory/vision/tenancy/notifications/vault/impetus."""
        tools = get_tool_definitions()
        assert len(tools) == 61

    def test_tool_structure(self):
        tools = get_tool_definitions()
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert isinstance(tool["name"], str)
            assert isinstance(tool["description"], str)
            assert isinstance(tool["inputSchema"], dict)

    def test_input_schema_has_type(self):
        tools = get_tool_definitions()
        for tool in tools:
            assert tool["inputSchema"]["type"] == "object"
            assert "properties" in tool["inputSchema"]

    def test_tool_names_unique(self):
        tools = get_tool_definitions()
        names = [t["name"] for t in tools]
        assert len(names) == len(set(names))

    def test_memory_tools_present(self):
        names = {t["name"] for t in get_tool_definitions()}
        assert "search_memory" in names
        assert "store_memory" in names
        assert "get_stats" in names
        assert "get_entity" in names

    def test_vision_tools_present(self):
        names = {t["name"] for t in get_tool_definitions()}
        assert "look" in names
        assert "who_is_here" in names
        assert "enroll_face" in names
        assert "set_vision_mode" in names

    def test_memory_block_tools_present(self):
        names = {t["name"] for t in get_tool_definitions()}
        assert "memory_block_read" in names
        assert "memory_block_write" in names
        assert "memory_block_list" in names

    def test_crm_people_tools_present(self):
        names = {t["name"] for t in get_tool_definitions()}
        for op in ["create_person", "get_person", "update_person", "list_people", "delete_person"]:
            assert op in names

    def test_crm_company_tools_present(self):
        names = {t["name"] for t in get_tool_definitions()}
        for op in [
            "create_company",
            "get_company",
            "update_company",
            "list_companies",
            "delete_company",
        ]:
            assert op in names

    def test_crm_note_tools_present(self):
        names = {t["name"] for t in get_tool_definitions()}
        for op in ["create_note", "get_note", "list_notes", "update_note", "delete_note"]:
            assert op in names

    def test_crm_task_tools_present(self):
        names = {t["name"] for t in get_tool_definitions()}
        for op in ["create_task", "get_task", "list_tasks", "update_task", "delete_task"]:
            assert op in names

    def test_crm_metadata_tools_present(self):
        names = {t["name"] for t in get_tool_definitions()}
        assert "get_metadata_objects" in names
        assert "get_object_metadata" in names
        assert "search_records" in names

    def test_crm_conversation_tools_present(self):
        names = {t["name"] for t in get_tool_definitions()}
        assert "list_conversations" in names
        assert "get_conversation" in names
        assert "list_messages" in names
        assert "create_message" in names
        assert "toggle_conversation_status" in names

    def test_log_interaction_present(self):
        names = {t["name"] for t in get_tool_definitions()}
        assert "log_interaction" in names

    def test_required_fields_on_search_memory(self):
        tool = next(t for t in get_tool_definitions() if t["name"] == "search_memory")
        assert "required" in tool["inputSchema"]
        assert "query" in tool["inputSchema"]["required"]

    def test_required_fields_on_create_person(self):
        tool = next(t for t in get_tool_definitions() if t["name"] == "create_person")
        assert "firstName" in tool["inputSchema"]["required"]

    def test_vision_mode_has_enum(self):
        tool = next(t for t in get_tool_definitions() if t["name"] == "set_vision_mode")
        mode_prop = tool["inputSchema"]["properties"]["mode"]
        assert "enum" in mode_prop
        assert set(mode_prop["enum"]) == {"disarmed", "basic", "armed"}


# ─── Tool Handler ────────────────────────────────────────────────────


class TestHandleToolCall:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        result = await handle_tool_call("nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    @pytest.mark.asyncio
    async def test_set_vision_mode_invalid(self):
        result = await handle_tool_call("set_vision_mode", {"mode": "invalid"})
        assert "error" in result
        assert "Invalid mode" in result["error"]

    @pytest.mark.asyncio
    async def test_enroll_face_no_name(self):
        result = await handle_tool_call("enroll_face", {})
        assert "error" in result
        assert "required" in result["error"].lower() or "Name" in result["error"]
