"""Persisted goal periods and revisions reject stale receipts atomically."""

import os
from uuid import uuid4

import psycopg2
import pytest

from robothor.goals import store
from robothor.goals.model import CreateGoal, GoalUpdate
from robothor.goals.presentation import render_goal_progress

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("boundary", ["assessment", "revision"])
def test_stale_receipt_cannot_satisfy_new_period_or_revision(boundary):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, receipt = "fresh-evidence-" + uuid4().hex, str(uuid4())
    reference = "calendar-operation:" + receipt
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    store.set_enabled(tenant, False, "operator")
    goal = store.create(
        tenant,
        CreateGoal(
            objective="Review the synthetic receipt",
            kind="long",
            mode="ongoing",
            success_criteria=[reference],
        ),
        "operator",
    )

    def change(action, **kwargs):
        nonlocal goal
        goal = store.update(
            tenant,
            goal["id"],
            GoalUpdate(action=action, version=goal["version"], **kwargs),
            "operator",
            operator=True,
        )
        return goal

    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO calendar_operations(id,tenant_id,agent_id,calendar_id,event_id,arguments,status,result)
               VALUES (%s,%s,'main','synthetic','synthetic','{}','completed','{"verification":"verified"}')""",
            (receipt, tenant),
        )
    evidence = {
        "criterion": 0,
        "reference": reference,
        "satisfied": True,
        "note": "Synthetic receipt checked",
    }
    change("evidence", **evidence)
    assert goal["evidence"][0]["verification"]["criterion_verified"]
    if boundary == "assessment":
        change("assess", assessment="meeting", note="Receipt verified for this period")
        assert goal["status"] == "waiting"
    else:
        change("revise", success_criteria=[reference], note="Require a fresh verification")
    before = store.get(tenant, goal["id"])
    assert before["evidence"] == []

    def history_count():
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pursuit_goal_history WHERE tenant_id=%s AND goal_id=%s",
                (tenant, goal["id"]),
            )
            return cur.fetchone()[0]

    recorded = history_count()
    with pytest.raises(ValueError, match="predates"):
        change("evidence", **evidence)
    with pytest.raises(ValueError, match="requires evidence"):
        change("assess", assessment="meeting", note="Attempt to reuse the earlier result")
    assert store.get(tenant, goal["id"]) == before
    assert history_count() == recorded
    text = render_goal_progress(before, execution_enabled=False)
    assert "This is an ongoing goal; completed tasks do not finish it." in text
    assert "No current criterion evidence has been recorded." in text

    # A new synthetic provider readback can satisfy the new period/revision.
    # Only this disposable receipt is updated; no calendar tool is invoked.
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE calendar_operations SET updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",
            (tenant, receipt),
        )
    change("evidence", **evidence)
    change("assess", assessment="meeting", note="Fresh synthetic readback verified")
    assert goal["status"] == "waiting" and goal["evidence"] == []
    assert goal["assessment"]["status"] == "meeting"
