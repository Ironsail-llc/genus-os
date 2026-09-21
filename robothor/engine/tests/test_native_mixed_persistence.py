"""Opt-in mixed native load with canonical storage and isolated business writes."""

import asyncio
import json
import os
import time
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import litellm
import psycopg2
import pytest
from bench.runtime.test_profile_load import batch_summary

from robothor.crm import dal
from robothor.engine.config import load_agent_config
from robothor.engine.models import TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import effects
from robothor.engine.runtime.current import active_context
from robothor.engine.task_registry import get_task_registry
from robothor.goals import store
from robothor.goals.controller import GoalController
from robothor.goals.model import CreateGoal
from robothor.goals.runtime import binding

pytestmark = pytest.mark.integration


@pytest.mark.timeout(600)
@pytest.mark.parametrize("tenants", [1, 5, 20])
async def test_persisted_chat_competes_with_goal_execution(engine_config, monkeypatch, tenants):
    settings = json.loads(os.environ.get("ROBOTHOR_RUNTIME_NATIVE_MIXED", "null"))
    if not settings:
        pytest.skip("native mixed screening is explicitly opt-in")
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    assert "host=/tmp/runtime-migrated-" in dsn
    installation = Path(settings["installation"])
    selected = load_agent_config(
        "main", installation / "docs/agents", workspace=installation, trigger_type="webchat"
    )
    assert selected is not None and not selected.auto_task
    agent = replace(selected, instruction_file="", bootstrap_files=[])
    monkeypatch.setattr("robothor.engine.config.load_agent_config_or_broken", lambda *a: agent)
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
    ordinary = ContextVar("mixed_persisted_ordinary", default=None)
    calls, starts, rows, targets = {}, {}, [], {}
    goal_requests, goal_rows = {}, []
    entered, release = {}, asyncio.Event()
    repetition = 0

    async def provider(**kwargs):
        ctx, goal = active_context.get(), binding.get()
        assert ctx is not None and ctx.tenant_id in targets
        key = ctx.request_id
        calls.setdefault(key, []).append(
            {
                "model": kwargs["model"],
                "provider_sort": (kwargs.get("extra_body") or {}).get("provider", {}).get("sort"),
                "planning": any(
                    str(m.get("content", "")).startswith(
                        "Analyze this task and produce a JSON execution plan."
                    )
                    for m in kwargs.get("messages", [])
                ),
            }
        )
        call = calls[key][-1]
        assert len(calls[key]) <= 4
        assert kwargs["model"] == agent.model_primary
        if call["planning"]:
            assert goal is not None
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "difficulty": "complex",
                        "estimated_steps": 1,
                        "plan": [
                            {
                                "step": 1,
                                "action": "Wait for next synthetic event",
                                "tool": "update_pursuit_goal",
                            }
                        ],
                        "risks": [],
                        "success_criteria": "Review the next synthetic event",
                    }
                ),
            }
        elif goal:
            goal_requests[(ctx.tenant_id, repetition)] = key
            assert call["provider_sort"] is None, "Interactive routing leaked into a goal"
            starts.setdefault(key, time.perf_counter())
            entered[ctx.tenant_id].set()
            await asyncio.wait_for(release.wait(), timeout=30)
            state = store.control(ctx.tenant_id, goal.goal_id)
            tool, arguments = (
                "update_pursuit_goal",
                {
                    "goal_id": goal.goal_id,
                    "version": state["version"],
                    "action": "wait",
                    "note": "Await next synthetic event",
                    "event_type": "fixture.next",
                },
            )
            message = tool_message(tool, arguments)
        else:
            title = ordinary.get()
            assert title and call["provider_sort"] == "throughput"
            if len(calls[key]) == 1:
                message = tool_message(
                    "create_task", {"title": title, "body": "Synthetic mixed work"}
                )
            else:
                assert len(calls[key]) == 2
                message = {
                    "role": "assistant",
                    "content": "The requested task was created. 17 × 19 = 323.",
                }
        await asyncio.sleep(0.01)
        return litellm.ModelResponse(
            model=agent.model_primary,
            choices=[
                {
                    "message": message,
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                }
            ],
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )

    def tool_message(name, arguments):
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": str(uuid4()),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }

    monkeypatch.setattr("litellm.acompletion", provider)
    fleets = []
    for _ in range(tenants):
        tenant = "mixed-persisted-" + uuid4().hex
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
        store.set_enabled(tenant, True, "operator")
        goal = store.create(
            tenant,
            CreateGoal(
                objective="Review synthetic events",
                kind="long",
                success_criteria=["Next event reviewed"],
                token_budget=10000000,
            ),
            "operator",
        )
        targets[tenant] = goal["id"]
        config = replace(engine_config, tenant_id=tenant)
        engine = AgentRunner(config)
        fleets.append((tenant, engine, GoalController(engine, config)))

    registry = fleets[0][1].registry
    assert all(engine.registry is registry for _, engine, _ in fleets)
    dispatch = registry.execute

    async def private_dispatch(name, args, **kwargs):
        context = active_context.get()
        assert context is not None and context.tenant_id in targets
        assert kwargs.get("tenant_id") == context.tenant_id
        if name == "create_task":
            assert ordinary.get() and ordinary.get() == args.get("title")
        else:
            assert (
                name == "update_pursuit_goal" and args.get("goal_id") == targets[context.tenant_id]
            )
            assert args.get("action") == "wait" and binding.get() is not None
        return await dispatch(name, args, **kwargs)

    monkeypatch.setattr(registry, "execute", private_dispatch)

    output = Path(settings["output"] + f"-{tenants}.jsonl")
    with output.open("x") as journal:

        def record(value):
            journal.write(json.dumps(value) + "\n")
            journal.flush()

        record(
            {
                "configuration": {
                    "tenants": tenants,
                    "rounds": 30,
                    "primary": agent.model_primary,
                    "tools_allowed": len(agent.tools_allowed),
                    "provider": "synthetic transport",
                    "scope": "Canonical private CRM/runs/goals, native non-streaming execution, selected main profile with isolated instructions. Direct runner/controller; no deployment queue, real provider capacity or billing.",
                }
            }
        )

        async def chat(tenant, engine):
            identifier, title = str(uuid4()), "Synthetic mixed " + uuid4().hex
            token = ordinary.set(title)
            started, accepted = time.perf_counter(), []
            row = {
                "kind": "interactive",
                "repetition": repetition,
                "tenant": tenant,
                "request_id": identifier,
                "verified": False,
            }
            record({"started": row})

            async def status(event):
                if event.get("event") == "accepted":
                    accepted.append((time.perf_counter() - started) * 1000)

            try:
                run = await engine.execute(
                    "main",
                    f'Create task "{title}" with body "Synthetic mixed work". Also calculate 17 times 19.',
                    agent_config=agent,
                    trigger_type=TriggerType.WEBCHAT,
                    tenant_id=tenant,
                    user_id="operator",
                    user_role="owner",
                    correlation_id=identifier,
                    on_status=status,
                )
                row.update(
                    duration_ms=(time.perf_counter() - started) * 1000,
                    ack_ms=accepted[0] if accepted else None,
                    model_calls=len(calls[identifier]),
                    status=str(run.status),
                )
                with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT tenant_id,body,status FROM crm_tasks WHERE title=%s", (title,)
                    )
                    tasks = cur.fetchall()
                    cur.execute(
                        "SELECT state,count(*) FROM agent_runtime_effects WHERE tenant_id=%s AND request_id=%s GROUP BY state",
                        (tenant, identifier),
                    )
                    action_states = dict(cur.fetchall())
                row["verified"] = (
                    tasks == [(tenant, "Synthetic mixed work", "TODO")]
                    and str(run.status) == "completed"
                    and "323" in run.output_text
                    and row["model_calls"] == 2
                    and action_states == {"confirmed": 1}
                )
            except Exception as exc:
                row.update(
                    duration_ms=(time.perf_counter() - started) * 1000,
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )
            finally:
                ordinary.reset(token)
                rows.append(row)
                record({"sample": row})

        for repetition in range(30):
            entered = {tenant: asyncio.Event() for tenant, *_ in fleets}
            release = asyncio.Event()
            if repetition:
                for tenant, *_ in fleets:
                    store.ingest_event(tenant, str(uuid4()), "fixture.next", {})
            for tenant, *_ in fleets:
                record({"goal_started": {"tenant": tenant, "repetition": repetition}})
            workers = [asyncio.create_task(controller.tick()) for _, _, controller in fleets]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(e.wait() for e in entered.values())), timeout=30
                )
                interactive = asyncio.gather(
                    *(chat(tenant, engine) for tenant, engine, _ in fleets)
                )
                release.set()
                await asyncio.wait_for(asyncio.gather(interactive, *workers), timeout=65)
            except Exception as exc:
                record(
                    {
                        "round_failure": {
                            "repetition": repetition,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:300],
                        }
                    }
                )
                raise
            finally:
                release.set()
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            for tenant, *_ in fleets:
                state = store.get(tenant, targets[tenant])
                request_id = goal_requests[(tenant, repetition)]
                with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT status FROM agent_runs WHERE tenant_id=%s AND correlation_id=%s",
                        (tenant, request_id),
                    )
                    saved_runs = [r[0] for r in cur.fetchall()]
                row = {
                    "tenant": tenant,
                    "repetition": repetition,
                    "request_id": request_id,
                    "status": state["status"],
                    "run_statuses": saved_runs,
                    "model_calls": len(calls[request_id]),
                    "tokens_used": state["tokens_used"],
                    "verified": state["status"] == "waiting" and saved_runs == ["completed"],
                }
                goal_rows.append(row)
                record({"goal_sample": row})
            assert all(row["verified"] for row in goal_rows[-tenants:])
            await get_task_registry().drain(timeout=5)
            assert all(row["model_calls"] == len(calls[row["request_id"]]) for row in rows)
        report = {
            "tenants": tenants,
            "verified": sum(r["verified"] for r in rows),
            "samples": len(rows),
            "goal_samples": len(goal_rows),
            "verified_goal_turns": sum(r["verified"] for r in goal_rows),
            "interactive_ms": batch_summary(rows, "duration_ms"),
            "ack_ms": batch_summary(rows, "ack_ms"),
        }
        record({"report": report})
        assert len(rows) == tenants * 30 and all(r["verified"] for r in rows)
        assert report["interactive_ms"]["p95"] < (2000 if tenants == 1 else 30000)
        assert report["ack_ms"]["p95"] < 2000
