"""Normal-chat goal review/control with native execution and private goal state.

The model is scripted: this checks chat/tool/state wiring and supplies a reviewable
transcript, not live language understanding or provider reliability.
"""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from litellm import ModelResponse

from bench.runtime.uat_server import seed_unfinished_work
from robothor.auth.deps import AuthContext
from robothor.engine import chat
from robothor.engine.llm_client import LLMClient
from robothor.engine.tests import test_runtime_controls
from robothor.engine.tests.test_runner import runner  # noqa: F401
from robothor.engine.tools.dispatch import ToolContext
from robothor.goals import store
from robothor.goals.tests import test_store
from robothor.goals.tools import HANDLERS, schemas
from robothor.identity import IdentityContext

db = test_store.db
private_database = test_store.private_database
runtime_db = test_runtime_controls.runtime_db


@pytest.mark.usefixtures("_mock_run_persistence")
async def test_unfinished_goal_review_and_pause_through_normal_chat(
    request,
    db,
    runtime_db,
    sample_agent_config,
    monkeypatch,
    tmp_path,  # noqa: F811
):
    engine = request.getfixturevalue("runner")
    sample_agent_config.id = "main"
    sample_agent_config.task_protocol = False
    sample_agent_config.tools_allowed = ["get_pursuit_goal", "update_pursuit_goal"]
    engine.registry.build_for_agent.return_value = [
        schemas()[name] for name in sample_agent_config.tools_allowed
    ]
    engine.registry.get_tool_names.return_value = sample_agent_config.tools_allowed
    monkeypatch.setattr(
        "robothor.engine.runner.load_agent_config_or_reason",
        lambda *args, **kwargs: (sample_agent_config, None),
    )
    store.set_enabled(db, False, "operator")
    seed_unfinished_work(db)
    (goal,) = store.list_goals(db)
    calls = []

    async def dispatch(name, arguments, **context):
        calls.append(name)
        return await HANDLERS[name](
            arguments,
            ToolContext(
                agent_id=context["agent_id"],
                run_id=context["run_id"],
                tenant_id=context["tenant_id"],
                user_id=context["user_id"],
                user_role=context["user_role"],
                identity=context["identity"],
            ),
        )

    engine.registry.execute = AsyncMock(side_effect=dispatch)

    async def provider(self, messages, models, tools, on_content=None, **kwargs):
        latest = max(i for i, message in enumerate(messages) if message["role"] == "user")
        pausing = "Pause that work" in messages[latest]["content"]
        results = [json.loads(m["content"]) for m in messages[latest:] if m["role"] == "tool"]
        name, args, text = None, None, None
        if not results:
            name, args = "get_pursuit_goal", {"goal_id": goal["id"]}
        elif pausing and len(results) == 1:
            name, args = (
                "update_pursuit_goal",
                {
                    "goal_id": goal["id"],
                    "version": results[0]["goal"]["version"],
                    "action": "pause",
                    "note": "User asked to pause in chat",
                },
            )
        elif pausing:
            assert results[-1]["goal"]["status"] == "paused"
            text = "Paused the goal. The unfinished task is still open; I haven't marked the goal complete."
        else:
            snapshot = results[-1]["goal"]
            assert snapshot["status"] == "waiting"
            assert snapshot["evidence"] == []
            assert sorted(task["status"] for task in snapshot["tasks"]) == ["DONE", "TODO"]
            text = (
                "One of the two tasks is done. ‘Check the remaining item’ is still open, "
                "so the goal isn't complete. It's waiting for that check, and no completion "
                "evidence has been recorded."
            )
        message = {"role": "assistant", "content": text}
        if name:
            message["tool_calls"] = [
                {
                    "id": f"call-{len(calls)}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                }
            ]
        elif on_content:
            await on_content(text)
        return ModelResponse(
            choices=[{"message": message, "finish_reason": "tool_calls" if name else "stop"}]
        )

    monkeypatch.setattr(LLMClient, "_call_llm_streaming", provider)
    monkeypatch.setattr(LLMClient, "_call_llm", provider)
    outbound = AsyncMock(side_effect=AssertionError("external model calls forbidden in chat UAT"))
    monkeypatch.setattr("litellm.acompletion", outbound)
    monkeypatch.setattr(chat, "_runner", engine)
    monkeypatch.setattr(chat, "_config", engine.config)
    monkeypatch.setattr(chat, "save_exchange_async", AsyncMock())
    monkeypatch.setattr(
        "robothor.memory.conversation_ingest.ingest_conversation_session", AsyncMock()
    )
    session = chat.ChatSession()
    monkeypatch.setattr(chat, "_get_session", lambda key: session)
    auth = AuthContext(user_id="operator", tenant_id=db, role="owner", typ="user")
    identity = IdentityContext(db, "webchat", "operator", True, role="owner")
    monkeypatch.setattr(chat, "_resolve_webchat_identity", lambda _: identity)
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.auth = auth
        return await call_next(request)

    app.include_router(chat.router)
    transcript = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for message in ("What's finished, and what's still left?", "Pause that work."):
            response = await client.post(
                "/chat/send", json={"message": message, "session_key": "web:main"}
            )
            assert response.status_code == 200
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            done = next(
                event for event in reversed(events) if "run_id" in event and "text" in event
            )
            assert done["status"] == "completed", done
            transcript.append({"user": message, "robothor": done["text"]})
            snapshot = store.get(db, goal["id"])
            assert snapshot["status"] == ("paused" if message.startswith("Pause") else "waiting")
            assert sorted(task["status"] for task in snapshot["tasks"]) == ["DONE", "TODO"]
    assert calls == ["get_pursuit_goal", "get_pursuit_goal", "update_pursuit_goal"]
    outbound.assert_not_called()
    assert "isn't complete" in transcript[0]["robothor"]
    assert "Paused the goal" in transcript[1]["robothor"]
    artifact = {
        "scope": "Native chat route/runner and goal handlers; scripted model, private PostgreSQL, no business connectors or scheduler.",
        "manual_acceptance": False,
        "transcript": transcript,
        "tool_calls": calls,
        "final_goal_status": snapshot["status"],
        "final_task_statuses": ["DONE", "TODO"],
    }
    output = Path(os.environ.get("ROBOTHOR_CHAT_GOAL_UAT_OUTPUT", str(tmp_path / "chat-uat.json")))
    output.write_text(json.dumps(artifact, indent=2) + "\n")
