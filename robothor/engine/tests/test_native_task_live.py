"""Opt-in configured-model task screening; only private task tools can execute."""

import asyncio
import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import litellm
import psycopg2
import pytest

from robothor.crm import dal
from robothor.engine.config import load_agent_config
from robothor.engine.model_breaker import get_model_breaker
from robothor.engine.models import TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import effects
from robothor.engine.task_registry import get_task_registry

pytestmark = pytest.mark.integration


@pytest.mark.timeout(2100)
async def test_configured_live_native_task_requests(engine_config, monkeypatch):
    settings = json.loads(os.environ.get("ROBOTHOR_RUNTIME_TASK_LIVE", "null"))
    if not settings:
        pytest.skip("live provider screening is explicitly opt-in")
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    assert "host=/tmp/runtime-migrated-" in dsn, "Disposable canonical storage required"
    samples = settings.get("samples", 30)
    assert 1 <= samples <= 30 and (samples == 30 or settings.get("diagnostic"))
    scenario = settings.get("scenario", "standalone_task")
    assert scenario in {"standalone_task", "task_and_calculation"}
    if "deferred_tools" in settings:
        assert type(settings["deferred_tools"]) is bool
        monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", "1" if settings["deferred_tools"] else "0")
    streaming = settings.get("streaming", False)
    assert type(streaming) is bool
    model_slice = settings.get("model_slice_seconds")
    isolate_timeout_health = settings.get("isolate_short_timeout_health", False)
    cloud_only = settings.get("model_slice_cloud_only", False)
    bound_stream = settings.get("bound_stream_consumption", False)
    assert type(bound_stream) is bool
    assert not bound_stream or (streaming and model_slice is not None)
    assert type(isolate_timeout_health) is bool
    assert type(cloud_only) is bool
    assert not isolate_timeout_health or model_slice is not None
    assert not cloud_only or model_slice is not None
    if model_slice is not None:
        assert type(model_slice) in {int, float} and 1 <= model_slice <= 30
        from bench.runtime.model_allowance_experiment import install

        install(
            monkeypatch,
            model_slice,
            isolate_short_timeout_health=isolate_timeout_health,
            cloud_only=cloud_only,
        )
    if bound_stream:
        from bench.runtime.stream_allowance_experiment import install as install_stream

        install_stream(monkeypatch, model_slice, cloud_only=cloud_only)
    workspace = Path(settings["installation"])
    agent = load_agent_config(
        "main", workspace / "docs/agents", workspace=workspace, trigger_type="webchat"
    )
    assert agent is not None and "create_task" in agent.tools_allowed
    assert not agent.auto_task, "This fixture requires explicit task creation"
    allowed_models = {agent.model_primary, *agent.model_fallbacks}
    if agent.planning_model:
        allowed_models.add(agent.planning_model)
    original_provider = litellm.acompletion
    calls, denied = [], []
    index, title = 0, ""

    async def provider(**kwargs):
        assert kwargs["model"] in allowed_models, "Unconfigured model refused"
        assert len(calls) < 12, "Diagnostic provider-call limit reached"
        planning = any(
            isinstance(message.get("content"), str)
            and message["content"].startswith(
                "Analyze this task and produce a JSON execution plan."
            )
            for message in kwargs.get("messages", [])
        )
        call = {
            "model": kwargs["model"],
            "stream": bool(kwargs.get("stream")),
            "phase": "planning" if planning else "execution",
            "max_tokens": kwargs.get("max_tokens"),
            "thinking_budget_tokens": (kwargs.get("thinking") or {}).get("budget_tokens"),
            "timeout_seconds": kwargs.get("timeout")
            if isinstance(kwargs.get("timeout"), int | float)
            else None,
        }
        calls.append(call)
        started = time.perf_counter()
        try:
            result = await original_provider(**kwargs)
            if kwargs.get("stream"):
                from bench.runtime.stream_observation import ObservedStream

                return ObservedStream(result, call, started)
            return result
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
        "robothor.engine.host_state.host_state_section",
        lambda *a, **k: "Engine health unavailable in this isolated test.",
    )
    # Avoid accidentally embedding the credential detector's "sk-" prefix in
    # an ordinary tenant ID ("live-task-<uuid>" did so).
    tenant = "runtime-live-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    runner = AgentRunner(replace(engine_config, tenant_id=tenant))
    advertised = runner.registry.build_for_agent(agent)
    from robothor.engine.feature_flags import deferred_tools_enabled

    original_dispatch = runner.registry.execute
    private_tools = {"create_task", "list_tasks", "get_task", "search_records", "todo_write"}
    metadata_tools = {"tool_search", "tool_describe"}

    async def private_dispatch(name, arguments, **kwargs):
        target = arguments.get("name") if name == "tool_call" else name
        target_args = arguments.get("arguments", {}) if name == "tool_call" else arguments
        if (
            target not in private_tools | metadata_tools
            or kwargs.get("tenant_id") != tenant
            or not isinstance(target_args, dict)
            or target == "create_task"
            and target_args.get("title") != title
        ):
            denied.append(name)
            return {
                "error": "This synthetic fixture only supports the requested private task operation",
                "retryable": False,
            }
        return await original_dispatch(name, arguments, **kwargs)

    monkeypatch.setattr(runner.registry, "execute", private_dispatch)
    assert (await runner.registry.execute("send_email", {}, tenant_id=tenant)).get("error")
    assert (
        await runner.registry.execute("tool_call", {"name": "send_email"}, tenant_id=tenant)
    ).get("error")
    assert (
        await runner.registry.execute("create_task", {"title": title}, tenant_id="foreign")
    ).get("error")
    results = []
    with Path(settings["output"]).open("x") as stream:
        stream.write(
            json.dumps(
                {
                    "configuration": {
                        "primary": agent.model_primary,
                        "fallbacks": agent.model_fallbacks,
                        "temperature": agent.temperature,
                        "reasoning_effort": agent.reasoning_effort,
                        "tools_allowed": len(agent.tools_allowed),
                        "tools_advertised": len(advertised),
                        "schema_characters": len(json.dumps(advertised, ensure_ascii=False)),
                        "deferred_tools": deferred_tools_enabled(),
                        "experimental_model_slice_seconds": model_slice,
                        "experimental_isolate_short_timeout_health": isolate_timeout_health,
                        "experimental_model_slice_cloud_only": cloud_only,
                        "task_protocol": agent.task_protocol,
                        "streaming": streaming,
                        "experimental_bound_stream_consumption": bound_stream,
                        "provider_call_duration_scope": "stream creation only when streaming; full request duration includes consumption",
                        "samples": samples,
                        "scenario": scenario,
                        "synthetic_tenant_prefix": "runtime-live-",
                        "scope": "Native runner, installation profile settings, isolated workspace and private CRM. Only requested task tools execute. No production instructions/memory staged; 65-second harness cutoff and 12-call diagnostic limit. Provider counts include planning. Native cost estimates are not billing. Not ASGI/browser latency or a matched baseline comparison.",
                    }
                }
            )
            + "\n"
        )
        stream.flush()
        for index in range(samples):
            title = f"Synthetic follow-up {index}-{uuid4().hex[:8]}"
            request_id = str(uuid4())
            calls.clear()
            denied.clear()
            row = {"index": index, "verified": False, "request_id": request_id}
            row["breaker_open_before"] = sorted(
                model for model in allowed_models if get_model_breaker().is_open(model)
            )
            stream.write(json.dumps({"started": row}) + "\n")
            stream.flush()
            message = (
                f'Create exactly one task with title "{title}" and body "Synthetic only". '
                "Use the task tool; do not contact anyone."
            )
            if scenario == "task_and_calculation":
                message += (
                    " Also calculate 17 times 19 and tell me the result in your reply, "
                    "separately from the task. Keep the task body exactly as requested."
                )
            content_updates = []

            async def on_content(content, updates=content_updates):
                updates.append(time.perf_counter())

            started = time.perf_counter()
            try:
                async with asyncio.timeout(65):
                    await runner.execute(
                        agent.id,
                        message,
                        agent_config=agent,
                        trigger_type=TriggerType.WEBCHAT,
                        tenant_id=tenant,
                        user_id="operator",
                        user_role="owner",
                        correlation_id=request_id,
                        on_content=on_content if streaming else None,
                    )
            except Exception as exc:
                row["error_type"] = type(exc).__name__
            row["duration_ms"] = (time.perf_counter() - started) * 1000
            row["content_update_count"] = len(content_updates)
            returned_calls = len(calls)
            await get_task_registry().drain(timeout=5)
            row["post_return_model_calls"] = len(calls) - returned_calls
            row["breaker_open_after"] = sorted(
                model for model in allowed_models if get_model_breaker().is_open(model)
            )
            row["provider_calls"] = list(calls)
            row["denied_tools"] = list(denied)
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT body,status FROM crm_tasks WHERE tenant_id=%s AND title=%s",
                    (tenant, title),
                )
                tasks = cur.fetchall()
                row["task_count"] = len(tasks)
                row["task_fields_match"] = tasks == [("Synthetic only", "TODO")]
                cur.execute(
                    "SELECT status,output_text,error_message,total_cost_usd,runtime_context FROM agent_runs WHERE tenant_id=%s AND correlation_id=%s ORDER BY started_at",
                    (tenant, request_id),
                )
                records = cur.fetchall()
                row["runs"] = [
                    {
                        "status": r[0],
                        "reply": r[1],
                        "error": r[2],
                        "estimated_cost_usd": float(r[3] or 0),
                        "deadline": (r[4] or {}).get("deadline"),
                    }
                    for r in records
                ]
                cur.execute(
                    "SELECT state,count(*) FROM agent_runtime_effects WHERE tenant_id=%s AND request_id=%s GROUP BY state",
                    (tenant, request_id),
                )
                row["effects"] = dict(cur.fetchall())
                cur.execute(
                    """SELECT s.step_type,s.tool_name,s.duration_ms,
                       s.tool_output->>'recovered',s.tool_output->>'verification_scope',
                       s.input_tokens,s.output_tokens,s.model
                       FROM agent_run_steps s JOIN agent_runs r ON r.id=s.run_id
                       WHERE r.tenant_id=%s AND r.correlation_id=%s
                       ORDER BY s.step_number""",
                    (tenant, request_id),
                )
                row["steps"] = [
                    dict(
                        zip(
                            (
                                "type",
                                "tool",
                                "duration_ms",
                                "recovered",
                                "verification_scope",
                                "input_tokens",
                                "output_tokens",
                                "model",
                            ),
                            step,
                            strict=True,
                        )
                    )
                    for step in cur.fetchall()
                ]
            row["additional_answer_verified"] = scenario == "standalone_task" or (
                len(records) == 1
                and re.search(r"\b323\b", records[0][1] or "") is not None
                and not any(
                    step["type"] == "checkpoint" and step["tool"] == "create_task"
                    for step in row["steps"]
                )
            )
            row["verified"] = (
                row["additional_answer_verified"]
                and not row.get("error_type")
                and row["task_count"] == 1
                and row["task_fields_match"]
                and len(records) == 1
                and records[0][0] == "completed"
                and row["effects"].get("confirmed") == 1
                and not any(
                    row["effects"].get(state) for state in ("prepared", "dispatching", "uncertain")
                )
                and row["post_return_model_calls"] == 0
            )
            results.append(row)
            stream.write(json.dumps({"sample": row}) + "\n")
            stream.flush()
        assert all(row["verified"] for row in results), "See retained per-sample live outcomes"
