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
            id UUID PRIMARY KEY,tenant_id TEXT,user_id TEXT,agent_id TEXT DEFAULT 'main',parent_run_id UUID,
            correlation_id UUID,runtime_context JSONB,status TEXT,output_text TEXT,
            error_message TEXT,verified_status TEXT,trigger_detail TEXT,started_at TIMESTAMPTZ DEFAULT now())""")
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS agent_run_steps (
            run_id UUID,step_number INTEGER,tool_name TEXT,tool_input JSONB,tool_output JSONB)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS calendar_operations (
            id UUID PRIMARY KEY,tenant_id TEXT,user_id TEXT,agent_id TEXT,status TEXT,result JSONB,
            created_at TIMESTAMPTZ DEFAULT now())""")
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


def record_calendar_receipt(
    connect, auth, run, *, status="completed", result=None, user=None, agent="main", output_id=None
):
    operation = str(uuid4())
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO calendar_operations(id,tenant_id,user_id,agent_id,status,result) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                operation,
                auth.tenant_id,
                user or auth.user_id,
                agent,
                status,
                Json(result or {"verification": "verified", "invitations_requested": True}),
            ),
        )
        cur.execute(
            "INSERT INTO agent_run_steps VALUES (%s,1,'gws_calendar_add_attendees',%s,%s)",
            (
                run,
                Json({"operation_id": operation}),
                Json({"operation_id": output_id or operation, "verification": "verified"}),
            ),
        )
    return operation


def test_interrupted_run_reports_verified_action_receipt_without_claiming_delivery(records):
    auth, client = identity(), str(uuid4())
    run = insert(
        records, auth, client, "cancelled", error_message="Run interrupted", verified_status=None
    )
    operation = record_calendar_receipt(records, auth, run)
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert result["state"] == "cancelled" and not result["verified"]
    assert result["effects"][0]["operation_id"] == operation
    assert result["effects"][0]["verified"] is True
    assert "calendar change is recorded as complete and verified" in result["text"]
    assert "delivery is not verified" in result["text"]
    assert "Run interrupted" in result["text"]


@pytest.mark.parametrize("status", ["blocked", "executing"])
def test_unverified_operation_overrides_successful_run_prose(records, status):
    auth, client = identity(), str(uuid4())
    run = insert(records, auth, client, output_text="Everything was completed successfully")
    record_calendar_receipt(
        records, auth, run, status=status, result={"verification": "unverified"}
    )
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert not result["verified"]
    assert "Everything was completed successfully" not in result["text"]
    assert "not fully verified" in result["text"]


@pytest.mark.parametrize("mismatch", ["principal", "agent", "operation_reference"])
def test_run_reference_cannot_expose_another_receipt_or_conflicting_audit(records, mismatch):
    auth, client = identity(), str(uuid4())
    run = insert(records, auth, client)
    record_calendar_receipt(
        records,
        auth,
        run,
        user="other" if mismatch == "principal" else None,
        agent="other" if mismatch == "agent" else "main",
        output_id=str(uuid4()) if mismatch == "operation_reference" else None,
    )
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert not result["verified"]
    assert all(not effect["verified"] for effect in result["effects"])
    assert all(effect["status"] == "unmatched" for effect in result["effects"])
    assert "Recorded result: task created once." not in result["text"]


def test_equivalent_uuid_references_match_without_trusting_invalid_reference_text(records):
    auth, client = identity(), str(uuid4())
    run = insert(records, auth, client)
    operation = record_calendar_receipt(records, auth, run)
    with records() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_run_steps SET tool_input=%s WHERE run_id=%s",
            (Json({"operation_id": operation.upper()}), run),
        )
    assert chat_recovery.read_outcome(auth, "web:main", client)["effects"][0]["verified"]
    with records() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_run_steps SET tool_input=%s WHERE run_id=%s",
            (Json({"operation_id": "untrusted invalid reference"}), run),
        )
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert not result["verified"]
    assert "untrusted invalid reference" not in result["text"]


