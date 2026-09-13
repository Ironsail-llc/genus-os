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

import ast
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from robothor.api import mcp
from robothor.crm import tool_ids
from robothor.engine.tools import dispatch as _dispatch  # noqa: F401 — registry import order
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import crm as crm_handlers
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


class TestIdsPostgresWouldRefuse:
    """The guard's whole purpose is that no malformed id reaches SQL.

    So "parses in Python" is the wrong bar: `uuid.UUID` accepts the RFC 4122
    URN form, which Postgres's uuid input does not, and the value would pass
    the guard and then crash exactly as before. Upper-case, {braced} and
    32-hex-no-dash all pass both and must keep passing.
    """

    UUID = "11111111-1111-4111-8111-111111111111"

    async def test_a_urn_uuid_is_refused(self):
        with patch("robothor.crm.dal.get_connection") as connection:
            result = await HANDLERS["get_person"]({"id": f"urn:uuid:{self.UUID}"}, CTX)
        connection.assert_not_called()
        assert "not a valid id" in result["error"]

    @pytest.mark.parametrize(
        "value",
        [
            UUID,
            UUID.upper(),
            "{" + UUID + "}",
            UUID.replace("-", ""),
        ],
    )
    async def test_forms_postgres_accepts_still_reach_the_dal(self, value):
        with patch("robothor.crm.dal.get_person", return_value={"id": value}) as dal_get:
            await HANDLERS["get_person"]({"id": value}, CTX)
        dal_get.assert_called_once()

    async def test_a_padded_id_is_stripped_rather_than_refused(self):
        """A model copying an id out of its own last tool result brings the
        whitespace with it. Refusing that reads as "this UUID is not a UUID"
        and invites the same value again; Postgres would reject it, so the
        boundary normalises instead of bouncing it."""
        with patch("robothor.crm.dal.get_person", return_value={"id": self.UUID}) as dal_get:
            result = await HANDLERS["get_person"]({"id": f"  {self.UUID}\n"}, CTX)
        assert "error" not in result
        assert dal_get.call_args.args[0] == self.UUID


class TestTheErrorPointsAtTheRouteThatWorks:
    async def test_contact_360_names_the_identifier_argument(self):
        """get_contact_360 resolves an email — telling the model to go and
        find an id instead, when an email is exactly what that tool takes,
        is advice that walks past the answer."""
        with patch("robothor.crm.dal.get_contact_360") as dal_get:
            result = await HANDLERS["get_contact_360"]({"id": "bob.quill@example.com"}, CTX)
        dal_get.assert_not_called()
        assert "identifier" in result["error"], result["error"]

    async def test_the_identifier_route_still_works(self):
        with patch("robothor.crm.dal.resolve_contact", return_value={"person_id": None}) as resolve:
            result = await HANDLERS["get_contact_360"](
                {"identifier": "bob.quill@example.com", "channel": "email"}, CTX
            )
        resolve.assert_called_once()
        assert "not a valid id" not in result.get("error", "")


class TestTheClassificationCannotDrift:
    """`get_task` grew this guard and its siblings did not — because coverage
    lived in one author's head. A hand-written list of cases repeats that: a
    handler added later with a new id argument name is silently unguarded and
    no test can fail. So the argument names are read out of the source.
    """

    #: An id name in either spelling, singular or plural. Matching only
    #: "…Id" left `fooIds`, `foo_id` and `foo_ids` invisible: a new argument
    #: in any of those shapes could sit beside a classified `fooId` and this
    #: gate would report nothing, which is the drift it exists to catch.
    _ID_SUFFIXES = ("Id", "Ids", "_id", "_ids")

    @classmethod
    def _id_literals(cls, path: Path) -> set[str]:
        """Every id-shaped string constant in a module."""
        tree = ast.parse(path.read_text())
        return {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (node.value == "id" or node.value.endswith(cls._ID_SUFFIXES))
        }

    @property
    def _classified(self) -> frozenset[str]:
        return tool_ids.UUID_ID_ARGS | tool_ids.INT_ID_ARGS | tool_ids.NOT_AN_ID

    def test_every_id_name_in_the_handlers_is_classified(self):
        module = Path(crm_handlers.__file__)
        unclassified = self._id_literals(module) - self._classified
        assert not unclassified, (
            f"unclassified id arguments in {module.name}: {sorted(unclassified)} — "
            "add each to UUID_ID_ARGS, INT_ID_ARGS, or NOT_AN_ID (with a reason) "
            "in robothor/crm/tool_ids.py"
        )

    def test_every_id_name_in_the_mcp_dispatcher_is_classified(self):
        """The second dispatcher for the same tools; same vocabulary, same rule."""
        module = Path(mcp.__file__)
        unclassified = self._id_literals(module) - self._classified
        assert not unclassified, (
            f"unclassified id arguments in {module.name}: {sorted(unclassified)}"
        )

    def test_no_classified_name_is_dead(self):
        """A name nobody reads is evidence the list was written from memory."""
        live = self._id_literals(Path(crm_handlers.__file__)) | self._id_literals(
            Path(mcp.__file__)
        )
        dead = (tool_ids.UUID_ID_ARGS | tool_ids.INT_ID_ARGS) - live
        assert not dead, f"classified but never read: {sorted(dead)}"

    def test_the_guarded_tool_set_matches_the_handler_registry(self):
        """CRM_TOOLS tells the MCP dispatcher where this vocabulary applies.
        A handler added without adding it there is an unguarded MCP path."""
        assert frozenset(HANDLERS) == tool_ids.CRM_TOOLS
