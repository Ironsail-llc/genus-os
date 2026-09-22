"""Private PostgreSQL plan approval admission, including overlapping workers."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extras import Json

from robothor.engine import chat_plan_claim
from robothor.engine.models import PlanState
from robothor.goals.tests.test_store import private_database  # noqa: F401


@pytest.fixture
def saved(private_database, monkeypatch):  # noqa: F811
    @contextmanager
    def connect():
        with psycopg2.connect(private_database) as conn:
            yield conn

    plan = PlanState(
        plan_id=str(uuid4()),
        plan_text="Check the synthetic task",
        original_message="Review the task",
        status="pending",
        created_at=datetime.now(UTC).isoformat(),
    )
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS agent_runs (
            id UUID PRIMARY KEY, tenant_id TEXT, user_id TEXT, parent_run_id UUID,
            correlation_id UUID, runtime_context JSONB)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS chat_sessions (
            tenant_id TEXT, session_key TEXT, plan_state JSONB,
            last_active_at TIMESTAMPTZ, PRIMARY KEY(tenant_id,session_key))""")
        cur.execute(
            "INSERT INTO chat_sessions(tenant_id,session_key,plan_state) VALUES ('tenant','session',%s) ON CONFLICT(tenant_id,session_key) DO UPDATE SET plan_state=EXCLUDED.plan_state",
            (Json(asdict(plan)),),
        )
    from pathlib import Path

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            (
                Path(__file__).resolve().parents[3]
                / "crm/migrations/140_chat_approval_receipts.sql"
            ).read_text()
        )
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM chat_approval_receipts")
    monkeypatch.setattr(chat_plan_claim, "get_connection", connect)
    return plan, connect


def test_concurrent_workers_consume_the_saved_plan_once(saved):
    plan, connect = saved
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda request: chat_plan_claim.claim("tenant", "session", plan, request),
                ["first", "second"],
            )
        )
    assert sorted(results) == [False, True]
    winner = ["first", "second"][results.index(True)]
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT plan_state FROM chat_sessions")
        recorded = cur.fetchone()[0]
    assert recorded["status"] == "approved"
    assert recorded["approval_request_id"] == winner
    assert not chat_plan_claim.claim("tenant", "session", plan, "after-restart")


@pytest.mark.parametrize(
    "mismatch",
    ["tenant", "session", "plan_id", "plan_text", "original_message", "deep_plan", "created_at"],
)
def test_claim_requires_scoped_exact_execution_intent(saved, mismatch):
    plan, _ = saved
    tenant, session = "tenant", "session"
    if mismatch == "tenant":
        tenant = "other"
    elif mismatch == "session":
        session = "other"
    else:
        plan = replace(plan, **{mismatch: True if mismatch == "deep_plan" else "changed"})
    assert not chat_plan_claim.claim(tenant, session, plan, "request")


def test_original_admission_survives_plan_clear_without_crossing_identity(saved):
    from robothor.auth.deps import AuthContext
    from robothor.engine.runtime.chat_control import request_key

    plan, connect = saved
    auth = AuthContext(tenant_id="tenant", user_id="operator", role="owner", typ="user")
    client_id = str(uuid4())
    identifier = request_key(auth, "session", client_id)
    assert not chat_plan_claim.already_admitted(auth, "session", client_id)
    assert chat_plan_claim.claim("tenant", "session", plan, identifier)
    # Claim committed, but the runner has not created its audit row yet.
    assert chat_plan_claim.already_admitted(auth, "session", client_id)
    assert not chat_plan_claim.already_admitted(auth, "session", str(uuid4()))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs(id,tenant_id,user_id,correlation_id) VALUES (%s,%s,%s,%s)",
            (str(uuid4()), auth.tenant_id, auth.user_id, identifier),
        )
        cur.execute("UPDATE chat_sessions SET plan_state=NULL WHERE tenant_id='tenant'")
    assert chat_plan_claim.already_admitted(auth, "session", client_id)
    assert not chat_plan_claim.already_admitted(auth, "other-session", client_id)
    assert not chat_plan_claim.already_admitted(
        replace(auth, user_id="other"), "session", client_id
    )
    assert not chat_plan_claim.already_admitted(
        replace(auth, tenant_id="other"), "session", client_id
    )