@pytest.mark.parametrize("status", ["completed", "executing"])
def test_deferred_calendar_receipt_uses_nested_arguments_and_direct_result(records, status):
    auth, client = identity(), str(uuid4())
    run = insert(records, auth, client)
    operation = record_calendar_receipt(records, auth, run, status=status)
    with records() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_run_steps SET tool_name='tool_call',tool_input=%s WHERE run_id=%s",
            (
                Json(
                    {
                        "name": " gws_calendar_add_attendees ",
                        "arguments": {"operation_id": operation},
                    }
                ),
                run,
            ),
        )
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert len(result["effects"]) == 1
    assert result["effects"][0]["operation_id"] == operation
    assert result["verified"] is (status == "completed")
    assert result["effects"][0]["verified"] is (status == "completed")


@pytest.mark.parametrize("arguments", [None, [], "invalid"])
def test_invalid_deferred_arguments_cannot_claim_calendar_receipt(records, arguments):
    auth, client = identity(), str(uuid4())
    run = insert(records, auth, client)
    record_calendar_receipt(records, auth, run)
    with records() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_run_steps SET tool_name='tool_call',tool_input=%s WHERE run_id=%s",
            (Json({"name": "gws_calendar_add_attendees", "arguments": arguments}), run),
        )
    assert chat_recovery.read_outcome(auth, "web:main", client)["effects"] == []


def test_unrelated_deferred_tool_output_is_not_a_calendar_receipt(records):
    auth, client = identity(), str(uuid4())
    run = insert(records, auth, client)
    operation = record_calendar_receipt(records, auth, run)
    with records() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_run_steps SET tool_name='tool_call',tool_input=%s WHERE run_id=%s",
            (Json({"name": "other_tool", "arguments": {"operation_id": operation}}), run),
        )
    assert chat_recovery.read_outcome(auth, "web:main", client)["effects"] == []


@pytest.mark.parametrize("status", ["executing", "blocked", "completed", "draft"])
def test_terminal_run_distinguishes_pending_action_reconciliation(records, status):
    auth, client = identity(), str(uuid4())
    run = insert(records, auth, client, "cancelled", verified_status=None)
    operation = record_calendar_receipt(records, auth, run, status=status)
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert result["terminal"] is True
    assert result["reconciliation_pending"] is (status == "executing")
    if status == "executing":
        with records() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE calendar_operations SET status='completed' WHERE id=%s", (operation,)
            )
        refreshed = chat_recovery.read_outcome(auth, "web:main", client)
        assert not refreshed["reconciliation_pending"]
        assert refreshed["effects"][0]["verified"]
        assert refreshed["state"] == "cancelled"


async def test_http_recovery_initiates_scoped_readback_without_replaying_action(
    records,
    chat_app,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
):
    from unittest.mock import Mock, patch

    from httpx import ASGITransport, AsyncClient

    from robothor.engine import calendar_operations

    auth, client_id = identity(), str(uuid4())
    run = insert(records, auth, client_id, "cancelled", verified_status=None)
    operation = record_calendar_receipt(records, auth, run, status="executing")
    with records() as conn, conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE calendar_operations ADD COLUMN calendar_id TEXT, ADD COLUMN event_id TEXT, ADD COLUMN arguments JSONB, ADD COLUMN updated_at TIMESTAMPTZ"
        )
        cur.execute(
            "UPDATE calendar_operations SET calendar_id='fixture-calendar',event_id='fixture-event',arguments=%s,updated_at=now()-interval '1 minute' WHERE id=%s",
            (Json({"attendees": ["sam@example.com"]}), operation),
        )
    readback = Mock(
        return_value={
            "error": "Interrupted write reconciled without retry; notification outcome unknown",
            "verification": "unverified",
            "attendees_present": ["sam@example.com"],
            "invitations_requested": None,
        }
    )
    monkeypatch.setattr(calendar_operations, "get_connection", records)
    monkeypatch.setattr(calendar_operations, "_reconcile_interrupted", readback)
    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "off")
    with patch("robothor.engine.chat._auth_context", return_value=auth):
        async with AsyncClient(
            transport=ASGITransport(app=chat_app), base_url="http://test"
        ) as http:
            first = await http.get(
                "/chat/outcome", params={"request_id": client_id, "session_key": "web:main"}
            )
            second = await http.get(
                "/chat/outcome", params={"request_id": client_id, "session_key": "web:main"}
            )
    assert first.json()["reconciliation_pending"]
    assert not second.json()["reconciliation_pending"]
    assert second.json()["state"] == "cancelled"
    assert "Readback found these requested attendees: sam@example.com" in second.json()["text"]
    assert "Whether notifications were sent remains unknown" in second.json()["text"]
    readback.assert_called_once_with(
        "fixture-calendar",
        "fixture-event",
        {"attendees": ["sam@example.com"]},
        {"verification": "verified", "invitations_requested": True},
    )
    mock_runner.execute.assert_not_called()


