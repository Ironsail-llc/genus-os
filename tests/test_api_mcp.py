"""Tests for robothor.api.mcp — MCP tool definitions and handlers."""

import pytest

from robothor.api.mcp import get_tool_definitions, handle_tool_call

# ─── Tool Definitions ────────────────────────────────────────────────


class TestToolDefinitions:
    def test_returns_list(self):
        tools = get_tool_definitions()
        assert isinstance(tools, list)

    def test_has_53_tools(self):
        """53 MCP tools: CRM/memory/vision/tenancy/notifications/vault/resolution."""
        tools = get_tool_definitions()
        assert len(tools) == 53

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
        assert set(mode_prop["enum"]) == {"disarmed", "basic", "armed", "disabled"}


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

    @pytest.mark.asyncio
    async def test_create_task_propagates_validation_error(self, monkeypatch):
        """The MCP create_task handler must propagate `create_task`'s error
        dict (Phase-1 contract) instead of wrapping it as a success response.

        Latent today — the MCP handler doesn't pass autonomy_budget — but
        we pin the guard so the next contributor who wires the param can't
        regress into the bridge POST bug (truthy error dict slipping past
        `if task_id:` and surfacing as `{"id": {"error": ...}, "title": ...}`).
        Mirrors the regression test for the bridge endpoint.
        """
        err = {"error": "reversible_cap_usd must be non-negative (got -1)"}

        def fake_create_task(**kwargs):
            return err

        monkeypatch.setattr("robothor.crm.dal.create_task", fake_create_task)

        result = await handle_tool_call("create_task", {"title": "Bad budget"})
        assert result == err
        assert "id" not in result


class TestDoNotContactSurface:
    """The opt-out flag has to be reachable by an agent, not just by SQL.

    ``get_tool_definitions`` is the single schema source for BOTH surfaces:
    the MCP server advertises it directly, and the engine's ToolRegistry
    converts the same definitions into function schemas
    (``robothor/engine/tools/registry.py::_register_all``). A field missing
    here is a field no agent can ever set.
    """

    def test_update_person_schema_exposes_the_flag(self):
        schema = next(t for t in get_tool_definitions() if t["name"] == "update_person")
        prop = schema["inputSchema"]["properties"]["doNotContact"]
        assert prop["type"] == "boolean"

    @pytest.mark.asyncio
    async def test_update_person_maps_the_flag_to_the_dal(self, monkeypatch):
        captured: dict = {}

        def _fake_update_person(pid, **kwargs):
            captured["pid"] = pid
            captured.update(kwargs)
            return True

        import robothor.crm.dal as dal

        monkeypatch.setattr(dal, "update_person", _fake_update_person)
        # A real UUID: ids are validated at this boundary now, so a
        # placeholder would be refused before the DAL (see TestIdValidation).
        person_id = "11111111-1111-4111-8111-111111111111"
        result = await handle_tool_call("update_person", {"id": person_id, "doNotContact": True})

        assert result == {"success": True, "id": person_id}
        assert captured["do_not_contact"] is True


class TestIdValidation:
    """The MCP surface is the *other* dispatcher for the same CRM tools.

    The engine handlers grew an id-shape guard after `get_person` and
    `list_tasks` crashed with psycopg2 InvalidTextRepresentation on
    2026-09-13. This module declares and dispatches the same tools straight
    to the DAL, and it is the surface the operator's own Claude sessions use
    (`mcp__robothor-memory__get_person`), so the identical crash lives here
    until both dispatchers call the same validator.
    """

    @pytest.mark.asyncio
    async def test_get_person_refuses_an_email_address_as_an_id(self, monkeypatch):
        import robothor.crm.dal as dal

        def _must_not_run(*a, **k):  # pragma: no cover - the point is it never runs
            raise AssertionError("the DAL was reached with a non-uuid id")

        monkeypatch.setattr(dal, "get_person", _must_not_run)
        result = await handle_tool_call("get_person", {"id": "bob.quill@example.com"})

        assert "bob.quill@example.com" in result["error"]
        assert "not a valid id" in result["error"]

    @pytest.mark.asyncio
    async def test_list_tasks_refuses_a_numeric_person_id(self, monkeypatch):
        import robothor.crm.dal as dal

        def _must_not_run(*a, **k):  # pragma: no cover
            raise AssertionError("the DAL was reached with a non-uuid personId")

        monkeypatch.setattr(dal, "list_tasks", _must_not_run)
        result = await handle_tool_call("list_tasks", {"personId": "85105"})

        assert "85105" in result["error"]
        assert "not a valid id" in result["error"]

    @pytest.mark.asyncio
    async def test_a_real_id_still_reaches_the_dal(self, monkeypatch):
        import robothor.crm.dal as dal

        person_id = "11111111-1111-4111-8111-111111111111"
        monkeypatch.setattr(dal, "get_person", lambda pid: {"id": pid})
        assert await handle_tool_call("get_person", {"id": person_id}) == {"id": person_id}

    @pytest.mark.asyncio
    async def test_an_unfiltered_list_still_reaches_the_dal(self, monkeypatch):
        """The optional personId filter being absent is not an invalid id."""
        import robothor.crm.dal as dal

        monkeypatch.setattr(dal, "list_tasks", lambda **kwargs: [])
        assert await handle_tool_call("list_tasks", {}) == {"tasks": [], "count": 0}

    @pytest.mark.asyncio
    async def test_a_non_crm_tool_is_untouched(self, monkeypatch):
        """The guard keys off CRM id argument names, not every tool call."""
        result = await handle_tool_call("nonexistent_tool", {"id": "not-a-uuid"})
        assert "not a valid id" not in result.get("error", "")
