"""Who may recall a private conversation in a tool call: decided by the run.

A private conversation segment is readable only when the person it belongs to
is the one being served: an interactive run, typed by that person, in their
own one-to-one conversation. A run that merely executes AS a member — a
scheduled, hook, workflow or sub-agent run, a workspace (room) run — is not
a reader, and neither is any run whose output goes to a shared audience (a
workspace, a group chat). The decision is made once per call from the run's
trigger, its conversation and its resolved identity, and travels on the tool
context as ``private_reader``. When unsure, there is no reader.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, TriggerType
from robothor.engine.private_recall import PrivateReader, private_reader_for_run
from robothor.identity.context import IdentityContext
from robothor.rooms.run_context import RoomRun, room_run_scope

MEMBER = "0c9d8e7f-6a5b-4c3d-9e2f-1a0b9c8d7e6f"
PERSON = "8a6a5b0e-2f0c-4b1d-9f7e-1c2d3e4f5a6b"
ROOM = "3f1c2a7e-0d4b-4c1e-9a55-6b0f2d9e8c11"
OWN = f"agent:main:user:{MEMBER}"


def _identity(role: str = "member", *, verified: bool = True) -> IdentityContext:
    return IdentityContext(
        tenant_id="test-tenant",
        channel="webchat",
        identifier="agent@example.com",
        verified=verified,
        role=role,
        person_id=PERSON,
    )


@pytest.mark.parametrize(
    ("trigger", "detail", "user_id", "identity", "expected"),
    [
        # The member, typing in their own webchat conversation.
        (TriggerType.WEBCHAT, f"webchat:{OWN}", MEMBER, _identity(), PrivateReader(PERSON, MEMBER)),
        (TriggerType.WEBCHAT, f"plan:{OWN}", MEMBER, _identity(), PrivateReader(PERSON, MEMBER)),
        # The operator, typing in the main session.
        (
            TriggerType.WEBCHAT,
            "webchat:agent:main:primary",
            MEMBER,
            _identity("owner"),
            PrivateReader(PERSON, MEMBER),
        ),
        # A member in the main session is not its owner.
        (TriggerType.WEBCHAT, "webchat:agent:main:primary", MEMBER, _identity(), None),
        # Someone else's conversation, or a workspace's.
        (TriggerType.WEBCHAT, "webchat:agent:main:user:someone-else", MEMBER, _identity(), None),
        (TriggerType.WEBCHAT, f"webchat:agent:main:room:{ROOM}", MEMBER, _identity(), None),
        # No conversation named, no identity, or an unverified one.
        (TriggerType.WEBCHAT, None, MEMBER, _identity(), None),
        (TriggerType.WEBCHAT, f"webchat:{OWN}", MEMBER, None, None),
        (TriggerType.WEBCHAT, f"webchat:{OWN}", MEMBER, _identity(verified=False), None),
        # A Telegram direct message is one-to-one; a group (negative id) is not.
        (
            TriggerType.TELEGRAM,
            "chat:4242|sender:Alice",
            MEMBER,
            _identity(),
            PrivateReader(PERSON, None),
        ),
        (TriggerType.TELEGRAM, "chat:-1001234|sender:Alice", MEMBER, _identity(), None),
        (TriggerType.TELEGRAM, "chat:abc", MEMBER, _identity(), None),
        # Channels whose audience the engine cannot tell: no reader.
        (TriggerType.SLACK, None, MEMBER, _identity(), None),
        (TriggerType.CHANNEL, "channel:teams:thread-1", MEMBER, _identity(), None),
        (TriggerType.IDE, None, MEMBER, _identity(), None),
        (TriggerType.MANUAL, None, MEMBER, _identity(), None),
        # Unattended runs, whoever they run as.
        (TriggerType.CRON, f"webchat:{OWN}", MEMBER, _identity(), None),
        (TriggerType.HOOK, None, MEMBER, _identity(), None),
        (TriggerType.WORKFLOW, None, MEMBER, _identity(), None),
        (TriggerType.SUB_AGENT, f"webchat:{OWN}", MEMBER, _identity(), None),
        (TriggerType.EVENT, None, MEMBER, _identity(), None),
        (TriggerType.CHANNEL_EVENT, None, MEMBER, _identity(), None),
        ("", None, MEMBER, _identity(), None),
    ],
)
def test_who_may_read_private_conversation(trigger, detail, user_id, identity, expected):
    assert (
        private_reader_for_run(
            trigger_type=trigger, trigger_detail=detail, user_id=user_id, identity=identity
        )
        == expected
    )


def test_a_run_inside_a_workspace_never_reads_private_conversation():
    run = RoomRun(tenant_id="test-tenant", room_id=ROOM, session_key=f"agent:main:room:{ROOM}")
    with room_run_scope(run):
        assert (
            private_reader_for_run(
                trigger_type=TriggerType.WEBCHAT,
                trigger_detail=f"webchat:{OWN}",
                user_id=MEMBER,
                identity=_identity(),
            )
            is None
        )


# ── the reader reaches the tool through the registry ───────────────────────


async def test_the_registry_puts_the_private_reader_on_the_tool_context():
    from robothor.engine.tools import dispatch
    from robothor.engine.tools.registry import get_registry

    seen: list[Any] = []

    async def probe(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        seen.append(ctx)
        return {"ok": True}

    reader = PrivateReader(PERSON, MEMBER)
    with (
        patch.object(dispatch, "_get_handlers", return_value={"probe": probe}),
        patch.object(dispatch, "_refuse_before_handler", AsyncMock(return_value=None)),
        patch.object(dispatch, "_runtime_denial", AsyncMock(return_value=None)),
    ):
        await get_registry().execute("probe", {}, timeout=0, private_reader=reader)
        await get_registry().execute("probe", {}, timeout=0)

    assert [c.private_reader for c in seen] == [reader, None]


# ── a real run decides it from its own trigger ─────────────────────────────


def _tool_call(name: str, args: dict | None = None):
    tc = MagicMock()
    tc.id = "call_1"
    tc.function.name = name
    tc.function.arguments = json.dumps(args or {})
    return tc


def _response(content=None, tool_calls=None):
    response = MagicMock()
    response.model = "test-model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    return response


@pytest.fixture
def runner(engine_config):
    from robothor.engine.runner import AgentRunner

    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "search_memory"}}
        ]
        registry.get_tool_names.return_value = ["search_memory"]
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def agent_config() -> AgentConfig:
    return AgentConfig(
        id="recall-agent",
        name="Recall Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
        error_feedback=False,
    )


async def _reader_seen_by_the_tool(runner, agent_config, **run_kwargs) -> Any:
    turns = {"n": 0}

    async def completion(**_kw):
        turns["n"] += 1
        if turns["n"] == 1:
            return _response(tool_calls=[_tool_call("search_memory", {"query": "locker"})])
        return _response(content="done")

    runner.registry.execute = AsyncMock(return_value={"results": []})
    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=completion),
    ):
        await runner.execute("recall-agent", "go", agent_config=agent_config, **run_kwargs)
    calls = [c for c in runner.registry.execute.call_args_list if c.args[0] == "search_memory"]
    assert calls, "the tool was never called"
    return calls[0].kwargs.get("private_reader", "missing")


@pytest.mark.usefixtures("_mock_run_persistence")
async def test_an_unattended_run_as_a_member_carries_no_private_reader(runner, agent_config):
    reader = await _reader_seen_by_the_tool(
        runner,
        agent_config,
        trigger_type=TriggerType.CRON,
        trigger_detail=f"webchat:{OWN}",
        user_id=MEMBER,
        user_role="member",
    )
    assert reader is None


@pytest.mark.usefixtures("_mock_run_persistence")
async def test_a_member_typing_in_their_own_conversation_carries_their_reader(runner, agent_config):
    reader = await _reader_seen_by_the_tool(
        runner,
        agent_config,
        trigger_type=TriggerType.WEBCHAT,
        trigger_detail=f"webchat:{OWN}",
        user_id=MEMBER,
        user_role="member",
        identity=_identity(),
    )
    assert reader == PrivateReader(PERSON, MEMBER)
