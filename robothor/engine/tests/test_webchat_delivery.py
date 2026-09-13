"""``delivery.channel: webchat`` — a worker agent reaching one member.

The end-to-end path this proves is the one C9 exists for: a manifest names
``webchat`` and a member's own Helm session plus their inbox is where the output
lands, with ``agent_runs.delivery_status`` saying ``delivered`` only because two
rows were actually written.

The second test is the regression guard that has to stay green beside it: a
channel name nothing is registered under must still fail loudly. Registering
``webchat`` as a built-in is the kind of change that quietly turns
``failed:no_channel:<name>`` into a fall-back, and a delivery redirected to a
surface that happens to work is the failure the registry exists to prevent.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from robothor.engine import chat
from robothor.engine.channels import reset_channels
from robothor.engine.delivery import deliver
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus

MEMBER = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_channels()
    chat._sessions.clear()
    yield
    reset_channels()
    chat._sessions.clear()


@pytest.fixture(autouse=True)
def _enforce_default(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_PER_USER_SESSIONS", raising=False)


@pytest.fixture
def persisted(monkeypatch):
    seen: list[str] = []

    async def _record(run: AgentRun) -> None:
        seen.append(run.delivery_status or "")

    monkeypatch.setattr("robothor.engine.delivery._persist_delivery_status", _record)
    return seen


@pytest.fixture
def store(monkeypatch):
    """The two writers, recorded. Nothing reaches a database."""
    from robothor.engine.channels import webchat as module

    written: dict[str, list] = {"turns": [], "notifications": []}

    async def _surface(session_key, content, author_agent_id, **kw):
        written["turns"].append((session_key, content))
        return 11

    def _notify(**kw):
        written["notifications"].append(kw)
        return "notif-9"

    monkeypatch.setattr(module.chat_store, "save_channel_surface_async", _surface)
    monkeypatch.setattr(module, "_send_notification", _notify)
    monkeypatch.setattr(
        module,
        "_resolve_identity",
        lambda channel, identifier, tenant: SimpleNamespace(
            channel="webchat",
            identifier=identifier,
            verified=True,
            role="member",
            user_account_id=identifier,
        ),
    )
    return written


def _run() -> AgentRun:
    return AgentRun(
        id="run-9",
        agent_id="worker",
        status=RunStatus.COMPLETED,
        output_text="Two invoices need approval.",
    )


def _config(**kw) -> AgentConfig:
    defaults: dict[str, object] = {
        "id": "worker",
        "name": "Worker",
        "delivery_mode": DeliveryMode.ANNOUNCE,
        "delivery_channel": "webchat",
        "delivery_to": MEMBER,
    }
    defaults.update(kw)
    return AgentConfig(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_delivery_channel_webchat_reaches_a_members_inbox(store, persisted):
    run = _run()
    result = await deliver(_config(), run)

    assert result is True
    assert run.delivery_channel == "webchat"
    assert run.delivery_status == "delivered"
    assert run.delivered_at is not None
    assert persisted == ["delivered"]

    session_key, body = store["turns"][0]
    assert session_key == chat.derive_user_session_key("worker", MEMBER)
    assert "Two invoices need approval." in body
    assert store["notifications"][0]["to_agent"] == MEMBER
    assert store["notifications"][0]["metadata"]["run_id"] == "run-9"


@pytest.mark.asyncio
async def test_a_half_written_delivery_is_not_reported_as_reach(store, persisted, monkeypatch):
    from robothor.engine.channels import webchat as module

    monkeypatch.setattr(module, "_send_notification", lambda **kw: None)
    run = _run()
    result = await deliver(_config(), run)

    assert result is False
    assert run.delivery_status == "failed:webchat_no_notification"
    assert run.delivered_at is None
    assert persisted == ["failed:webchat_no_notification"]


@pytest.mark.asyncio
async def test_an_unknown_channel_still_fails_loudly(store, persisted):
    run = _run()
    result = await deliver(_config(delivery_channel="teams"), run)

    assert result is False
    assert run.delivery_status == "failed:no_channel:teams"
    assert store["turns"] == []
