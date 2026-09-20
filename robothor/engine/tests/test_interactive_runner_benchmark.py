"""Actual runner + PostgreSQL operations, with deterministic provider and Google.

Normal CI runs one repetition. The benchmark command supplies repetitions and an
output path. No real model, Calendar, notification, or production tracking writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import litellm
import pytest

from robothor.engine.calendar_operations import perform
from robothor.engine.performance import run_measurements
from robothor.engine.routine_request import TOOL
from robothor.engine.tests.test_calendar_operations import (  # noqa: F401
    calendar_api,
    ctx,
    google,
    store,
)
from robothor.engine.tests.test_runner import runner  # noqa: F401
from robothor.engine.tools.schemas import get_engine_schemas
from robothor.settings import get_settings


@pytest.mark.integration
@pytest.mark.parametrize("fast", [False, True])
async def test_runner_measurements(request, sample_agent_config, monkeypatch, fast, tmp_path):
    request.getfixturevalue("store")
    api = request.getfixturevalue("google")
    context = request.getfixturevalue("ctx")
    engine = request.getfixturevalue("runner")
    engine.registry.build_for_agent.return_value = [get_engine_schemas()[TOOL]]
    engine.registry.get_tool_names.return_value = [TOOL]
    sample_agent_config.tools_allowed = [TOOL]
    monkeypatch.setattr(get_settings().engine, "calendar_operations_enabled", fast)
    # The draft belongs to the requester who will confirm it: a confirmation
    # binds only to its own creator, so the benchmark has to run as that
    # requester rather than replaying a stranger's operation.
    context.tenant_id = "bench-tenant"
    context.user_id = "bench-operator"
    context.agent_id = sample_agent_config.id
    original = deepcopy(api.event)
    records = []
    repetitions = int(os.environ.get("ROBOTHOR_INTERACTIVE_BENCH_SAMPLES", "1"))
    for iteration in range(repetitions + 1):
        api.event = deepcopy(original)
        prepared = perform(
            {
                "calendar_id": "owner@example.com",
                "event_id": "meeting",
                "attendees": ["sam@example.com"],
                "draft": True,
            },
            context,
        )
        op = prepared["operation_id"]
        api.calls.clear()

        async def execute(name, args, **kwargs):
            assert name == TOOL
            return await asyncio.to_thread(perform, args, context)

        engine.registry.execute = AsyncMock(side_effect=execute)
        first = litellm.ModelResponse(
            model="test-model",
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "fixture-call",
                                "type": "function",
                                "function": {
                                    "name": TOOL,
                                    "arguments": json.dumps({"operation_id": op}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        )
        second = litellm.ModelResponse(
            model="test-model",
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": "The attendee is present; notifications requested.",
                    },
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        )
        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_step"),
            patch(
                "litellm.acompletion", new_callable=AsyncMock, side_effect=[first, second]
            ) as model,
        ):
            start = time.perf_counter()
            run = await engine.execute(
                "test-agent",
                "Go",
                agent_config=sample_agent_config,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                conversation_history=[
                    {
                        "role": "assistant",
                        "content": "Draft: add the guest.\nCalendar operation: " + op,
                    }
                ],
            )
            elapsed = (time.perf_counter() - start) * 1000
        assert run.status.value == "completed", run.error_message
        assert model.await_count == (0 if fast else 2)
        assert api.calls and [c[0] for c in api.calls] == ["GET", "PATCH", "GET"]
        assert api.event["attendees"][0] == original["attendees"][0]
        assert len(api.event["attendees"]) == 2
        assert api.event["start"] == original["start"]
        assert api.event["end"] == original["end"]
        metrics = run_measurements(run)
        if iteration == 0:
            continue  # Equal unmeasured warmup for both execution paths.
        records.append(
            {
                "harness": "optimized" if fast else "current",
                "version": "same-checkout",
                "case": "confirmed-attendee-add",
                "cohort": "fixture",
                "model": "deterministic-two-turn",
                "reasoning": "none",
                "startup": "warm",
                "machine": "same-process",
                "prompt_hash": "confirmed-draft-v1",
                "tools_hash": "native-add-v1",
                "duration_ms": elapsed,
                "harness_ms": max(0, elapsed - metrics["provider_ms"] - metrics["tool_wall_ms"]),
                "model_calls": model.await_count,
                "input_tokens": metrics["input_tokens"],
                "post_completion_tool_calls": metrics["post_completion_tool_calls"],
                "state_checks": {
                    "attendee_set": True,
                    "rsvp_preserved": True,
                    "time_preserved": True,
                    "single_write": True,
                },
            }
        )
    output = os.environ.get("ROBOTHOR_INTERACTIVE_BENCH_OUTPUT")
    if output:
        from pathlib import Path

        with Path(output).open("a") as file:
            file.writelines(json.dumps(record) + "\n" for record in records)
