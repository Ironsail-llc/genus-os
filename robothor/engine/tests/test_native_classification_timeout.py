"""Early setup and planning expiry have recoverable outcomes in private storage."""

import asyncio
import json
import os
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import litellm
import psycopg2
import pytest

from robothor.auth.deps import AuthContext
from robothor.engine import chat_recovery
from robothor.engine.models import TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime.chat_control import request_key
from robothor.engine.runtime.classification_window import ClassificationDeadlineError
from robothor.engine.stall_watchdog import _active_watchdog_var
from robothor.engine.task_registry import get_task_registry

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "stage", ["setup", "planning", "unknown", "missing", "malformed", "complex"]
)
async def test_native_unclassified_expiry_is_recoverable(
    engine_config, sample_agent_config, monkeypatch, stage
):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, client_id = "classification-" + uuid4().hex, str(uuid4())
    auth = AuthContext(tenant_id=tenant, user_id="operator", role="owner", typ="user")
    session_key = "web:main"
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    runner = AgentRunner(replace(engine_config, tenant_id=tenant))
    # Only an explicitly simple profile receives the short classification
    # ceiling; automatic profiles must retain their normal run budget.
    sample_agent_config.difficulty_class = "" if stage == "complex" else "simple"
    sample_agent_config.task_protocol = False
    sample_agent_config.planning_enabled = True
    sample_agent_config.model_fallbacks = []
    monkeypatch.setattr("robothor.engine.runtime.action_policy.SIMPLE_ACTION_SECONDS", 0.2)
    entered = []
    watchdogs = []
    model_calls = 0

    async def stall(*args, **kwargs):
        entered.append(True)
        watchdogs.append(_active_watchdog_var.get())
        await asyncio.Event().wait()

    async def respond(**kwargs):
        nonlocal model_calls
        model_calls += 1
        assert stage != "setup", "No model before setup"
        if stage == "planning":
            return await stall()
        if model_calls == 1:
            assert "Analyze this task" in str(kwargs["messages"])
            content = (
                "invalid planning JSON"
                if stage == "unknown"
                else json.dumps(
                    {
                        "difficulty": "complex",
                        "estimated_steps": 3,
                        "plan": [],
                        "risks": [],
                        "success_criteria": "Provide a synthetic reply",
                    }
                )
            )
            if stage == "missing":
                content = "{}"
            elif stage == "malformed":
                content = '{"difficulty": []}'
        elif stage in {"unknown", "missing", "malformed"}:
            return await stall()
        else:
            entered.append(True)
            watchdogs.append(_active_watchdog_var.get())
            await asyncio.sleep(0.3)
            content = "Synthetic reply finished."
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ]
        )

    provider = AsyncMock(side_effect=respond)
    monkeypatch.setattr("litellm.acompletion", provider)
    if stage == "setup":
        monkeypatch.setattr("robothor.engine.runner.prepare_toolset", stall)
    tools = AsyncMock(side_effect=AssertionError("No business action before classification"))
    monkeypatch.setattr(runner.registry, "execute", tools)
    before = _active_watchdog_var.get()
    async with asyncio.timeout(3):
        with nullcontext() if stage == "complex" else pytest.raises(ClassificationDeadlineError):
            await runner.execute(
                sample_agent_config.id,
                "Provide a synthetic reply. " + "Synthetic context. " * 40,
                agent_config=sample_agent_config,
                trigger_type=TriggerType.WEBCHAT,
                tenant_id=tenant,
                user_id=auth.user_id,
                user_role="owner",
                correlation_id=request_key(auth, session_key, client_id),
            )
    await get_task_registry().drain(timeout=5)
    assert entered == [True]
    tools.assert_not_awaited()
    result = chat_recovery.read_outcome(auth, session_key, client_id)
    assert result["terminal"]
    assert result["state"] in ({"completed"} if stage == "complex" else {"timeout", "cancelled"})
    assert not result["verified"] and not result["effects"]
    if stage == "complex":
        assert result["text"] == "Synthetic reply finished." and model_calls == 2
    else:
        assert "before request complexity was established" in result["text"]
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_runs WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (1,)
    assert _active_watchdog_var.get() is before, "Setup cancellation leaked its watchdog"
    assert all(watchdog._task.done() for watchdog in watchdogs)