@pytest.mark.parametrize(
    "case", ["own", "new_plan", "new_approval", "pending_revision", "tenant", "session"]
)
def test_late_retirement_only_clears_its_own_approved_record(saved, case):
    plan, connect = saved
    assert chat_plan_claim.claim("tenant", "session", plan, "old-request")
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT plan_state FROM chat_sessions WHERE tenant_id='tenant' AND session_key='session'"
        )
        expected = cur.fetchone()[0]
        if case == "new_plan":
            expected.update(plan_id="new-plan", status="pending", approval_request_id="")
        elif case == "new_approval":
            expected["approval_request_id"] = "new-request"
        elif case == "pending_revision":
            expected.update(status="pending", plan_text="Revised plan")
        cur.execute(
            "UPDATE chat_sessions SET plan_state=%s WHERE tenant_id='tenant' AND session_key='session'",
            (Json(expected),),
        )
    chat_plan_claim.clear_claim(
        "other" if case == "tenant" else "tenant",
        "other" if case == "session" else "session",
        plan.plan_id,
        "old-request",
    )
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT plan_state FROM chat_sessions WHERE tenant_id='tenant' AND session_key='session'"
        )
        assert cur.fetchone()[0] == (None if case == "own" else expected)


async def test_cache_retirement_preserves_another_approval_of_same_plan(monkeypatch):
    from types import SimpleNamespace

    older = PlanState(
        plan_id="plan",
        plan_text="Check task",
        original_message="Check task",
        status="approved",
        approval_request_id="old",
    )
    newer = replace(older, approval_request_id="new")
    session = SimpleNamespace(active_plan=newer)
    monkeypatch.setattr(chat_plan_claim, "clear_claim", lambda *args: None)
    await chat_plan_claim.finish_plan(session, older, "tenant", "session")
    assert session.active_plan is newer


@pytest.mark.parametrize("intervening", ["reject", "approve", "revise"])
def test_delayed_revision_cannot_restore_or_overwrite_changed_plan(saved, monkeypatch, intervening):
    from robothor.engine import chat_plan_changes

    plan, connect = saved
    monkeypatch.setattr(chat_plan_changes, "get_connection", connect)
    newer = replace(plan, plan_id=str(uuid4()), plan_text="New reviewed draft")
    if intervening == "approve":
        assert chat_plan_claim.claim("tenant", "session", plan, "approval")
    else:
        assert chat_plan_changes.replace_pending(
            "tenant", "session", plan, newer if intervening == "revise" else None
        )
    assert not chat_plan_changes.replace_pending("tenant", "session", plan, newer)
    assert not chat_plan_changes.replace_pending("tenant", "session", plan, None)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT plan_state FROM chat_sessions WHERE tenant_id='tenant' AND session_key='session'"
        )
        recorded = cur.fetchone()[0]
    if intervening == "reject":
        assert recorded is None
    elif intervening == "approve":
        assert recorded["status"] == "approved" and recorded["approval_request_id"] == "approval"
    else:
        assert recorded["plan_id"] == newer.plan_id


def test_concurrent_revisions_publish_one_fresh_approval_identity(saved, monkeypatch):
    from robothor.engine import chat_plan_changes

    plan, connect = saved
    monkeypatch.setattr(chat_plan_changes, "get_connection", connect)
    revisions = [
        replace(plan, plan_id=str(uuid4()), plan_text=text) for text in ["First", "Second"]
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda revised: chat_plan_changes.replace_pending(
                    "tenant", "session", plan, revised
                ),
                revisions,
            )
        )
    assert sorted(results) == [False, True]
    assert not chat_plan_claim.claim("tenant", "session", plan, "stale-approval")
    winner = revisions[results.index(True)]
    assert chat_plan_claim.claim("tenant", "session", winner, "fresh-approval")


@pytest.mark.parametrize(
    "mismatch",
    ["tenant", "session", "plan_id", "plan_text", "original_message", "deep_plan", "created_at"],
)
def test_pending_changes_require_scoped_exact_draft(saved, monkeypatch, mismatch):
    from robothor.engine import chat_plan_changes

    plan, connect = saved
    monkeypatch.setattr(chat_plan_changes, "get_connection", connect)
    tenant, session = "tenant", "session"
    if mismatch == "tenant":
        tenant = "other"
    elif mismatch == "session":
        session = "other"
    else:
        plan = replace(plan, **{mismatch: True if mismatch == "deep_plan" else "changed"})
    assert not chat_plan_changes.replace_pending(tenant, session, plan, None)


