"""Lost chat delivery is recovered from scoped durable records, never replayed."""

from contextlib import contextmanager
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extras import Json

from robothor.auth.deps import AuthContext
from robothor.engine import chat_recovery
from robothor.engine.runtime.chat_control import request_key
from robothor.engine.tests.test_chat_per_user_sessions import (  # noqa: F401
    chat_app,
    mock_runner,
)
from robothor.goals.tests.test_store import private_database  # noqa: F401


@pytest.fixture
def records(private_database, monkeypatch):  # noqa: F811
    @contextmanager
    def connect():
        with psycopg2.connect(private_database) as conn:
            yield conn

    with connect() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS agent_runs (
            id UUID PRIMARY KEY,tenant_id TEXT,user_id TEXT,parent_run_id UUID,
            correlation_id UUID,runtime_context JSONB,status TEXT,output_text TEXT,
            error_message TEXT,verified_status TEXT,started_at TIMESTAMPTZ DEFAULT now())""")
    monkeypatch.setattr(chat_recovery, "get_connection", connect)
    return connect


def identity():
    return AuthContext(user_id="operator", tenant_id=str(uuid4()), role="owner", typ="user")


def insert(connect, auth, client, status="completed", *, session="web:main", **changes):
    key = request_key(auth, session, client)
    row = {
        "id": str(uuid4()),
        "tenant_id": auth.tenant_id,
        "user_id": auth.user_id,
        "parent_run_id": None,
        "correlation_id": key,
        "runtime_context": Json({"request_id": key}),
        "status": status,
        "output_text": "Recorded result: task created once.",
        "error_message": None,
        "verified_status": "verified",
    }
    row.update(changes)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs ("
            + ",".join(row)
            + ") VALUES ("
            + ",".join(["%s"] * len(row))
            + ")",
            list(row.values()),
        )
    return row["id"]


def test_recovered_result_survives_session_loss_and_is_scoped(records):
    auth, client = identity(), str(uuid4())
    run = insert(records, auth, client)
    for _ in range(2):  # no session registry or execution object is involved
        outcome = chat_recovery.read_outcome(auth, "web:main", client)
        assert outcome["terminal"] and outcome["run_id"] == run
        assert outcome["text"] == "Recorded result: task created once."
    assert not chat_recovery.read_outcome(auth, "another-session", client)["terminal"]
    assert not chat_recovery.read_outcome(identity(), "web:main", client)["terminal"]
    other = AuthContext(user_id="other", tenant_id=auth.tenant_id, role="owner", typ="user")
    assert not chat_recovery.read_outcome(other, "web:main", client)["terminal"]
    with records() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_runs WHERE tenant_id=%s", (auth.tenant_id,))
        assert cur.fetchone()[0] == 1


@pytest.mark.parametrize(
    "status", ["running", "pending", "awaiting_approval", "failed", "timeout", "cancelled"]
)
def test_recovery_does_not_claim_partial_output_is_success(records, status):
    auth, client = identity(), str(uuid4())
    insert(
        records,
        auth,
        client,
        status,
        output_text="Everything is done.",
        error_message="Dispatched effect requires reconciliation",
        verified_status=None,
    )
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert result["state"] == status
    assert result["terminal"] is (status in {"failed", "timeout", "cancelled"})
    assert "Everything is done" not in result["text"]
    assert not result["verified"]


def test_missing_ambiguous_and_child_records_never_select_an_arbitrary_answer(records):
    auth, client = identity(), str(uuid4())
    assert chat_recovery.read_outcome(auth, "web:main", client)["state"] == "not_found"
    parent = insert(
        records, auth, client, correlation_id=None
    )  # runtime metadata supports deep runs
    insert(records, auth, client, parent_run_id=parent)
    assert chat_recovery.read_outcome(auth, "web:main", client)["run_id"] == parent
    insert(records, auth, client)
    assert chat_recovery.read_outcome(auth, "web:main", client) == {
        "state": "ambiguous",
        "terminal": False,
    }


async def test_http_recovery_reads_durable_answer_after_session_cache_loss(
    records,
    chat_app,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
):
    from unittest.mock import patch

    from httpx import ASGITransport, AsyncClient

    from robothor.engine.chat import _sessions

    auth, client_id = identity(), str(uuid4())
    insert(records, auth, client_id)
    _sessions.clear()
    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "off")
    with patch("robothor.engine.chat._auth_context", return_value=auth):
        async with AsyncClient(
            transport=ASGITransport(app=chat_app), base_url="http://test"
        ) as http:
            response = await http.get(
                "/chat/outcome", params={"request_id": client_id, "session_key": "web:main"}
            )
            invalid = await http.get(
                "/chat/outcome", params={"request_id": "not-a-uuid", "session_key": "web:main"}
            )
    assert response.status_code == 200
    assert response.json()["text"] == "Recorded result: task created once."
    assert response.headers["cache-control"] == "no-store"
    assert invalid.status_code == 400
    mock_runner.execute.assert_not_called()
    assert not _sessions
