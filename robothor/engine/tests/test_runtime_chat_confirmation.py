"""Reviewable ordinary-chat confirmation using a private operation store and fake Calendar."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from robothor.auth.deps import AuthContext
from robothor.engine import calendar_operations, chat
from robothor.engine.llm_client import LLMClient
from robothor.engine.routine_request import TOOL
from robothor.engine.tests.test_calendar_operations import (  # noqa: F401
    calendar_api,
    ctx,
    google,
    store,
)
from robothor.engine.tests.test_runner import runner  # noqa: F401
from robothor.engine.tests.test_runtime_controls import runtime_db  # noqa: F401
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.gws import HANDLERS
from robothor.engine.tools.schemas import get_engine_schemas
from robothor.goals.tests.test_store import private_database  # noqa: F401
from robothor.identity import IdentityContext
from robothor.settings import get_settings


@pytest.fixture
def db_dsn(private_database):  # noqa: F811
    return private_database


@pytest.mark.usefixtures("_mock_run_persistence")
async def test_repeated_confirmation_through_normal_chat(
    store,  # noqa: F811
    google,  # noqa: F811
    ctx,  # noqa: F811
    runtime_db,  # noqa: F811
    runner,  # noqa: F811
    sample_agent_config,
    monkeypatch,
    tmp_path,  # noqa: F811
):
    engine = runner
    sample_agent_config.id = "main"
    sample_agent_config.task_protocol = False
    sample_agent_config.tools_allowed = [TOOL]
    engine.registry.build_for_agent.return_value = [get_engine_schemas()[TOOL]]
    engine.registry.get_tool_names.return_value = [TOOL]
    monkeypatch.setattr(
        "robothor.engine.runner.load_agent_config_or_reason",
        lambda *a, **kw: (sample_agent_config, None),
    )
    monkeypatch.setattr(get_settings().engine, "calendar_operations_enabled", True)
    outbound = AsyncMock(side_effect=AssertionError("Confirmation must not call a model"))
    monkeypatch.setattr("litellm.acompletion", outbound)
    monkeypatch.setattr(LLMClient, "_call_llm", outbound)
    monkeypatch.setattr(LLMClient, "_call_llm_streaming", outbound)

    async def dispatch(name, arguments, **context):
        assert name == TOOL
        assert (context["tenant_id"], context["user_id"], context["agent_id"]) == (
            ctx.tenant_id,
            ctx.user_id,
            ctx.agent_id,
        )
        return await HANDLERS[name](
            arguments,
            ToolContext(
                **{
                    key: context[key]
                    for key in (
                        "tenant_id",
                        "user_id",
                        "user_role",
                        "agent_id",
                        "run_id",
                        "identity",
                    )
                }
            ),
        )

    engine.registry.execute = AsyncMock(side_effect=dispatch)
    prepared = calendar_operations.perform(
        {
            "calendar_id": "owner@example.com",
            "event_id": "meeting",
            "attendees": ["sam@example.com"],
            "draft": True,
        },
        ctx,
    )
    assert prepared["status"] == "draft"
    operation_id = prepared["operation_id"]
    session = chat.ChatSession()
    session.history.append(
        {"role": "assistant", "content": "Ready to add Sam.\nCalendar operation: " + operation_id}
    )
    monkeypatch.setattr(chat, "_runner", engine)
    monkeypatch.setattr(chat, "_config", engine.config)
    monkeypatch.setattr(chat, "_get_session", lambda key: session)
    monkeypatch.setattr(chat, "save_exchange_async", AsyncMock())
    monkeypatch.setattr(
        "robothor.memory.conversation_ingest.ingest_conversation_session", AsyncMock()
    )
    auth = AuthContext(user_id=ctx.user_id, tenant_id=ctx.tenant_id, role="owner", typ="user")
    identity = IdentityContext(ctx.tenant_id, "webchat", ctx.user_id, True, role="owner")
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
        for message in ["Go ahead", "Yes"]:
            response = await client.post(
                "/chat/send",
                json={"message": message, "session_key": "web:main", "request_id": str(uuid4())},
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
            assert sum(call[0] == "PATCH" for call in google.calls) == 1
    outbound.assert_not_awaited()
    assert "Added sam@example.com" in transcript[0]["robothor"]
    assert "completed previously" in transcript[1]["robothor"]
    assert "No new calendar changes or invitations" in transcript[1]["robothor"]
    assert [call[0] for call in google.calls] == ["GET", "GET", "PATCH", "GET"]
    assert engine.registry.execute.await_count == 2
    receipt = calendar_operations.load_operation(
        operation_id, ctx.tenant_id, ctx.user_id, ctx.agent_id
    )
    assert receipt["status"] == "completed"
    report = {
        "scope": "Native chat and runner admission, real calendar handler and private operation receipts; fake Calendar transport, mocked run/history persistence. No model calls or production writes.",
        "manual_acceptance": False,
        "transcript": transcript,
        "model_calls": 0,
        "calendar_writes": 1,
        "operation_status": receipt["status"],
    }
    output = Path(
        os.environ.get(
            "ROBOTHOR_CHAT_CONFIRMATION_UAT_OUTPUT", str(tmp_path / "confirmation-uat.json")
        )
    )
    assert not output.exists(), "Preserve prior acceptance evidence"
    output.write_text(json.dumps(report, indent=2) + "\n")
