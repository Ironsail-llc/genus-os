"""Native interactive execution competing with real goal coordinators.

Private PostgreSQL; deterministic provider and business dispatch. This measures
the runner/coordinator boundary, not the deployment queue or provider capacity.
"""

import asyncio
import json
import os
import time
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import litellm
import pytest

from bench.interactive.statistics import summary
from bench.runtime.candidates import PROMPT, SCHEMA, FixtureGateway
from robothor.engine.tests.test_runner import runner  # noqa: F401
from robothor.goals import store
from robothor.goals.controller import GoalController
from robothor.goals.model import CreateGoal, GoalUpdate
from robothor.goals.runtime import binding
from robothor.goals.tests.test_store import db, private_database  # noqa: F401


@pytest.mark.parametrize("tenants", [1, 5, 20])
@pytest.mark.timeout(300)
async def test_native_requests_compete_with_background_goals(
    request,
    db,  # noqa: F811
    sample_agent_config,
    tenants,
    monkeypatch,  # noqa: F811
):
    engine = request.getfixturevalue("runner")
    config = replace(sample_agent_config, task_protocol=False, tools_allowed=["record"])
    engine.registry.build_for_agent.return_value = [
        {"type": "function", "function": {"name": "record", "parameters": SCHEMA}}
    ]
    engine.registry.get_tool_names.return_value = ["record"]
    active = ContextVar("mixed_fixture")
    rows = []

    async def dispatch(name, args, **kwargs):
        assert name == "record"
        gateway = active.get()
        return await gateway.dispatch(gateway.tenant, **args)

    async def provider(**kwargs):
        await asyncio.sleep(0.001)
        called = active.get().writes > 0
        message = {"role": "assistant", "content": "Verified fixture receipt" if called else None}
        if not called:
            message["tool_calls"] = [
                {
                    "id": "synthetic-call",
                    "type": "function",
                    "function": {
                        "name": "record",
                        "arguments": json.dumps({"key": "report", "value": "delivered"}),
                    },
                }
            ]
        return litellm.ModelResponse(
            model="fixture",
            choices=[{"message": message, "finish_reason": "stop" if called else "tool_calls"}],
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        )

    async def execute(**kwargs):
        tenant = kwargs["tenant_id"]
        gateway = FixtureGateway(tenant)
        token = active.set(gateway)
        accepted = []
        started = time.perf_counter()

        async def status(event):
            if event.get("event") == "accepted":
                accepted.append((time.perf_counter() - started) * 1000)

        try:
            run = await engine.execute(**{**kwargs, "on_status": status})
            goal = binding.get()
            rows.append(
                {
                    "kind": "goal" if goal else "interactive",
                    "duration_ms": (time.perf_counter() - started) * 1000,
                    "ack_ms": accepted[0] if accepted else None,
                    "verified": gateway.values == {"report": "delivered"} and gateway.writes == 1,
                    "status": str(run.status),
                }
            )
            if goal:
                current = await asyncio.to_thread(store.get, tenant, goal.goal_id)
                await asyncio.to_thread(
                    store.update,
                    tenant,
                    goal.goal_id,
                    GoalUpdate(
                        action="wait",
                        version=current["version"],
                        note="Wait for next fixture event",
                        event_type="fixture.next",
                    ),
                    "main",
                )
            return run
        finally:
            active.reset(token)

    async def workload():
        tenant = str(uuid4())
        await asyncio.to_thread(store.set_enabled, tenant, True, "operator")
        goal = await asyncio.to_thread(
            store.create,
            tenant,
            CreateGoal(
                objective="Track fixture receipts",
                success_criteria=["Receipts checked"],
                kind="long",
            ),
            "operator",
        )
        controller = GoalController(
            SimpleNamespace(execute=execute),
            SimpleNamespace(tenant_id=tenant, manifest_dir="fixture"),
        )
        for index in range(30):
            if index:
                await asyncio.to_thread(
                    store.ingest_event, tenant, str(uuid4()), "fixture.next", {}
                )
            await asyncio.gather(
                controller.tick(),
                execute(
                    agent_id="test-agent", message=PROMPT, tenant_id=tenant, agent_config=config
                ),
            )
            assert (await asyncio.to_thread(store.get, tenant, goal["id"]))["status"] == "waiting"

    engine.registry.execute = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr("robothor.engine.config.load_agent_config_or_broken", lambda *args: config)
    with (
        patch("litellm.acompletion", side_effect=provider),
        patch("robothor.goals.events.capture"),
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("robothor.engine.tracking.create_step"),
        patch("robothor.engine.tracking.create_steps_batch"),
    ):
        async with asyncio.TaskGroup() as group:
            for _ in range(tenants):
                group.create_task(workload())
    report = {
        "concurrent_tenants": tenants,
        "scope": "native runner + goal coordinator; synthetic provider/business tools; private goal database; deployment queue excluded",
        "samples": rows,
        "summary": {
            kind: {
                metric: summary([r[metric] for r in rows if r["kind"] == kind])
                for metric in ("duration_ms", "ack_ms")
            }
            for kind in ("goal", "interactive")
        },
    }
    if output := os.environ.get("ROBOTHOR_RUNTIME_MIXED_OUTPUT"):
        with Path(output).open("a") as file:
            file.write(json.dumps(report) + "\n")
    assert len(rows) == tenants * 30 * 2
    assert all(r["verified"] and r["status"] == "completed" for r in rows)
    # The uncontended warm harness target is 2s. Under load, report where this
    # operating limit is exceeded and enforce the 30s simple-action deadline.
    assert report["summary"]["interactive"]["duration_ms"]["p95"] < (
        2000 if tenants == 1 else 30000
    )
    assert report["summary"]["interactive"]["ack_ms"]["p95"] < 2000