@pytest.mark.parametrize("acknowledged", [True, None])
def test_recovered_attendees_keep_known_notification_request_distinct_from_delivery(
    records, acknowledged
):
    auth, client = identity(), str(uuid4())
    run = insert(records, auth, client, "cancelled", verified_status=None)
    record_calendar_receipt(
        records,
        auth,
        run,
        status="blocked",
        result={
            "verification": "unverified",
            "attendees_present": ["sam@example.com"],
            "invitations_requested": acknowledged,
        },
    )
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert not result["verified"]
    assert "sam@example.com" in result["text"]
    if acknowledged:
        assert "Notifications were requested; delivery is not verified" in result["text"]
        assert "Whether notifications were sent remains unknown" not in result["text"]
    else:
        assert "Whether notifications were sent remains unknown" in result["text"]


def test_browser_recovery_scope_separates_tenants_principals_and_sessions():
    from types import SimpleNamespace

    from robothor.engine.runtime.chat_control import recovery_scope

    auth = SimpleNamespace(tenant_id="tenant", user_id="person")
    scope = recovery_scope(auth, "web:main")
    assert scope == recovery_scope(auth, "web:main")
    assert scope != recovery_scope(auth, "web:other")
    assert scope != recovery_scope(SimpleNamespace(tenant_id="other", user_id="person"), "web:main")
    assert scope != recovery_scope(SimpleNamespace(tenant_id="tenant", user_id="other"), "web:main")
    assert recovery_scope(SimpleNamespace(tenant_id="a:b", user_id="c"), "d") != recovery_scope(
        SimpleNamespace(tenant_id="a", user_id="b:c"), "d"
    )


async def test_history_exposes_authenticated_noncacheable_recovery_namespace(
    chat_app,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
):
    from unittest.mock import patch

    from httpx import ASGITransport, AsyncClient

    from robothor.engine.runtime.chat_control import recovery_scope

    auth = identity()
    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "off")
    with patch("robothor.engine.chat._auth_context", return_value=auth):
        async with AsyncClient(
            transport=ASGITransport(app=chat_app), base_url="http://test"
        ) as http:
            response = await http.get("/chat/history", params={"session_key": "web:main"})
    assert response.json()["recoveryScope"] == recovery_scope(auth, "web:main")
    assert response.headers["cache-control"] == "no-store"
    mock_runner.execute.assert_not_called()


@pytest.mark.parametrize("depth", [1, 2])
def test_delegated_calendar_effect_prevents_false_parent_completion(records, monkeypatch, depth):
    from unittest.mock import Mock

    from robothor.engine import calendar_reconciliation

    auth, client = identity(), str(uuid4())
    parent = insert(records, auth, client, output_text="Everything was completed successfully")
    child = parent
    for _ in range(depth):
        child = insert(records, auth, str(uuid4()), parent_run_id=child, agent_id="delegate")
    operation = record_calendar_receipt(
        records,
        auth,
        child,
        agent="delegate",
        status="executing",
        result={"verification": "unverified", "reconciliation_pending": True},
    )
    outcome = chat_recovery.read_outcome(auth, "web:main", client)
    assert outcome["reconciliation_pending"] and not outcome["verified"]
    assert "Everything was completed successfully" not in outcome["text"]
    assert outcome["effects"][0]["operation_id"] == operation
    assert outcome["effects"][0]["agent_id"] == "delegate"
    readback = Mock()
    monkeypatch.setattr(calendar_reconciliation, "reconcile_record", readback)
    calendar_reconciliation.reconcile_outcome(auth, "web:main", client)
    readback.assert_called_once_with(operation, auth, "delegate")


