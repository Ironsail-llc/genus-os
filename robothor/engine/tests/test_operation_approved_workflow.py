"""An approved operation action runs a workflow that executes it.

``Operations.decide`` publishes ``operation_action.approved`` on the
``operations`` stream once the approval commits. A workflow whose trigger is
``{type: hook, stream: operations, event_type: operation_action.approved}``
runs with the action id in ``event.payload`` and hands it to a plugin's tool.

No PostgreSQL or Redis: the ledger's connection is a fake that answers like a
row in review, and the published event is captured and replayed through the
hook consumer the way Redis would deliver it.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.hooks import STREAM_PREFIX, EventHooks
from robothor.engine.models import RunStatus
from robothor.engine.workflow import WorkflowEngine, parse_workflow
from robothor.events import bus
from robothor.operations.store import Operations

TENANT = "test-tenant"
ACTION = "00000000-0000-4000-8000-000000000001"

WORKFLOW = {
    "id": "acme-execute-approved",
    "triggers": [
        {"type": "hook", "stream": "operations", "event_type": "operation_action.approved"}
    ],
    "steps": [
        {
            "id": "execute",
            "type": "tool",
            "tool_name": "acme_execute_action",
            "tool_args": {
                "action_id": "{{ event.payload.action_id }}",
                "kind": "{{ event.payload.kind }}",
            },
        }
    ],
}


class _Cursor:
    """Answers every query as if one acme-plugin row were in review."""

    rowcount = 1

    def execute(self, *_args: Any) -> None:
        return None

    def fetchone(self) -> dict[str, Any]:
        return {"kind": "acme-plugin.note", "proposed_by": "acme-plugin", "status": "review"}

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


@contextmanager
def _connection():
    conn = MagicMock()
    conn.cursor.return_value = _Cursor()
    yield conn


def _approve(actor: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Approve ACTION through the real ``decide`` and return what it published."""
    published: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    with (
        patch("robothor.operations.store.get_connection", _connection),
        patch("robothor.events.bus.publish", side_effect=lambda *a, **k: published.append((a, k))),
    ):
        Operations(TENANT).decide(ACTION, True, actor)
    return published


def _as_delivered(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[bytes, bytes]:
    """The stream entry Redis would hand the consumer for this publish call."""
    _stream, event_type, payload = args
    envelope = bus._make_envelope(
        event_type,
        payload,
        source=kwargs.get("source", "unknown"),
        actor=kwargs.get("actor", "robothor"),
        tenant_id=kwargs.get("tenant_id", ""),
    )
    return {k.encode(): v.encode() for k, v in envelope.items()}


def _engine(engine_config: Any, tool: AsyncMock) -> WorkflowEngine:
    engine = WorkflowEngine(engine_config, runner=MagicMock())
    engine.runner.registry.execute = tool
    wf = parse_workflow(WORKFLOW)
    engine._workflows[wf.id] = wf
    engine._persist_run_start = MagicMock()  # type: ignore[method-assign]
    engine._persist_run_end = MagicMock()  # type: ignore[method-assign]
    engine._persist_step_start = MagicMock()  # type: ignore[method-assign]
    engine._persist_step = MagicMock()  # type: ignore[method-assign]
    return engine


async def _deliver(hooks: EventHooks, stream: str, data: dict[bytes, bytes]) -> list[Any]:
    """Handle one stream entry and await every workflow run it spawned."""
    registry = MagicMock()
    runs: list[Any] = []
    original = hooks.workflow_engine.execute

    async def recording_execute(*args: Any, **kwargs: Any) -> Any:
        run = await original(*args, **kwargs)
        runs.append(run)
        return run

    hooks.workflow_engine.execute = recording_execute  # type: ignore[method-assign]
    with patch("robothor.engine.hooks.get_task_registry", return_value=registry):
        await hooks._handle_event(stream, b"1-0", data, AsyncMock(), "engine")
    for call in registry.spawn.call_args_list:
        await call.args[0]
    return runs


def test_operations_is_a_known_stream():
    assert "operations" in bus.VALID_STREAMS


@pytest.mark.parametrize("actor", ["policy:acme-plugin:2026-10-29", "operator:1"])
def test_approval_publishes_the_action_on_the_operations_stream(actor):
    published = _approve(actor)
    assert len(published) == 1
    args, kwargs = published[0]
    assert args == (
        "operations",
        "operation_action.approved",
        {
            "action_id": ACTION,
            "kind": "acme-plugin.note",
            "tenant_id": TENANT,
            "proposed_by": "acme-plugin",
        },
    )
    assert kwargs["tenant_id"] == TENANT
    assert kwargs["actor"] == actor


@pytest.mark.asyncio
async def test_approved_event_runs_the_workflow_with_the_action_id(engine_config):
    tool = AsyncMock(return_value={"ok": True})
    engine = _engine(engine_config, tool)
    hooks = EventHooks(engine_config, engine.runner, workflow_engine=engine)
    stream = f"{STREAM_PREFIX}operations"
    hooks._prefixed_to_bare = {stream: "operations"}

    (published,) = _approve("policy:acme-plugin:2026-10-29")
    runs = await _deliver(hooks, stream, _as_delivered(*published))

    assert [r.status for r in runs] == [RunStatus.COMPLETED]
    assert runs[0].context["event"]["payload"]["action_id"] == ACTION
    tool.assert_awaited_once()
    name, args = tool.await_args.args[:2]
    assert name == "acme_execute_action"
    assert args == {"action_id": ACTION, "kind": "acme-plugin.note"}
    assert tool.await_args.kwargs["tenant_id"] == engine_config.tenant_id


@pytest.mark.asyncio
async def test_a_rejection_runs_nothing():
    published: list[Any] = []
    with (
        patch("robothor.operations.store.get_connection", _connection),
        patch("robothor.events.bus.publish", side_effect=lambda *a, **k: published.append(a)),
    ):
        Operations(TENANT).decide(ACTION, False, "policy:acme-plugin:1")
    assert published == []
