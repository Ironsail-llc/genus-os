"""Tool-boundary id validation for get_task / list_messages.

LLMs hallucinate placeholder ids ("task_jkl012", "cnv-00456"). These used to
reach SQL verbatim and crash with psycopg2 InvalidTextRepresentation, which
dispatch reported as a raw tool crash. The handlers now validate the id shape
at the boundary and return a friendly structured error naming the expected
format — before any DB call happens.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.crm import HANDLERS

CTX = ToolContext(agent_id="test", tenant_id="test-tenant")


class TestGetTaskIdValidation:
    async def test_hallucinated_id_returns_friendly_error_without_db_call(self):
        with patch("robothor.crm.dal.get_task") as dal_get_task:
            result = await HANDLERS["get_task"]({"id": "task_jkl012"}, CTX)
        dal_get_task.assert_not_called()
        assert "error" in result
        assert "task_jkl012" in result["error"]
        assert "UUID" in result["error"]

    async def test_missing_id_returns_friendly_error(self):
        result = await HANDLERS["get_task"]({}, CTX)
        assert "error" in result
        assert "UUID" in result["error"]

    async def test_valid_uuid_reaches_dal(self):
        task_id = str(uuid.uuid4())
        task = {"id": task_id, "title": "Review the report"}
        with patch("robothor.crm.dal.get_task", return_value=task) as dal_get_task:
            result = await HANDLERS["get_task"]({"id": task_id}, CTX)
        dal_get_task.assert_called_once()
        assert result == task


class TestListMessagesIdValidation:
    async def test_hallucinated_conversation_id_returns_friendly_error(self):
        with patch("robothor.crm.dal.list_messages") as dal_list:
            result = await HANDLERS["list_messages"]({"conversationId": "cnv-00456"}, CTX)
        dal_list.assert_not_called()
        assert "error" in result
        assert "cnv-00456" in result["error"]
        assert "integer" in result["error"]

    async def test_missing_conversation_id_returns_friendly_error(self):
        result = await HANDLERS["list_messages"]({}, CTX)
        assert "error" in result
        assert "integer" in result["error"]

    async def test_integer_string_is_accepted(self):
        with patch("robothor.crm.dal.list_messages", return_value=[]) as dal_list:
            result = await HANDLERS["list_messages"]({"conversationId": "42"}, CTX)
        dal_list.assert_called_once()
        assert result == {"payload": []}
        assert dal_list.call_args.args[0] == 42

    async def test_integer_is_accepted(self):
        with patch("robothor.crm.dal.list_messages", return_value=[]) as dal_list:
            result = await HANDLERS["list_messages"]({"conversationId": 7}, CTX)
        dal_list.assert_called_once()
        assert result == {"payload": []}
