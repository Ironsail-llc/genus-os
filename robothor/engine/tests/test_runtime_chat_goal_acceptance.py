"""Normal-chat goal review/control with native execution and private goal state.

The model is scripted: this checks chat/tool/state wiring and supplies a reviewable
transcript, not live language understanding or provider reliability.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from litellm import ModelResponse

from bench.runtime.uat_server import seed_unfinished_work
from robothor.auth.deps import AuthContext
from robothor.engine import chat
from robothor.engine.llm_client import LLMClient
from robothor.engine.runtime.contracts import ExecutionContext
from robothor.engine.runtime.current import active_context
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
@pytest.mark.timeout(150)
@pytest.mark.parametrize("hierarchy", [False, True], ids=["single-goal", "parent-child"])
async def test_unfinished_goal_review_and_pause_through_normal_chat(
    request,
    hierarchy,
    db,
    runtime_db,
    sample_agent_config,
    monkeypatch,
    tmp_path,  # noqa: F811
):
    engine = request.getfixturevalue("runner")
    # Private run rows are test activity, not the running installation's health.
    monkeypatch.setattr(
        "robothor.engine.host_state.host_state_section",
        lambda *args, **kwargs: "Engine health: unavailable in this isolated test.",
    )
    live = json.loads(os.environ.get("ROBOTHOR_RUNTIME_CHAT_LIVE", "null"))
    if live and hierarchy:
        pytest.skip("hierarchy acceptance uses synthetic provider only")
    live_output = None
    if live:
        import yaml

        configuration = yaml.safe_load(Path(live["manifest"]).read_text())["model"]
        sample_agent_config.model_primary = configuration["primary"]
        sample_agent_config.model_fallbacks = list(configuration.get("fallbacks", []))
        sample_agent_config.temperature = configuration.get("temperature", 0.5)
        sample_agent_config.timeout_seconds = 60
        live_output = Path(live["output"])
        assert not live_output.exists(), "preserve every live diagnostic, including failures"
    sample_agent_config.id = "main"
    sample_agent_config.task_protocol = False
    sample_agent_config.tools_allowed = [
        "list_pursuit_goals",
        "get_pursuit_goal",
        "update_pursuit_goal",
    ]
    engine.registry.build_for_agent.return_value = [
        schemas()[name] for name in sample_agent_config.tools_allowed
    ]
    engine.registry.get_tool_names.return_value = sample_agent_config.tools_allowed
    monkeypatch.setattr(
        "robothor.engine.runner.load_agent_config_or_reason",
        lambda *args, **kwargs: (sample_agent_config, None),
    )
    store.set_enabled(db, False, "operator")
    seed_unfinished_work(db, kind="long" if hierarchy else "short")
    (goal,) = store.list_goals(db)
    child = test_store.create(db, parent_goal_id=goal["id"]) if hierarchy else None
    controls = {"Pause that work.": ("pause", goal["id"])}
    if hierarchy:
        controls.update(
            {
                "Keep the child paused separately.": ("pause", child["id"]),
                "Resume the parent, keeping that child paused.": ("resume", goal["id"]),
            }
        )
    calls = []
    active_message = ""

    def open_tasks(*, tenant_id, exclude_resolved=True, **kwargs):
        assert tenant_id == db
        return [
            task
            for task in store.get(db, goal["id"])["tasks"]
            if not exclude_resolved or task["status"] != "DONE"
        ]

    monkeypatch.setattr("robothor.crm.dal.list_tasks", open_tasks)

    async def dispatch(name, arguments, **context):
        assert name in sample_agent_config.tools_allowed
        if name == "update_pursuit_goal":
            assert active_message in controls
            assert (arguments.get("action"), arguments.get("goal_id")) == controls[active_message]
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
        command = controls.get(active_message)
        target = command[1] if command else goal["id"]
        results = [json.loads(m["content"]) for m in messages[latest:] if m["role"] == "tool"]
        name, args, text = None, None, None
        if not results:
            name, args = "get_pursuit_goal", {"goal_id": target}
        elif command and len(results) == 1:
            name, args = (
                "update_pursuit_goal",
                {
                    "goal_id": target,
                    "version": results[0]["goal"]["version"],
                    "action": command[0],
                    "note": active_message,
                },
            )
        elif command and hierarchy and len(results) == 2:
            name, args = "get_pursuit_goal", {"goal_id": target}
        elif command:
            observed = results[-1]["goal"]
            assert observed["status"] == ("queued" if command[0] == "resume" else "paused")
            if hierarchy and target == child["id"]:
                assert "paused_by_parent" not in observed
                text = "Kept the child goal paused separately. Resuming its parent will not resume this child."
            elif hierarchy:
                assert observed["children"][0]["id"] == child["id"]
                assert observed["children"][0]["status"] == "paused"
                assert sorted(task["status"] for task in observed["tasks"]) == ["DONE", "TODO"]
                text = "Paused the goal and its unfinished child goal. The remaining task is still open; neither goal is complete."
                if command[0] == "resume":
                    assert results[-1]["execution_enabled"] is False
                    text = "The parent is queued to continue, and the child remains paused as requested. Goal execution is disabled, so no background work has started."
            else:
                text = "Paused the goal. The unfinished task is still open; I haven't marked the goal complete."
        else:
            snapshot = results[-1]["goal"]
            assert results[-1]["execution_enabled"] is False
            assert snapshot["status"] == "waiting"
            assert snapshot["evidence"] == []
            if hierarchy:
                assert snapshot["children"][0]["status"] == "queued"
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

    if live:
        import litellm

        original = litellm.acompletion
        allowed = {sample_agent_config.model_primary, *sample_agent_config.model_fallbacks}

        async def bounded_provider(**kwargs):
            assert kwargs["model"] in allowed, "only the configured model chain is authorized"
            assert outbound.call_count <= 12, "live diagnostic provider-call bound exceeded"
            return await original(**kwargs)

        outbound = AsyncMock(side_effect=bounded_provider)
    else:
        monkeypatch.setattr(LLMClient, "_call_llm_streaming", provider)
        monkeypatch.setattr(LLMClient, "_call_llm", provider)
        outbound = AsyncMock(
            side_effect=AssertionError("external model calls forbidden in chat UAT")
        )
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
        status_question = (live or {}).get(
            "status_question", "What's finished, and what's still left?"
        )
        for message in (status_question, *controls):
            active_message = message
            token = active_context.set(
                ExecutionContext(
                    db, "operator", str(uuid4()), deadline=datetime.now(UTC) + timedelta(seconds=60)
                )
            )
            try:
                response = await client.post(
                    "/chat/send", json={"message": message, "session_key": "web:main"}
                )
            finally:
                active_context.reset(token)
            assert response.status_code == 200
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            done = next(
                (event for event in reversed(events) if "run_id" in event and "text" in event),
                {
                    "status": "failed",
                    "text": next(
                        (e["error"] for e in reversed(events) if "error" in e),
                        "Chat ended without terminal run metadata",
                    ),
                },
            )
            transcript.append({"user": message, "robothor": done["text"], "run": done})
            snapshot = store.get(db, goal["id"])
            if live_output:
                live_output.write_text(
                    json.dumps(
                        {
                            "scope": "Two-turn diagnostic through native chat; configured model chain, isolated minimal manifest/workspace, private goal data; not a performance cohort or full production configuration.",
                            "configured_primary": sample_agent_config.model_primary,
                            "manual_acceptance": False,
                            "transcript": transcript,
                            "tool_calls": calls,
                            "provider_attempts": outbound.call_count,
                            "goal_status": snapshot["status"],
                            "execution_enabled": store.enabled(db),
                            "task_statuses": sorted(t["status"] for t in snapshot["tasks"]),
                        },
                        indent=2,
                    )
                    + "\n"
                )
            assert done["status"] == "completed", done
            assert done["duration_ms"] <= 60_000, "completed response exceeded the host deadline"
            expected_parent = (
                "waiting"
                if message == status_question
                else ("queued" if controls[message][0] == "resume" else "paused")
            )
            assert snapshot["status"] == expected_parent
            assert sorted(task["status"] for task in snapshot["tasks"]) == ["DONE", "TODO"]
            if hierarchy:
                child_state = store.get(db, child["id"])
                assert child_state["status"] == (
                    "queued" if message == status_question else "paused"
                )
                assert child_state["evidence"] == []
    if live:
        assert "get_pursuit_goal" in calls and calls.count("update_pursuit_goal") == 1
        assert outbound.call_count > 0
    else:
        assert calls == ["get_pursuit_goal"] + (
            ["get_pursuit_goal", "update_pursuit_goal", "get_pursuit_goal"] * 3
            if hierarchy
            else ["get_pursuit_goal", "update_pursuit_goal"]
        )
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
        "final_child_status": store.get(db, child["id"])["status"] if child else None,
    }
    output = Path(os.environ.get("ROBOTHOR_CHAT_GOAL_UAT_OUTPUT", str(tmp_path / "chat-uat.json")))
    if hierarchy:
        output = output.with_name(output.stem + "-hierarchy" + output.suffix)
    if not live:
        output.write_text(json.dumps(artifact, indent=2) + "\n")
