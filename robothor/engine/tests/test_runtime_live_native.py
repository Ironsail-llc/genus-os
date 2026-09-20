"""Opt-in current runner timing with configured cloud models and synthetic business tools."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from bench.runtime.candidates import PROMPT, SCHEMA, FixtureGateway
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
    samples = settings.get("samples", 30)
    assert samples >= 30 or settings.get("diagnostics"), "smaller runs are diagnostics only"
    config = yaml.safe_load(Path(settings["manifest"]).read_text())["model"]
    models = list(dict.fromkeys([config["primary"], *config.get("fallbacks", [])]))
    models = [model for model in models if model.startswith("openrouter/")]
    selected = settings.get("models", models)
    assert selected and set(selected).issubset(models), "use existing configured models only"
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
    import litellm

    real_completion = litellm.acompletion

    async def completion(**kwargs):
        if settings.get("diagnostics"):
            traces.get().append(json.loads(json.dumps(kwargs.get("messages", []))))
        return await real_completion(**kwargs)

    async def dispatch(name, args, **kwargs):
        assert name == "record"
        return await current.get().dispatch("fixture", **args)

    engine.registry.execute = AsyncMock(side_effect=dispatch)
    rows = []

    async def sample(model, index):
        gateway = FixtureGateway("fixture")
        token = current.set(gateway)
        trace_token = traces.set([])
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
            run = await engine.execute("test-agent", PROMPT, agent_config=manifest)
            metrics = run_measurements(run)
            row = {
                "model": model,
                "repetition": index,
                "status": str(run.status),
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
                    error_message=run.error_message,
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
        except Exception as exc:
            row = {
                "model": model,
                "repetition": index,
                "status": "failed",
                "error_type": type(exc).__name__,
                "duration_ms": (time.perf_counter() - started) * 1000,
            }
        finally:
            traces.reset(trace_token)
            current.reset(token)
        rows.append(row)
        with Path(settings["output"]).open("a") as file:
            file.write(json.dumps(row) + "\n")

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
