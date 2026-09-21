"""Fresh native admissions cannot replay an unresolved write from an earlier run."""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg2
import pytest
from litellm import ModelResponse

from robothor.engine.models import TriggerType
from robothor.engine.output_validation import output_validation_scope
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest, effects
from robothor.engine.task_registry import get_task_registry
from robothor.engine.tools import dispatch
from robothor.engine.workflow_completion import WorkflowCompletion, workflow_completion_scope

pytestmark = pytest.mark.integration


def response(model, name):
    return ModelResponse(
        model=model,
        choices=[
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-" + name,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(
                                    {"title": "Synthetic note", "body": "once"}
                                ),
                            },
                        }
                    ],
                },
            }
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


async def test_new_native_run_recovers_original_effect_without_another_write(
    engine_config, sample_agent_config, monkeypatch
):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant = "effect-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    config = replace(
        sample_agent_config,
        id="main",
        task_protocol=False,
        max_iterations=1,
        model_fallbacks=[],
        tools_allowed=["create_note", "get_note"],
    )
    host = replace(engine_config, tenant_id=tenant)
    original_context = ExecutionContext(
        tenant, "service:main", str(uuid4()), deadline=datetime.now(UTC) + timedelta(seconds=30)
    )
    writes, reads, provider_calls = [], [], []
    effect_id = []
    phase = ["original"]
    recovery_calls = []
    original_handlers = dispatch._get_handlers()

    async def write(args, ctx):
        writes.append(dict(args))
        raise httpx.ReadTimeout("synthetic provider lost its response after committing")

    async def read(args, ctx):
        reads.append(dict(args))
        assert len(writes) == 1
        assert effects.resolve(
            original_context,
            effect_id[0],
            lambda row: effects.Verification(
                "applied", True, "synthetic-provider:note-1", {"id": "note-1"}
            ),
        )
        return {"id": "note-1", "body": "once"}

    monkeypatch.setattr(
        dispatch,
        "_get_handlers",
        lambda: {**original_handlers, "create_note": write, "get_note": read},
    )

    async def provider(**kwargs):
        assert not reads, "No model calls after verified recovery"
        provider_calls.append(kwargs)
        if phase[0] == "original":
            if writes:
                raise ValueError("Synthetic model connection unavailable after the uncertain write")
            return response(config.model_primary, "create_note")
        recovery_calls.append(kwargs)
        return response(
            config.model_primary, "create_note" if len(recovery_calls) == 1 else "get_note"
        )

    monkeypatch.setattr("litellm.acompletion", provider)
    with output_validation_scope(lambda run, text: "Readback has not verified the effect"):
        original = await CurrentRuntime(AgentRunner(host).execute).run(
            RunRequest(
                original_context,
                "main",
                "Create the requested note once.",
                options={"agent_config": config, "trigger_type": TriggerType.EVENT},
            )
        )
    await get_task_registry().drain(timeout=5)
    assert original.run.status == "failed" and len(writes) == 1
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,state FROM agent_runtime_effects WHERE tenant_id=%s", (tenant,))
        identifier, state = cur.fetchone()
        assert state == "uncertain"
        effect_id.append(str(identifier))
    # A fresh runner, request identity and run have no original in-memory state.
    context = replace(original_context, request_id=str(uuid4()))
    phase[0] = "recovery"

    def verified():
        return (
            len(reads) == 1 and effects.read(original_context, effect_id[0])["state"] == "confirmed"
        )

    with (
        workflow_completion_scope(
            tenant,
            "main",
            lambda: (
                WorkflowCompletion(
                    output="The note was saved once; its original outcome is verified."
                )
                if verified()
                else None
            ),
        ),
        output_validation_scope(
            lambda run, text: None if verified() else "Missing verified recovery"
        ),
    ):
        recovered = await CurrentRuntime(AgentRunner(host).execute).run(
            RunRequest(
                context,
                "main",
                "Recover the original note action without duplicating it.",
                options={
                    "agent_config": replace(config, max_iterations=3),
                    "trigger_type": TriggerType.EVENT,
                },
            )
        )
    await get_task_registry().drain(timeout=5)
    assert recovered.run.status == "completed", recovered.run.error_message
    assert (
        len(writes) == 1
        and len(reads) == 1
        and len(provider_calls) == 4
        and len(recovery_calls) == 2
    )
    denied = next(step for step in recovered.run.steps if step.tool_name == "create_note")
    assert denied.tool_output["effect_id"] == effect_id[0]
    assert denied.tool_output["outcome_unknown"]
    assert "saved once" in recovered.run.output_text
