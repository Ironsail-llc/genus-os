"""Native goal hierarchy survives a simulated next day alongside ordinary work."""

import asyncio
import json
import os
import time
from collections import Counter
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import litellm
import psycopg2
import pytest

from robothor.crm import dal
from robothor.engine.models import RunStatus, TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import effects
from robothor.engine.task_registry import get_task_registry
from robothor.goals import store
from robothor.goals.controller import GoalController
from robothor.goals.model import CreateGoal, GoalUpdate
from robothor.goals.runtime import binding

pytestmark = pytest.mark.integration


async def test_native_multiday_parent_child_and_everyday_requests(
    engine_config, sample_agent_config, monkeypatch
):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, receipt = "native-multiday-" + uuid4().hex, str(uuid4())
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    monkeypatch.setattr("robothor.goals.events.capture", lambda *a: None)
    config = replace(engine_config, tenant_id=tenant)
    agent = replace(
        sample_agent_config,
        id="main",
        task_protocol=True,
        auto_task=False,
        todo_list_enabled=True,
        model_fallbacks=[],
        tools_allowed=["create_task", "get_pursuit_goal", "update_pursuit_goal"],
    )
    monkeypatch.setattr("robothor.engine.config.load_agent_config_or_broken", lambda *a: agent)
    store.set_enabled(tenant, True, "operator")
    parent = store.create(
        tenant,
        CreateGoal(
            objective="Finish the follow-up after tomorrow's reply",
            success_criteria=["calendar-operation:" + receipt],
            kind="long",
            token_budget=1000000,
        ),
        "operator",
    )
    child = store.create(
        tenant,
        CreateGoal(
            objective="Check tomorrow's appointment confirmation",
            success_criteria=["calendar-operation:" + receipt],
            parent_goal_id=parent["id"],
        ),
        "operator",
    )
    parent = store.get(tenant, parent["id"])
    store.update(
        tenant,
        parent["id"],
        GoalUpdate(action="wait", version=parent["version"], note="Wait for the authorized child"),
        "operator",
    )
    everyday = ContextVar("multiday_everyday_request")
    counts = Counter()
    planning_calls = Counter()
    ordinary_latencies = []
    day_two = False
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(**kwargs):
        current = binding.get()
        key = current.goal_id if current else "interactive-" + str(everyday.get())
        if kwargs.get("response_format") == {"type": "json_object"}:
            assert current is not None, "Unexpected auxiliary model call"
            planning_calls[key] += 1
            assert planning_calls[key] <= 2
            return litellm.ModelResponse(
                model=agent.model_primary,
                choices=[
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "difficulty": "complex",
                                    "estimated_steps": 2,
                                    "plan": [
                                        {
                                            "step": 1,
                                            "action": "Check the goal and record its next state",
                                            "tool": "update_pursuit_goal",
                                        }
                                    ],
                                    "risks": [],
                                    "success_criteria": "Check the durable criterion evidence",
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            )
        counts[key] += 1
        text = None
        if current:
            assert counts[key] <= 3
            state = store.control(tenant, key)
            if key == child["id"] and (not day_two or not state["evidence"]):
                entered.set()
                await asyncio.wait_for(release.wait(), timeout=10)
            if not day_two:
                action = "wait"
                extras = (
                    {"event_type": "fixture.reply", "event_match": {"customer": "synthetic"}}
                    if key == child["id"]
                    else {}
                )
            elif not state["evidence"]:
                action = "evidence"
                extras = {
                    "criterion": 0,
                    "reference": "calendar-operation:" + receipt,
                    "satisfied": True,
                }
            else:
                action, extras = "complete", {}
            if day_two and key == parent["id"]:
                assert store.control(tenant, child["id"])["status"] == "complete"
            tool = "update_pursuit_goal"
            args = {
                "goal_id": key,
                "version": state["version"],
                "action": action,
                "note": "Check the saved synthetic appointment result"
                if day_two
                else "Wait for tomorrow's reply",
                **extras,
            }
        elif counts[key] == 1:
            tool, args = "create_task", {"title": key, "body": "Synthetic everyday work"}
        else:
            assert counts[key] == 2
            text = "The requested task was created."
        message = {"role": "assistant", "content": text}
        if text is None:
            message["tool_calls"] = [
                {
                    "id": key + str(counts[key]),
                    "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(args)},
                }
            ]
        return litellm.ModelResponse(
            model=agent.model_primary,
            choices=[{"message": message, "finish_reason": "stop" if text else "tool_calls"}],
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )

    monkeypatch.setattr("litellm.acompletion", provider)

    async def ordinary(runner, number):
        token = everyday.set(number)
        started = time.perf_counter()
        try:
            async with asyncio.timeout(10):
                run = await runner.execute(
                    "main",
                    "Create the synthetic everyday task",
                    agent_config=agent,
                    trigger_type=TriggerType.WEBCHAT,
                    tenant_id=tenant,
                )
            assert run.status == RunStatus.COMPLETED, run.error_message
            ordinary_latencies.append((time.perf_counter() - started) * 1000)
        finally:
            everyday.reset(token)

    async def overlap(controller, runner, number):
        work = asyncio.create_task(controller.tick())
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            await ordinary(runner, number)
            assert not work.done(), "The goal wait was not concurrent with everyday work"
            release.set()
            await asyncio.wait_for(work, timeout=10)
        finally:
            release.set()
            if not work.done():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)

    runner = AgentRunner(config)
    controller = GoalController(runner, config)
    await overlap(controller, runner, 1)
    assert store.get(tenant, child["id"])["status"] == "waiting"
    before = (counts.copy(), planning_calls.copy())
    for _ in range(3):
        await controller.tick()
    assert (counts, planning_calls) == before, "Waiting goals consumed model calls"
    await ordinary(runner, 2)
    assert store.get(tenant, parent["id"])["tokens_used"] == 600

    # Simulated day change and fresh persisted provider evidence; no real calendar.
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pursuit_goals SET data=jsonb_set(data,'{wait,registered_at}',%s::jsonb) WHERE tenant_id=%s",
            (json.dumps((datetime.now(UTC) - timedelta(days=1)).isoformat()), tenant),
        )
        cur.execute(
            """INSERT INTO calendar_operations(id,tenant_id,agent_id,calendar_id,event_id,arguments,status,result)
                       VALUES (%s,%s,'main','synthetic','synthetic','{}','completed','{"verification":"verified"}')""",
            (receipt, tenant),
        )
    # New runner/controller instances must recover the durable waiting hierarchy.
    runner = AgentRunner(config)
    controller = GoalController(runner, config)
    day_two = True
    entered, release = asyncio.Event(), asyncio.Event()
    for _ in range(2):
        store.ingest_event(tenant, "same-reply", "fixture.reply", {"customer": "synthetic"})
    await overlap(controller, runner, 3)
    await get_task_registry().drain(timeout=5)
    result = store.get(tenant, parent["id"])
    assert result["status"] == "complete", result
    assert result["children"][0]["status"] == "complete"
    assert result["tokens_used"] == 1500
    assert planning_calls == {child["id"]: 2, parent["id"]: 2}
    assert counts == {
        child["id"]: 3,
        parent["id"]: 3,
        "interactive-1": 2,
        "interactive-2": 2,
        "interactive-3": 2,
    }
    before = (counts.copy(), planning_calls.copy())
    await controller.tick()
    assert (counts, planning_calls) == before, "Completion caused post-completion model work"
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM crm_tasks WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (3,)
        cur.execute(
            "SELECT count(*) FROM agent_runtime_effects WHERE tenant_id=%s AND tool_name='create_task' AND state='confirmed'",
            (tenant,),
        )
        assert cur.fetchone() == (3,)
        cur.execute(
            "SELECT goal_id,count(*) FROM pursuit_goal_attempts WHERE tenant_id=%s GROUP BY goal_id",
            (tenant,),
        )
        assert {str(goal): count for goal, count in cur.fetchall()} == {
            child["id"]: 2,
            parent["id"]: 2,
        }
        cur.execute(
            "SELECT status,count(*) FROM agent_runs WHERE tenant_id=%s GROUP BY status", (tenant,)
        )
        assert cur.fetchall() == [("completed", 7)]
    artifact = os.environ.get("ROBOTHOR_RUNTIME_MULTIDAY_ARTIFACT")
    if artifact:
        with Path(artifact).open("x") as stream:
            json.dump(
                {
                    "scope": "Private canonical storage, actual native runner/coordinator/CRM dispatcher, scripted planning and execution provider. Simulated day-old waiting state, fresh synthetic calendar receipt, new runner/controller instances; not an OS restart or live calendar/model test.",
                    "parent_status": result["status"],
                    "child_status": result["children"][0]["status"],
                    "goal_tokens": result["tokens_used"],
                    "planning_calls": sum(planning_calls.values()),
                    "goal_execution_calls": counts[parent["id"]] + counts[child["id"]],
                    "interactive_calls": sum(counts["interactive-" + str(i)] for i in range(1, 4)),
                    "ordinary_request_ms": ordinary_latencies,
                    "completed_native_runs": 7,
                    "created_tasks": 3,
                    "confirmed_task_receipts": 3,
                    "waiting_ticks_without_calls": 3,
                    "duplicate_wake_deliveries": 2,
                    "goal_attempts_each": 2,
                    "calls_after_completion": 0,
                    "manual_acceptance": False,
                },
                stream,
                indent=2,
            )
