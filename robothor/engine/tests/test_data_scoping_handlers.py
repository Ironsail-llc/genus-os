"""Tests that the data-scoping tool handlers actually wire mode + identity
into a DataScope and pass it to the DAL layer (Task 5, Unified Identity
Context).

Exercised through ``dispatch._execute_tool`` end-to-end (permission check +
ToolContext construction + handler dispatch) rather than calling handler
functions directly, so the whole ``identity=`` plumbing added in this task is
covered along with the handler wiring itself. The underlying DataScope
math (``scope_for``/``scope_for_query``/``observe_scope``/``rows_dropped_by_*``)
is unit-tested in ``robothor/identity/tests/test_scope.py``; these tests only
pin that each handler reads ``ROBOTHOR_DATA_SCOPING`` and ``ctx.identity`` and
forwards the right thing to its DAL call.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from robothor.engine.tools.dispatch import _execute_tool
from robothor.identity import IdentityContext

RESTRICTED_IDENTITY = IdentityContext(
    tenant_id="tenant-a",
    channel="webchat",
    identifier="user-1",
    verified=True,
    role="member",
    person_id="person-1",
)


def _mode_env(mode: str):
    return patch.dict(os.environ, {"ROBOTHOR_DATA_SCOPING": mode}, clear=False)


async def _call(name, args, *, identity=None, user_role="owner", user_id="user-1"):
    return await _execute_tool(
        name,
        args,
        tenant_id="tenant-a",
        user_id=user_id,
        user_role=user_role,
        identity=identity,
    )


class TestSearchMemoryScoping:
    @pytest.mark.asyncio
    async def test_off_mode_scope_none(self):
        with _mode_env("off"), patch("robothor.memory.facts.search_facts") as mock_search:
            mock_search.return_value = []
            await _call(
                "search_memory", {"query": "x"}, identity=RESTRICTED_IDENTITY, user_role="member"
            )
        assert mock_search.call_args.kwargs.get("scope") is None

    @pytest.mark.asyncio
    async def test_enforce_mode_restricted_identity_passes_scope(self):
        with _mode_env("enforce"), patch("robothor.memory.facts.search_facts") as mock_search:
            mock_search.return_value = []
            await _call(
                "search_memory", {"query": "x"}, identity=RESTRICTED_IDENTITY, user_role="member"
            )
        scope = mock_search.call_args.kwargs.get("scope")
        assert scope is not None
        assert scope.restricted is True
        assert scope.person_id == "person-1"

    @pytest.mark.asyncio
    async def test_enforce_mode_no_identity_passes_none(self):
        """System/cron callers (identity=None) stay unrestricted under enforce."""
        with _mode_env("enforce"), patch("robothor.memory.facts.search_facts") as mock_search:
            mock_search.return_value = []
            await _call("search_memory", {"query": "x"}, identity=None, user_role="service")
        assert mock_search.call_args.kwargs.get("scope") is None

    @pytest.mark.asyncio
    async def test_observe_mode_query_unrestricted_but_logs_drop(self, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="robothor.identity.scope")
        with (
            _mode_env("observe"),
            patch("robothor.memory.facts.search_facts") as mock_search,
        ):
            mock_search.return_value = [
                {"id": 1, "person_id": "person-1", "fact_text": "own"},
                {"id": 2, "person_id": "person-9", "fact_text": "not mine"},
            ]
            await _call(
                "search_memory", {"query": "x"}, identity=RESTRICTED_IDENTITY, user_role="member"
            )

        # Observe must not touch the query.
        assert mock_search.call_args.kwargs.get("scope") is None
        # ...but must log what it would have dropped.
        msgs = [r.getMessage() for r in caplog.records]
        assert any("data_scoping" in m and "would_drop=1" in m for m in msgs)


class TestListNotesScoping:
    @pytest.mark.asyncio
    async def test_off_mode_scope_none(self):
        with _mode_env("off"), patch("robothor.crm.dal.list_notes") as mock_list:
            mock_list.return_value = []
            await _call("list_notes", {}, identity=RESTRICTED_IDENTITY, user_role="member")
        assert mock_list.call_args.kwargs.get("scope") is None

    @pytest.mark.asyncio
    async def test_enforce_mode_restricted_identity_passes_scope(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.list_notes") as mock_list:
            mock_list.return_value = []
            await _call("list_notes", {}, identity=RESTRICTED_IDENTITY, user_role="member")
        scope = mock_list.call_args.kwargs.get("scope")
        assert scope is not None
        assert scope.restricted is True

    @pytest.mark.asyncio
    async def test_enforce_mode_owner_identity_passes_none(self):
        owner_identity = IdentityContext(
            tenant_id="tenant-a",
            channel="webchat",
            identifier="owner-1",
            verified=True,
            role="owner",
            person_id="owner-person",
        )
        with _mode_env("enforce"), patch("robothor.crm.dal.list_notes") as mock_list:
            mock_list.return_value = []
            await _call("list_notes", {}, identity=owner_identity, user_role="owner")
        assert mock_list.call_args.kwargs.get("scope") is None

    @pytest.mark.asyncio
    async def test_observe_mode_logs_drop_without_filtering(self, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="robothor.identity.scope")
        with _mode_env("observe"), patch("robothor.crm.dal.list_notes") as mock_list:
            mock_list.return_value = [
                {"id": "n1", "person_id": "person-1"},
                {"id": "n2", "person_id": "person-9"},
            ]
            await _call("list_notes", {}, identity=RESTRICTED_IDENTITY, user_role="member")
        assert mock_list.call_args.kwargs.get("scope") is None
        msgs = [r.getMessage() for r in caplog.records]
        assert any("would_drop=1" in m and "table=crm_notes" in m for m in msgs)


class TestOtherCrmHandlersWireScope:
    """Enforce-mode wiring smoke test for the remaining tool family — the
    scope math itself is covered by robothor/crm/tests/test_dal_data_scoping.py
    and robothor/identity/tests/test_scope.py."""

    @pytest.mark.asyncio
    async def test_get_person(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.get_person") as mock_get:
            mock_get.return_value = {"id": "person-1"}
            await _call(
                "get_person", {"id": "person-1"}, identity=RESTRICTED_IDENTITY, user_role="member"
            )
        assert mock_get.call_args.kwargs.get("scope") is not None

    @pytest.mark.asyncio
    async def test_list_people(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.list_people") as mock_list:
            mock_list.return_value = []
            await _call("list_people", {}, identity=RESTRICTED_IDENTITY, user_role="member")
        assert mock_list.call_args.kwargs.get("scope") is not None

    @pytest.mark.asyncio
    async def test_get_note(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.get_note") as mock_get:
            mock_get.return_value = {"id": "note-1"}
            await _call(
                "get_note", {"id": "note-1"}, identity=RESTRICTED_IDENTITY, user_role="member"
            )
        assert mock_get.call_args.kwargs.get("scope") is not None

    @pytest.mark.asyncio
    async def test_get_task(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.get_task") as mock_get:
            mock_get.return_value = {"id": "task-1"}
            await _call(
                "get_task", {"id": "task-1"}, identity=RESTRICTED_IDENTITY, user_role="member"
            )
        assert mock_get.call_args.kwargs.get("scope") is not None

    @pytest.mark.asyncio
    async def test_list_tasks(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.list_tasks") as mock_list:
            mock_list.return_value = []
            await _call("list_tasks", {}, identity=RESTRICTED_IDENTITY, user_role="member")
        assert mock_list.call_args.kwargs.get("scope") is not None

    @pytest.mark.asyncio
    async def test_list_conversations(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.list_conversations") as mock_list:
            mock_list.return_value = []
            await _call("list_conversations", {}, identity=RESTRICTED_IDENTITY, user_role="member")
        assert mock_list.call_args.kwargs.get("scope") is not None

    @pytest.mark.asyncio
    async def test_get_conversation(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.get_conversation") as mock_get:
            mock_get.return_value = {"id": 1}
            await _call(
                "get_conversation",
                {"conversationId": 1},
                identity=RESTRICTED_IDENTITY,
                user_role="member",
            )
        assert mock_get.call_args.kwargs.get("scope") is not None

    @pytest.mark.asyncio
    async def test_get_contact_360(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.get_contact_360") as mock_get:
            mock_get.return_value = {"person": {"id": "person-1"}}
            await _call(
                "get_contact_360",
                {"id": "person-1"},
                identity=RESTRICTED_IDENTITY,
                user_role="member",
            )
        assert mock_get.call_args.kwargs.get("scope") is not None

    @pytest.mark.asyncio
    async def test_search_records(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.search_records") as mock_search:
            mock_search.return_value = []
            await _call(
                "search_records",
                {"query": "alice"},
                identity=RESTRICTED_IDENTITY,
                user_role="member",
            )
        assert mock_search.call_args.kwargs.get("scope") is not None

    @pytest.mark.asyncio
    async def test_list_messages(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.list_messages") as mock_list:
            mock_list.return_value = []
            await _call(
                "list_messages",
                {"conversationId": 1},
                identity=RESTRICTED_IDENTITY,
                user_role="member",
            )
        assert mock_list.call_args.kwargs.get("scope") is not None

    @pytest.mark.asyncio
    async def test_list_agent_tasks(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.list_agent_tasks") as mock_list:
            mock_list.return_value = []
            await _call("list_agent_tasks", {}, identity=RESTRICTED_IDENTITY, user_role="member")
        assert mock_list.call_args.kwargs.get("scope") is not None

    @pytest.mark.asyncio
    async def test_list_my_tasks(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.list_agent_tasks") as mock_list:
            mock_list.return_value = []
            await _call("list_my_tasks", {}, identity=RESTRICTED_IDENTITY, user_role="member")
        assert mock_list.call_args.kwargs.get("scope") is not None


class TestListMessagesRefusal:
    """Finding 2 (Task 5 review): list_messages IDOR — a restricted caller
    with a bare conversationId could read any tenant conversation's
    messages. Enforce mode refuses; observe mode logs without refusing."""

    @pytest.mark.asyncio
    async def test_enforce_mode_denies_other_persons_conversation(self):
        with _mode_env("enforce"), patch("robothor.crm.dal.list_messages") as mock_list:
            mock_list.return_value = {
                "error": "Access denied — conversation not linked to your record"
            }
            result = await _call(
                "list_messages",
                {"conversationId": 1},
                identity=RESTRICTED_IDENTITY,
                user_role="member",
            )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_observe_mode_logs_would_refuse_without_denying(self, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="robothor.identity.scope")
        with (
            _mode_env("observe"),
            patch("robothor.crm.dal.list_messages") as mock_list,
            patch("robothor.crm.dal.get_conversation") as mock_get_convo,
        ):
            mock_list.return_value = [{"id": "m1"}, {"id": "m2"}]
            mock_get_convo.return_value = {"id": 1, "person_id": "person-9"}
            result = await _call(
                "list_messages",
                {"conversationId": 1},
                identity=RESTRICTED_IDENTITY,
                user_role="member",
            )
        # Observe must not filter/deny — the real (unrestricted) messages
        # still come back.
        assert result == {"payload": [{"id": "m1"}, {"id": "m2"}]}
        assert mock_list.call_args.kwargs.get("scope") is None
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "data_scoping" in m and "would_drop=2" in m and "table=crm_messages" in m for m in msgs
        )

    @pytest.mark.asyncio
    async def test_observe_mode_own_conversation_no_log(self, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="robothor.identity.scope")
        with (
            _mode_env("observe"),
            patch("robothor.crm.dal.list_messages") as mock_list,
            patch("robothor.crm.dal.get_conversation") as mock_get_convo,
        ):
            mock_list.return_value = [{"id": "m1"}]
            mock_get_convo.return_value = {"id": 1, "person_id": "person-1"}
            await _call(
                "list_messages",
                {"conversationId": 1},
                identity=RESTRICTED_IDENTITY,
                user_role="member",
            )
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("data_scoping" in m for m in msgs)


class TestSearchRecordsPerTableObserve:
    @pytest.mark.asyncio
    async def test_observe_mode_logs_per_table(self, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="robothor.identity.scope")
        with _mode_env("observe"), patch("robothor.crm.dal.search_records") as mock_search:
            mock_search.return_value = [
                {"id": "p1", "_table": "crm_people"},  # not own row -> dropped
                {"id": "n1", "person_id": "person-9", "_table": "crm_notes"},  # dropped
                {"id": "c1", "_table": "crm_companies"},  # unscoped, never dropped
            ]
            await _call(
                "search_records", {"query": "x"}, identity=RESTRICTED_IDENTITY, user_role="member"
            )
        assert mock_search.call_args.kwargs.get("scope") is None
        msgs = [r.getMessage() for r in caplog.records]
        assert any("would_drop=1" in m and "table=crm_people" in m for m in msgs)
        assert any("would_drop=1" in m and "table=crm_notes" in m for m in msgs)
        assert not any("table=crm_companies" in m for m in msgs)
