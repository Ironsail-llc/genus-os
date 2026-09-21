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
        cur.execute("""CREATE TABLE IF NOT EXISTS chat_sessions (
            tenant_id TEXT, session_key TEXT, plan_state JSONB,
            last_active_at TIMESTAMPTZ, PRIMARY KEY(tenant_id,session_key))""")
        cur.execute(
            "INSERT INTO chat_sessions(tenant_id,session_key,plan_state) VALUES ('tenant','session',%s) ON CONFLICT(tenant_id,session_key) DO UPDATE SET plan_state=EXCLUDED.plan_state",
            (Json(asdict(plan)),),
        )
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
