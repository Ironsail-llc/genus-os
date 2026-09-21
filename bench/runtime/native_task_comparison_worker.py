"""Portable accepted-baseline/current native task contract; synthetic provider only."""

import asyncio
import json
import os
import socket
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import litellm
import psycopg2

from robothor.crm import dal
from robothor.engine import host_state, permissions
from robothor.engine.config import EngineConfig
from robothor.engine.llm_client import LLMClient
from robothor.engine.models import AgentConfig, DeliveryMode, TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.task_registry import get_task_registry
from robothor.events import bus
from robothor.llm import ollama


async def main():
    import robothor.engine.runner as module

    assert Path(module.__file__).resolve().is_relative_to(Path.cwd())
    dsn = os.environ["ROBOTHOR_TEST_DB_DSN"]
    assert "host=/tmp/runtime-migrated-" in dsn
    tenant = "native-compare-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    original_connect = socket.socket.connect

    def connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("Network forbidden in synthetic comparison")
        return original_connect(sock, address)

    socket.socket.connect = connect
    permissions.check_tool_permission = lambda role, scope, name, **kw: (
        None if scope == tenant and name == "create_task" else "Denied"
    )
    dal._safe_audit = lambda *a, **k: None
    bus.publish = lambda *a, **k: None
    ollama.get_embeddings_batch_async = AsyncMock(return_value=[])
    host_state.host_state_section = lambda *a, **k: "Synthetic installation health unavailable."
    calls = 0
    title = ""
    scenario = ""

    async def provider(self, messages, models, tools, on_content=None, **kwargs):
        nonlocal calls
        calls += 1
        assert calls <= 2, "Unexpected model work"
        if calls == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "create",
                        "type": "function",
                        "function": {
                            "name": "create_task",
                            "arguments": json.dumps({"title": title, "body": "Synthetic only"}),
                        },
                    }
                ],
            }
        else:
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            assert results and results[-1].get("id") and not results[-1].get("error"), results
            message = {
                "role": "assistant",
                "content": "Task creation recorded."
                + (" 17 × 19 = 323." if scenario == "compound" else ""),
            }
            if on_content:
                await on_content(message["content"])
        return litellm.ModelResponse(
            choices=[{"message": message, "finish_reason": "tool_calls" if calls == 1 else "stop"}],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

    LLMClient._call_llm = provider
    LLMClient._call_llm_streaming = provider
    with tempfile.TemporaryDirectory(prefix="native-task-comparison-") as workspace:
        root = Path(workspace)
        (root / "docs/agents").mkdir(parents=True)
        config = EngineConfig(
            tenant_id=tenant,
            workspace=root,
            manifest_dir=root / "docs/agents",
            workflow_dir=root / "docs/workflows",
            max_iterations=5,
        )
        agent = AgentConfig(
            id="main",
            name="Synthetic main",
            model_primary="openrouter/test/model",
            model_fallbacks=[],
            timeout_seconds=30,
            delivery_mode=DeliveryMode.NONE,
            tools_allowed=["create_task"],
            instruction_file="",
            bootstrap_files=[],
            task_protocol=True,
            difficulty_class="simple",
        )
        runner = AgentRunner(config)
        rows = []
        for scenario in ["standalone", "compound"]:
            for index in range(31):
                calls = 0
                title = f"{scenario} {index} {uuid4().hex}"
                prompt = f'Create one task titled "{title}" with body "Synthetic only".'
                if scenario == "compound":
                    prompt += " Also calculate 17 times 19 separately in your reply."
                started = time.perf_counter()
                row = {
                    "scenario": scenario,
                    "index": index,
                    "warmup": index == 0,
                    "verified": False,
                }
                try:
                    async with asyncio.timeout(60):
                        run = await runner.execute(
                            "main",
                            prompt,
                            agent_config=agent,
                            trigger_type=TriggerType.WEBCHAT,
                            tenant_id=tenant,
                            user_id="operator",
                            user_role="owner",
                            correlation_id=str(uuid4()),
                        )
                    row["duration_ms"] = (time.perf_counter() - started) * 1000
                    returned_calls = calls
                    await get_task_registry().drain(timeout=5)
                    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                        cur.execute(
                            "SELECT body,status FROM crm_tasks WHERE tenant_id=%s AND title=%s",
                            (tenant, title),
                        )
                        tasks = cur.fetchall()
                    row.update(
                        status=str(run.status),
                        model_calls=calls,
                        task_count=len(tasks),
                        reply=run.output_text,
                        post_return_model_calls=calls - returned_calls,
                    )
                    row["verified"] = (
                        tasks == [("Synthetic only", "TODO")]
                        and str(run.status) == "completed"
                        and calls == 2
                        and calls == returned_calls
                        and ("323" in run.output_text if scenario == "compound" else True)
                    )
                except Exception as exc:
                    row.update(
                        duration_ms=(time.perf_counter() - started) * 1000,
                        error_type=type(exc).__name__,
                        error=str(exc)[:400],
                        model_calls=calls,
                    )
                rows.append(row)
                print("NATIVE_TASK_SAMPLE " + json.dumps(row), flush=True)
        assert all(r["verified"] for r in rows), "Native comparison failures retained"


if __name__ == "__main__":
    asyncio.run(main())
