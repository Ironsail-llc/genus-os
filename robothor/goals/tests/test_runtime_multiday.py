"""A restored waiting goal wakes once, completes its child, then verifies the parent."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from robothor.engine.models import AgentRun, RunStatus
from robothor.goals import store
from robothor.goals.controller import GoalController
from robothor.goals.model import CreateGoal, GoalUpdate
from robothor.goals.runtime import attach_run
from robothor.goals.tests.test_store import db, private_database  # noqa: F401


@pytest.mark.asyncio
async def test_restored_multiday_family_waits_without_models_and_wakes_once(db):  # noqa: F811
    receipt = str(uuid4())
    parent = store.create(
        db,
        CreateGoal(
            objective="Deliver after the customer replies",
            success_criteria=["calendar-operation:" + receipt],
            kind="long",
            token_budget=50,
        ),
        "operator",
    )
    child = store.create(
        db,
        CreateGoal(
            objective="Confirm the appointment",
            success_criteria=["calendar-operation:" + receipt],
            parent_goal_id=parent["id"],
        ),
        "operator",
    )
    child = store.update(
        db,
        child["id"],
        GoalUpdate(
            action="wait",
            version=child["version"],
            note="Customer reply due tomorrow",
            event_type="fixture.reply",
            event_match={"customer": "synthetic"},
        ),
        "main",
    )
    parent = store.get(db, parent["id"])
    store.update(
        db,
        parent["id"],
        GoalUpdate(
            action="wait", version=parent["version"], note="Child is waiting for tomorrow's reply"
        ),
        "main",
    )
    assert store.claim(db) is None
    # Restore a day-old waiting snapshot, as after a process restart. No sleeping/model polling.
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with store.transaction() as cur:
        cur.execute(
            "UPDATE pursuit_goals SET data=jsonb_set(data,'{wait,registered_at}',%s::jsonb) WHERE tenant_id=%s",
            (json.dumps(yesterday), db),
        )
        cur.execute(
            "CREATE TABLE calendar_operations(id UUID PRIMARY KEY,tenant_id TEXT,status TEXT,result JSONB,updated_at TIMESTAMPTZ)"
        )
        cur.execute(
            "INSERT INTO calendar_operations VALUES (%s,%s,'completed',%s::jsonb,now())",
            (receipt, db, json.dumps({"verification": "verified"})),
        )
    for _ in range(2):
        store.ingest_event(db, "same-reply-event", "fixture.reply", {"customer": "synthetic"})
    calls = []

    async def execute(**kwargs):
        goal_id = kwargs["trigger_detail"].removeprefix("goal:")
        calls.append(goal_id)
        run = AgentRun(
            tenant_id=db,
            trigger_detail=kwargs["trigger_detail"],
            status=RunStatus.COMPLETED,
            input_tokens=5,
        )
        attach_run(run)
        g = store.control(db, goal_id)
        g = store.update(
            db,
            goal_id,
            GoalUpdate(
                action="evidence",
                version=g["version"],
                criterion=0,
                reference="calendar-operation:" + receipt,
                satisfied=True,
                note="Independently checked the durable provider receipt",
            ),
            "main",
        )
        store.update(
            db,
            goal_id,
            GoalUpdate(action="complete", version=g["version"], note="Receipt checked"),
            "main",
        )
        return run

    controller = GoalController(
        SimpleNamespace(execute=execute), SimpleNamespace(tenant_id=db, manifest_dir="unused")
    )
    with (
        patch("robothor.engine.config.load_agent_config_or_broken", return_value=SimpleNamespace()),
        patch("robothor.goals.events.capture"),
    ):
        await controller.tick()
        await controller.tick()
    assert calls == [child["id"], parent["id"]]
    result = store.get(db, parent["id"])
    assert result["status"] == "complete" and result["tokens_used"] == 10
    assert result["children"][0]["status"] == "complete"
