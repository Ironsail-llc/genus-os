"""Tool-boundary id validation for every id-bearing CRM handler.

LLMs hallucinate placeholder ids ("task_jkl012", "cnv-00456"). These used to
reach SQL verbatim and crash with psycopg2 InvalidTextRepresentation, which
dispatch reported as a raw tool crash. The handlers now validate the id shape
at the boundary and return a friendly structured error naming the expected
format — before any DB call happens.

The guard was written for `get_task` and applied to `get_task` only. Its two
siblings kept crashing for another year:

    2026-09-13T02:42:01  Tool get_person raised unhandled exception
        psycopg2.errors.InvalidTextRepresentation:
        invalid input syntax for type uuid: "bob.quill@example.com"
    2026-09-13T05:10:50  Tool list_tasks raised unhandled exception
        invalid input syntax for type uuid: "85105"

So the validation now lives in the `_handler` decorator every tool in the
module already passes through — "guard the path every caller crosses, not the
one the current caller uses" — and these tests sweep the whole module rather
than naming the two handlers that happened to crash this week.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

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


# The two handlers that crashed in production on 2026-09-13, named
# explicitly so the regression is readable without decoding the sweep below.
class TestTheHandlersThatCrashed:
    async def test_get_person_refuses_an_email_address_as_an_id(self):
        with patch("robothor.crm.dal.get_connection") as connect:
            result = await HANDLERS["get_person"]({"id": "bob.quill@example.com"}, CTX)
        connect.assert_not_called()
        assert "bob.quill@example.com" in result["error"]
        assert "not a valid id" in result["error"]

    async def test_list_tasks_refuses_a_numeric_person_id(self):
        with patch("robothor.crm.dal.get_connection") as connect:
            result = await HANDLERS["list_tasks"]({"personId": "85105"}, CTX)
        connect.assert_not_called()
        assert "85105" in result["error"]
        assert "not a valid id" in result["error"]

    async def test_get_person_still_reaches_the_dal_for_a_real_id(self):
        person_id = str(uuid.uuid4())
        person = {"id": person_id, "first_name": "Alice"}
        with patch("robothor.crm.dal.get_person", return_value=person) as dal_get_person:
            result = await HANDLERS["get_person"]({"id": person_id}, CTX)
        dal_get_person.assert_called_once()
        assert result == person

    async def test_list_tasks_without_a_person_filter_still_reaches_the_dal(self):
        """The personId filter is optional — absent is not invalid."""
        with patch("robothor.crm.dal.list_tasks", return_value=[]) as dal_list_tasks:
            result = await HANDLERS["list_tasks"]({}, CTX)
        dal_list_tasks.assert_called_once()
        assert result == {"tasks": [], "count": 0}

    async def test_list_tasks_with_a_real_person_filter_reaches_the_dal(self):
        person_id = str(uuid.uuid4())
        with patch("robothor.crm.dal.list_tasks", return_value=[]) as dal_list_tasks:
            await HANDLERS["list_tasks"]({"personId": person_id}, CTX)
        assert dal_list_tasks.call_args.kwargs["person_id"] == person_id


# Every id-bearing argument in the module, with an id an LLM might invent.
# A handler added later inherits the guard from the decorator; a handler
# added later with a NEW id argument name has to be added here.
_HALLUCINATED: list[tuple[str, dict]] = [
    ("get_person", {"id": "bob.quill@example.com"}),
    ("update_person", {"id": "bob.quill@example.com", "firstName": "Bob"}),
    ("delete_person", {"id": "person_001"}),
    ("get_company", {"id": "acme-corp"}),
    ("update_company", {"id": "acme-corp", "name": "Acme"}),
    ("delete_company", {"id": "acme-corp"}),
    ("create_note", {"title": "Call notes", "personId": "85105"}),
    ("get_note", {"id": "note_abc123"}),
    ("list_notes", {"companyId": "acme-corp"}),
    ("update_note", {"id": "note_abc123", "title": "x"}),
    ("delete_note", {"id": "note_abc123"}),
    ("create_task", {"title": "Follow up", "personId": "85105"}),
    ("create_task", {"title": "Follow up", "parentTaskId": "task_jkl012"}),
    ("get_task", {"id": "task_jkl012"}),
    ("list_tasks", {"personId": "85105"}),
    ("update_task", {"id": "task_jkl012", "status": "done"}),
    ("delete_task", {"id": "task_jkl012"}),
    ("resolve_task", {"id": "task_jkl012", "resolution": "done"}),
    ("approve_task", {"id": "task_jkl012"}),
    ("reject_task", {"id": "task_jkl012", "reason": "no"}),
    ("send_notification", {"toAgent": "main", "subject": "hi", "taskId": "task_jkl012"}),
    ("ack_notification", {"notificationId": "notif_1"}),
    ("merge_people", {"keeperId": "person_001", "loserId": "person_002"}),
    ("merge_contacts", {"keeperId": "person_001", "loserId": "person_002"}),
    ("merge_companies", {"keeperId": "acme-corp", "loserId": "acme-inc"}),
    ("get_contact_360", {"id": "bob.quill@example.com"}),
    ("list_contact_messages", {"id": "bob.quill@example.com"}),
    ("get_conversation", {"conversationId": "cnv-00456"}),
    ("list_messages", {"conversationId": "cnv-00456"}),
    ("create_message", {"conversationId": "cnv-00456", "content": "hi"}),
    ("toggle_conversation_status", {"conversationId": "cnv-00456"}),
]


class TestEveryIdBearingHandler:
    """One crash class, one guard, applied everywhere it can happen."""

    @pytest.mark.parametrize(("tool", "args"), _HALLUCINATED)
    async def test_a_hallucinated_id_is_refused_before_the_database(self, tool, args):
        connection = MagicMock()
        with patch("robothor.crm.dal.get_connection", connection):
            result = await HANDLERS[tool](args, CTX)

        connection.assert_not_called()
        assert isinstance(result, dict), f"{tool} did not return a dict"
        assert "error" in result, f"{tool} accepted a hallucinated id: {result}"
        assert "not a valid id" in result["error"], result["error"]

    @pytest.mark.parametrize(("tool", "args"), _HALLUCINATED)
    async def test_the_error_quotes_the_offending_value(self, tool, args):
        """The model has to be able to tell which argument it got wrong."""
        offenders = [
            v
            for v in args.values()
            if isinstance(v, str) and ("_" in v or "-" in v or "@" in v or v.isdigit())
        ]
        with patch("robothor.crm.dal.get_connection", MagicMock()):
            result = await HANDLERS[tool](args, CTX)
        assert any(o in result["error"] for o in offenders), result["error"]
