"""Verbatim chat search never reaches a workspace the caller cannot open.

``search_chat_turns`` searches every session of the tenant. A workspace's
conversation is a session too, so without a filter the main agent's memory
recall could quote a workspace to someone who has no access to it. Sessions
that back a workspace are left out unless the caller passes the ids of the
workspaces they can access; only those are searched.
"""

from __future__ import annotations

import uuid

import pytest

from robothor.engine import chat_store
from robothor.rooms.access import room_session_key
from robothor.rooms.tests.conftest import committed_connection, rooms_db  # noqa: F401

pytestmark = pytest.mark.integration

VECTOR = [0.1] * 1024


@pytest.fixture
def searchable(rooms_db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(chat_store, "get_connection", committed_connection)
    db = rooms_db
    mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    keys = {
        "main": f"agent:main:user:{db.alice}",
        "mine": room_session_key("main", mine),
        "theirs": room_session_key("analyst", theirs),
    }
    for name, key in keys.items():
        chat_store.save_exchange(key, f"question in {name}", f"answer in {name}", tenant_id=db.tenant)
    db.cur.execute(
        "UPDATE chat_messages SET embedding = %s::vector WHERE session_id IN "
        "(SELECT id FROM chat_sessions WHERE tenant_id = %s)",
        (VECTOR, db.tenant),
    )
    db.keys, db.mine, db.theirs = keys, mine, theirs
    return db


def _sessions(rows):
    return {r["session_key"] for r in rows}


def test_without_room_ids_no_workspace_is_searched(searchable):
    db = searchable
    rows = chat_store.search_chat_turns(VECTOR, limit=50, tenant_id=db.tenant)
    assert _sessions(rows) == {db.keys["main"]}


def test_only_the_workspaces_passed_in_are_searched(searchable):
    db = searchable
    rows = chat_store.search_chat_turns(VECTOR, limit=50, tenant_id=db.tenant, room_ids=[db.mine])
    assert _sessions(rows) == {db.keys["main"], db.keys["mine"]}


def test_an_empty_list_searches_no_workspace(searchable):
    db = searchable
    rows = chat_store.search_chat_turns(VECTOR, limit=50, tenant_id=db.tenant, room_ids=[])
    assert _sessions(rows) == {db.keys["main"]}
