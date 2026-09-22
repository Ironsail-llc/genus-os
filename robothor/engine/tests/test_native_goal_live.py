"""Opt-in selected-profile live status/pause screening on private goal state."""

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import litellm
import psycopg2
import pytest

from bench.runtime.stream_observation import ObservedStream
from robothor.crm import dal
from robothor.engine.config import load_agent_config
from robothor.engine.models import TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import effects
from robothor.engine.task_registry import get_task_registry
from robothor.engine.tests.runtime_fixtures import seed_unfinished_work
from robothor.goals import store

pytestmark = pytest.mark.integration


@pytest.mark.timeout(4200)
async def test_selected_profile_live_goal_status_and_pause(engine_config, monkeypatch):
    settings = json.loads(os.environ.get("ROBOTHOR_RUNTIME_GOAL_LIVE", "null"))
    if not settings:
        pytest.skip("live goal screening is explicitly opt-in")
    samples = settings.get("samples", 30)
    assert samples == 30 or settings.get("diagnostic") and 1 <= samples <= 3
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    assert "host=/tmp/runtime-migrated-" in dsn
    installation = Path(settings["installation"])
    selected = load_agent_config(
        "main", installation / "docs/agents", workspace=installation, trigger_type="webchat"
    )
    assert selected is not None and not selected.auto_task
    agent = replace(selected, instruction_file="", bootstrap_files=[])
    allowed_models = {agent.model_primary, *agent.model_fallbacks}
    if agent.planning_model:
        allowed_models.add(agent.planning_model)
    original = litellm.acompletion
    calls, denied, rows = [], [], []
    tenant, goal_id, task_ids, phase = None, None, set(), None

    async def provider(**kwargs):
        assert kwargs["model"] in allowed_models
        assert len(calls) < 12, "Synthetic goal turn exceeded its provider-call limit"
        call = {
            "model": kwargs["model"],
            "stream": bool(kwargs.get("stream")),
            "provider_sort": (kwargs.get("extra_body") or {}).get("provider", {}).get("sort"),
        }
        calls.append(call)
        started = time.perf_counter()
        try:
            response = await original(**kwargs)
            return ObservedStream(response, call, started) if kwargs.get("stream") else response
        except BaseException as exc:
            call["error_type"] = type(exc).__name__
            raise
        finally:
            call["duration_ms"] = (time.perf_counter() - started) * 1000

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    monkeypatch.setattr("robothor.goals.events.capture", lambda *a, **k: None)
    monkeypatch.setattr(
        "robothor.llm.ollama.get_embeddings_batch_async", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "robothor.engine.host_state.host_state_section", lambda *a, **k: "Synthetic health"
    )
    engine = AgentRunner(engine_config)
    dispatch = engine.registry.execute
    advertised = engine.registry.build_for_agent(agent)

    async def private_dispatch(name, args, **kwargs):
        target = args.get("name") if name == "tool_call" else name
        values = args.get("arguments", {}) if name == "tool_call" else args
        permitted = kwargs.get("tenant_id") == tenant and isinstance(values, dict)
        if target in {"get_pursuit_goal", "report_pursuit_goal"}:
            permitted = permitted and values.get("goal_id") == goal_id
        elif target == "update_pursuit_goal":
            permitted = (
                permitted
                and phase == "pause"
                and values.get("goal_id") == goal_id
                and values.get("action") == "pause"
            )
        elif target == "get_task":
            permitted = permitted and values.get("id", values.get("task_id")) in task_ids
        elif target not in {
            "list_pursuit_goals",
            "list_tasks",
            "tool_search",
            "tool_describe",
            "todo_write",
        }:
            permitted = False
        if not permitted:
            denied.append(target)
            return {
                "error": "Only the requested private goal status/pause operation is authorized",
                "retryable": False,
            }
        return await dispatch(name, args, **kwargs)

    monkeypatch.setattr(engine.registry, "execute", private_dispatch)
    assert (await engine.registry.execute("send_email", {}, tenant_id="foreign")).get("error")
    with Path(settings["output"]).open("x") as output:

        def record(event):
            output.write(json.dumps(event) + "\n")
            output.flush()

        record(
            {
                "configuration": {
                    "samples": samples,
                    "primary": agent.model_primary,
                    "fallbacks": agent.model_fallbacks,
                    "temperature": agent.temperature,
                    "tools_allowed": len(agent.tools_allowed),
                    "tools_advertised": len(advertised),
                    "task_protocol": agent.task_protocol,
                    "streaming": True,
                    "scope": "Selected main execution settings, isolated instructions and canonical private CRM/goals/runs. Native streaming runner with real provider; direct admission, not HTTP/queue latency. Only requested goal status/pause and private reads can dispatch. No production goals, memory or connectors used.",
                }
            }
        )
        for index in range(samples):
            tenant = "live-goal-" + uuid4().hex
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant)
                )
            store.set_enabled(tenant, False, "operator")
            seed_unfinished_work(tenant)
            (goal,) = store.list_goals(tenant)
            goal_id = goal["id"]
            initial = store.get(tenant, goal_id)
            task_ids = {task["id"] for task in initial["tasks"]}
            history = []
            for phase, message in [
                ("status", f"Show the status of goal {goal_id}."),
                ("pause", "Pause that work."),
            ]:
                calls.clear()
                denied.clear()
                request_id = str(uuid4())
                row = {"index": index, "phase": phase, "request_id": request_id, "verified": False}
                record({"started": row})
                updates = []

                async def content(text, updates=updates):
                    updates.append(time.perf_counter())

                started = time.perf_counter()
                run = None
                try:
                    async with asyncio.timeout(65):
                        run = await engine.execute(
                            "main",
                            message,
                            agent_config=agent,
                            trigger_type=TriggerType.WEBCHAT,
                            tenant_id=tenant,
                            user_id="operator",
                            user_role="owner",
                            correlation_id=request_id,
                            conversation_history=history,
                            on_content=content,
                        )
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                row["duration_ms"] = (time.perf_counter() - started) * 1000
                returned = len(calls)
                await get_task_registry().drain(timeout=5)
                snapshot = store.get(tenant, goal_id)
                trusted_report = bool(
                    run
                    and any(
                        str(step.step_type) == "checkpoint"
                        and step.tool_name == "report_pursuit_goal"
                        and (step.tool_output or {}).get("origin") == "trusted_goal_report"
                        for step in run.steps
                    )
                )
                row.update(
                    provider_calls=list(calls),
                    post_return_model_calls=len(calls) - returned,
                    denied_tools=list(denied),
                    content_updates=len(updates),
                    run_status=str(run.status) if run else None,
                    reply=run.output_text if run else None,
                    native_estimated_cost_usd=run.total_cost_usd if run else None,
                    goal_status=snapshot["status"],
                    task_statuses=sorted(t["status"] for t in snapshot["tasks"]),
                    trusted_report=trusted_report,
                    evidence_count=len(snapshot["evidence"]),
                )
                row["verified"] = (
                    not row.get("error_type")
                    and row["run_status"] == "completed"
                    and trusted_report
                    and row["post_return_model_calls"] == 0
                    and row["task_statuses"] == ["DONE", "TODO"]
                    and row["evidence_count"] == 0
                    and snapshot["status"] == (initial["status"] if phase == "status" else "paused")
                )
                record({"sample": row})
                rows.append(row)
                history.extend(
                    [
                        {"role": "user", "content": message},
                        {
                            "role": "assistant",
                            "content": row["reply"] or "No confirmed final reply.",
                        },
                    ]
                )
        assert all(r["verified"] for r in rows), "See retained status/pause outcomes"
