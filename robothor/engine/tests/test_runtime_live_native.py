"""Opt-in current runner timing with configured models and synthetic business tools."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import yaml

from bench.runtime.candidates import PROMPT, SCHEMA, FixtureGateway
from bench.runtime.native_journal import NativeJournal, failed_sample
from robothor.engine.performance import run_measurements
from robothor.engine.tests.test_runner import runner  # noqa: F401


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.skipif(
    not os.environ.get("ROBOTHOR_RUNTIME_LIVE_NATIVE"),
    reason="explicit live benchmark invocation required",
)
async def test_configured_native_provider_cohort(request, sample_agent_config):
    engine = request.getfixturevalue("runner")
    settings = json.loads(os.environ["ROBOTHOR_RUNTIME_LIVE_NATIVE"])
    output = Path(settings["output"])
    assert not output.exists(), "use a fresh artifact; preserve previous outcomes"
    samples = settings.get("samples", 30)
    assert samples >= 30 or settings.get("diagnostics"), "smaller runs are diagnostics only"
    config = yaml.safe_load(Path(settings["manifest"]).read_text())["model"]
    models = list(dict.fromkeys([config["primary"], *config.get("fallbacks", [])]))
    selected = settings.get("models", [m for m in models if m.startswith("openrouter/")])
    assert selected and set(selected).issubset(models), "use existing configured models only"
    assert all(m.startswith(("openrouter/", "ollama_chat/")) for m in selected)
    models = selected
    schema = {
        "type": "function",
        "function": {
            "name": "record",
            "description": "Store an authorized value",
            "parameters": SCHEMA,
        },
    }
    engine.registry.build_for_agent.return_value = [schema]
    engine.registry.get_tool_names.return_value = ["record"]
    current = ContextVar("fixture_gateway")
    traces = ContextVar("provider_trace")
    counters = ContextVar("completion_counters")
    import litellm

    real_completion = litellm.acompletion

    async def completion(**kwargs):
        counters.get()["provider_attempts"] += 1
        if current.get().values.get("report") == "delivered":
            counters.get()["post_success_provider_calls"] += 1
        if settings.get("diagnostics"):
            traces.get().append(json.loads(json.dumps(kwargs.get("messages", []))))
        return await real_completion(**kwargs)

    async def dispatch(name, args, **kwargs):
        assert name == "record"
        if current.get().values.get("report") == "delivered":
            counters.get()["post_success_tool_calls"] += 1
        return await current.get().dispatch("fixture", **args)

    engine.registry.execute = AsyncMock(side_effect=dispatch)
    rows = []
    journal = NativeJournal(output)

    async def sample(model, index):
        journal.record("sample_started", model, index)
        interrupted = None
        gateway = FixtureGateway("fixture")
        token = current.set(gateway)
        trace_token = traces.set([])
        counts = {
            "provider_attempts": 0,
            "post_success_provider_calls": 0,
            "post_success_tool_calls": 0,
        }
        counter_token = counters.set(counts)
        manifest = replace(
            sample_agent_config,
            model_primary=model,
            model_fallbacks=[],
            tools_allowed=["record"],
            task_protocol=False,
            max_iterations=4,
            timeout_seconds=60,
            temperature=config.get("temperature", 0.5),
        )
        started = time.perf_counter()
        try:
            from robothor.engine.output_validation import output_validation_scope
            from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
            from robothor.engine.workflow_completion import (
                WorkflowCompletion,
                workflow_completion_scope,
            )

            def complete():
                if gateway.values.get("report") == "delivered" and gateway.writes == 1:
                    return WorkflowCompletion(
                        output="The requested value is independently verified in the fixture store."
                    )
                return None

            contract = settings.get("contract", False)
            scope = (
                workflow_completion_scope(engine.config.tenant_id, "test-agent", complete)
                if contract
                else nullcontext()
            )

            def validate(run, text):
                if gateway.values.get("report") != "delivered" or gateway.writes != 1:
                    return 'The authorized action is incomplete: the independently checked store must contain key "report" with value "delivered" exactly once.'
                return None

            validation = (
                output_validation_scope(validate)
                if settings.get("require_outcome")
                else nullcontext()
            )
            with scope, validation:
                if contract:
                    context = ExecutionContext(
                        engine.config.tenant_id,
                        "benchmark-operator",
                        str(uuid4()),
                        deadline=datetime.now(UTC) + timedelta(seconds=60),
                    )
                    result = await CurrentRuntime(engine.execute).run(
                        RunRequest(
                            context,
                            "test-agent",
                            PROMPT,
                            options={"agent_config": manifest},
                        )
                    )
                    run = result.run
                else:
                    run = await engine.execute("test-agent", PROMPT, agent_config=manifest)
            metrics = run_measurements(run)
            row = {
                "model": model,
                "repetition": index,
                "status": str(run.status),
                "error_message": run.error_message,
                "duration_ms": (time.perf_counter() - started) * 1000,
                "verified": gateway.values.get("report") == "delivered",
                "writes": gateway.writes,
                "model_calls": metrics["model_calls"],
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "engine_cost_usd": run.total_cost_usd,
            }
            if settings.get("diagnostics"):
                row.update(
                    output_text=run.output_text,
                    provider_messages=traces.get(),
                    steps=[
                        {
                            "type": str(step.step_type),
                            "tool": step.tool_name,
                            "input": step.tool_input,
                            "output": step.tool_output,
                            "error": step.error_message,
                        }
                        for step in run.steps
                    ],
                )
        except (Exception, asyncio.CancelledError) as exc:
            row = failed_sample(model, index, exc, (time.perf_counter() - started) * 1000)
            if isinstance(exc, asyncio.CancelledError):
                interrupted = exc
        finally:
            counters.reset(counter_token)
            traces.reset(trace_token)
            current.reset(token)
        row.update(
            mode="host_verified" if settings.get("contract") else "raw_native",
            require_outcome=bool(settings.get("require_outcome")),
            verified=gateway.values.get("report") == "delivered",
            writes=gateway.writes,
            dispatches=gateway.dispatches,
            **counts,
        )
        rows.append(row)
        with output.open("a") as file:
            file.write(json.dumps(row) + "\n")
            file.flush()
            os.fsync(file.fileno())
        journal.record("sample_finished", model, index, sample=row)
        if interrupted is not None:
            raise interrupted

    with (
        patch("litellm.acompletion", side_effect=completion),
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("robothor.engine.tracking.create_step"),
        patch("robothor.engine.tracking.create_steps_batch"),
    ):
        for index in range(samples):
            await asyncio.gather(*(sample(model, index) for model in models))
    assert all(
        row["status"] == "completed" and row["verified"] and row["writes"] == 1 for row in rows
    )

    if settings.get("contract"):
        assert all(
            row["post_success_provider_calls"] == row["post_success_tool_calls"] == 0
            and row["duration_ms"] <= 60000
            for row in rows
        )