@pytest.mark.parametrize("rejected", [False, True])
async def test_revision_publishes_only_durable_fresh_draft(saved, monkeypatch, rejected):
    from copy import deepcopy
    from hashlib import sha256
    from types import SimpleNamespace

    from robothor.engine import chat_plan_changes
    from robothor.engine.models import AgentRun, RunStatus

    plan, connect = saved
    monkeypatch.setattr(chat_plan_changes, "get_connection", connect)
    session = SimpleNamespace(active_plan=plan)
    snapshot = deepcopy(plan)
    run = AgentRun(status=RunStatus.COMPLETED, output_text="Revised draft[PLAN_READY]")
    if rejected:
        assert chat_plan_changes.replace_pending("tenant", "session", plan, None)
        session.active_plan = None
    revised, output = await chat_plan_changes.revise_plan(
        session, plan, snapshot, run, "Change it", "tenant", "session"
    )
    assert plan == snapshot
    if rejected:
        assert revised is None and session.active_plan is None
        assert "not applied" in output
    else:
        assert revised is session.active_plan
        assert revised.plan_id != plan.plan_id
        assert revised.plan_hash == sha256(revised.plan_text.encode()).hexdigest()[:16]
        assert revised.revision_count == 1
        assert revised.revision_history[0]["plan_text"] == plan.plan_text
        assert not chat_plan_claim.claim("tenant", "session", plan, "stale")
        assert chat_plan_claim.claim("tenant", "session", revised, "fresh")


def test_approval_receipt_survives_new_plan_before_worker_record(saved):
    from robothor.auth.deps import AuthContext
    from robothor.engine.runtime.chat_control import request_key

    plan, connect = saved
    auth = AuthContext(tenant_id="tenant", user_id="operator", role="owner", typ="user")
    client = str(uuid4())
    identifier = request_key(auth, "session", client)
    assert chat_plan_claim.claim("tenant", "session", plan, identifier)
    replacement = replace(plan, plan_id=str(uuid4()), plan_text="A different task")
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE chat_sessions SET plan_state=%s", (Json(asdict(replacement)),))
    assert chat_plan_claim.already_admitted(auth, "session", client)
    # Reusing an old request cannot consume a different pending plan.
    assert not chat_plan_claim.claim("tenant", "session", replacement, identifier)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT plan_state FROM chat_sessions")
        assert cur.fetchone()[0]["status"] == "pending"
        cur.execute(
            "SELECT plan_state FROM chat_approval_receipts WHERE request_id=%s", (identifier,)
        )
        assert cur.fetchone()[0]["plan_text"] == plan.plan_text
    assert chat_plan_claim.claim("tenant", "session", replacement, str(uuid4()))


def test_receipt_write_failure_rolls_back_plan_admission(saved):
    plan, connect = saved
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE chat_approval_receipts ADD CONSTRAINT reject_test_receipt CHECK (false) NOT VALID"
        )
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            chat_plan_claim.claim("tenant", "session", plan, str(uuid4()))
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT plan_state FROM chat_sessions WHERE tenant_id='tenant' AND session_key='session'"
            )
            assert cur.fetchone()[0]["status"] == "pending"
            cur.execute("SELECT count(*) FROM chat_approval_receipts")
            assert cur.fetchone()[0] == 0
    finally:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE chat_approval_receipts DROP CONSTRAINT reject_test_receipt")


def test_receipt_upgrade_backfills_existing_approval_once(saved):
    from pathlib import Path

    plan, connect = saved
    approved = replace(plan, status="approved", approval_request_id=str(uuid4()))
    migration = (
        Path(__file__).resolve().parents[3] / "crm/migrations/140_chat_approval_receipts.sql"
    ).read_text()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE chat_sessions SET plan_state=%s WHERE tenant_id='tenant' AND session_key='session'",
            (Json(asdict(approved)),),
        )
        cur.execute(migration)
        cur.execute(migration)
        cur.execute(
            "SELECT plan_state FROM chat_approval_receipts WHERE tenant_id='tenant' AND request_id=%s",
            (approved.approval_request_id,),
        )
        assert cur.fetchall() == [(asdict(approved),)]