@pytest.mark.parametrize("boundary", ["tenant", "principal", "unrelated"])
def test_delegated_receipts_cannot_cross_run_authority(records, boundary):
    auth, client = identity(), str(uuid4())
    parent = insert(records, auth, client)
    other = (
        identity()
        if boundary == "tenant"
        else AuthContext(user_id="other", tenant_id=auth.tenant_id, role="owner", typ="user")
        if boundary == "principal"
        else auth
    )
    child = insert(
        records, other, str(uuid4()), parent_run_id=None if boundary == "unrelated" else parent
    )
    # Even a same-principal grandchild beneath a foreign boundary is excluded.
    grandchild = insert(records, auth, str(uuid4()), parent_run_id=child)
    record_calendar_receipt(records, other, child, status="executing")
    record_calendar_receipt(records, auth, grandchild, status="executing")
    outcome = chat_recovery.read_outcome(auth, "web:main", client)
    assert outcome["effects"] == []
    assert not outcome["reconciliation_pending"]


@pytest.mark.parametrize("conflict_first", [True, False])
def test_family_receipts_preserve_conflicting_evidence(records, conflict_first):
    auth, client = identity(), str(uuid4())
    parent_id, child_id = sorted([str(uuid4()), str(uuid4())])
    parent = insert(records, auth, client, id=parent_id)
    child = insert(records, auth, str(uuid4()), parent_run_id=parent, id=child_id)
    conflict, valid = (parent, child) if conflict_first else (child, parent)
    operation = record_calendar_receipt(records, auth, valid)
    with records() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_run_steps VALUES (%s,1,'gws_calendar_add_attendees',%s,%s)",
            (conflict, Json({"operation_id": operation}), Json({"operation_id": str(uuid4())})),
        )
    outcome = chat_recovery.read_outcome(auth, "web:main", client)
    assert len(outcome["effects"]) == 1
    assert outcome["effects"][0]["status"] == "unmatched"
    assert not outcome["verified"]


def test_chat_recovery_follows_native_resume_chain(records):
    auth, client = identity(), str(uuid4())
    original = insert(records, auth, client, "cancelled", verified_status=None)
    middle = insert(
        records,
        auth,
        str(uuid4()),
        "cancelled",
        runtime_context=Json({"resume_from_run_id": original}),
        verified_status=None,
    )
    latest = insert(
        records,
        auth,
        str(uuid4()),
        runtime_context=Json({"resume_from_run_id": middle}),
        output_text="Saved work continued successfully.",
    )
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert result["run_id"] == latest and result["state"] == "completed"
    assert result["text"] == "Saved work continued successfully."


def test_resume_chain_never_crosses_identity_scope(records):
    auth, client = identity(), str(uuid4())
    original = insert(records, auth, client, "cancelled", verified_status=None)
    for changes in ({"tenant_id": str(uuid4())}, {"user_id": "other"}):
        insert(
            records,
            auth,
            str(uuid4()),
            runtime_context=Json({"resume_from_run_id": original}),
            **changes,
        )
    assert chat_recovery.read_outcome(auth, "web:main", client)["run_id"] == original


@pytest.mark.parametrize("cycle", [False, True])
def test_resume_forks_and_cycles_do_not_select_a_success(records, cycle):
    auth, client = identity(), str(uuid4())
    original = insert(records, auth, client, "cancelled", verified_status=None)
    child = insert(
        records, auth, str(uuid4()), runtime_context=Json({"resume_from_run_id": original})
    )
    if cycle:
        with records() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_runs SET runtime_context=runtime_context || %s WHERE id=%s",
                (Json({"resume_from_run_id": child}), original),
            )
    else:
        insert(records, auth, str(uuid4()), runtime_context=Json({"resume_from_run_id": original}))
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert result == {"state": "ambiguous", "terminal": False}


def test_resumed_success_keeps_uncertain_prior_and_delegated_receipts(records):
    auth, client = identity(), str(uuid4())
    original = insert(records, auth, client, "cancelled", verified_status=None)
    operation = record_calendar_receipt(records, auth, original, status="executing")
    latest = insert(
        records, auth, str(uuid4()), runtime_context=Json({"resume_from_run_id": original})
    )
    child = insert(records, auth, str(uuid4()), parent_run_id=latest)
    child_operation = record_calendar_receipt(records, auth, child)
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert result["run_id"] == latest and result["terminal"]
    assert result["reconciliation_pending"] and not result["verified"]
    assert {item["operation_id"] for item in result["effects"]} == {operation, child_operation}
    assert "not fully verified" in result["text"]
