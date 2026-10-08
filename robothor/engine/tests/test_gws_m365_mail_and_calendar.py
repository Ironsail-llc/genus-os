"""Mail AND calendar on one Microsoft 365 workspace, in one process, end to end.

The two families were built on separate branches, each with a placeholder for
the other. This drives the REAL ``gws_*`` handlers with
``workspace_provider=microsoft365`` against one fake tenant that serves both
Exchange mail and Exchange calendar, and proves that a mail send and a
calendar create both reach Graph through the same workspace, sharing one
Graph client, and that the guards in front of each still run first.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import gws
from robothor.workspace.microsoft.calendar import GraphCalendar
from robothor.workspace.microsoft.graph import GraphClient
from robothor.workspace.microsoft.mail import GraphMail
from robothor.workspace.tests.fake_graph import FakeGraphTenant
from robothor.workspace.tests.fake_graph_calendar import FakeExchangeCalendar, install_calendar
from robothor.workspace.tests.fake_graph_mail import FakeExchangeMail, install_mail

ASSISTANT = "assistant@example.com"
OWNER = "owner@example.com"
BOB = "bob@example.com"
BLOCKED = "optout@example.com"
TENANT = "tenant-a"


class StaticToken:
    def __init__(self, tenant: FakeGraphTenant) -> None:
        tenant.issued_tokens.append("static-token")

    async def token(self) -> str:
        return "static-token"


class Env:
    def __init__(
        self, tenant: FakeGraphTenant, mail: FakeExchangeMail, calendar: FakeExchangeCalendar
    ) -> None:
        self.tenant = tenant
        self.mail = mail
        self.calendar = calendar
        self.built: list[str] = []
        self.clients: list[GraphClient] = []
        self.sent: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        ctx = ToolContext(agent_id="main", run_id="run-1", tenant_id=TENANT)
        return await gws.HANDLERS[tool](args, ctx)


@pytest.fixture
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import yaml

    from robothor import workspace
    from robothor.settings import reset_settings

    owner = tmp_path / "owner.yaml"
    owner.write_text(yaml.safe_dump({"first_name": "Alice", "email": OWNER}))
    monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(owner))
    monkeypatch.setenv("ROBOTHOR_WORKSPACE_PROVIDER", "microsoft365")
    monkeypatch.setenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", ASSISTANT)
    monkeypatch.setenv("ROBOTHOR_M365_OWNER_MAILBOX", OWNER)
    monkeypatch.setenv("ROBOTHOR_TIMEZONE", "America/New_York")
    reset_settings()
    workspace.reset_workspace_cache()

    tenant = FakeGraphTenant()
    state = Env(tenant, install_mail(tenant), install_calendar(tenant))

    async def from_vault(tenant_id: str = "default", **_: Any) -> GraphClient:
        state.built.append(tenant_id)
        client = GraphClient(StaticToken(tenant), transport=tenant.transport())
        state.clients.append(client)
        return client

    monkeypatch.setattr("robothor.workspace.microsoft.graph_client_from_vault", from_vault)
    monkeypatch.setattr(gws, "ROBOTHOR_EMAIL", ASSISTANT)

    def no_cli(*a: Any, **k: Any) -> Any:
        raise AssertionError("a Microsoft 365 call reached the gws CLI")

    monkeypatch.setattr(gws, "_run_gws", no_cli)
    monkeypatch.setattr(
        "robothor.crm.dal.do_not_contact_emails",
        lambda emails, tenant_id="default": {e.lower() for e in emails if e.lower() == BLOCKED},
    )
    monkeypatch.setattr("robothor.engine.tracking.log_guardrail_event", lambda *a, **k: None)
    monkeypatch.setattr(gws, "_dnc_mode", lambda: "enforce")
    monkeypatch.setattr("robothor.engine.guardrails._lookup_scheduling_policies", lambda emails: {})
    monkeypatch.setattr("robothor.engine.feature_flags.calendar_send_updates", lambda: "all")
    monkeypatch.setattr(gws, "_record_sent_email", lambda **kw: state.sent.append(kw))
    monkeypatch.setattr(gws, "_record_calendar_event", lambda **kw: state.events.append(kw))
    yield state
    for client in state.clients:
        await client.aclose()
    workspace.reset_workspace_cache()
    reset_settings()


async def test_mail_send_and_calendar_create_in_one_workspace(env: Env) -> None:
    from robothor.workspace import get_workspace

    ws = get_workspace(TENANT)
    assert ws.provider == "microsoft365"
    assert isinstance(ws.mail, GraphMail)
    assert isinstance(ws.calendar, GraphCalendar)
    assert ws.unavailable == {}

    sent = await env.call("gws_gmail_send", {"to": BOB, "subject": "Agenda", "body": "See you"})
    assert "error" not in sent, sent
    message = env.mail.message(ASSISTANT, sent["id"])
    assert env.mail.folder_of(ASSISTANT, message) == "sentitems"
    assert [r["emailAddress"]["address"] for r in message["toRecipients"]] == [BOB]
    assert env.sent[0]["result"]["id"] == sent["id"]

    event = await env.call(
        "gws_calendar_create",
        {
            "summary": "Kickoff",
            "start": "2026-10-09T14:00:00-04:00",
            "end": "2026-10-09T15:00:00-04:00",
            "attendees": [BOB],
            "with_meet": False,
        },
    )
    assert "error" not in event, event
    assert event["calendar"] == {"kind": "operator", "id": OWNER}
    (post,) = env.tenant.requests_matching("POST", f"/users/{OWNER}/calendar/events")
    assert json.loads(post.content)["subject"] == "Kickoff"
    assert env.calendar.notifications == [{"kind": "invite", "to": [BOB], "event": event["id"]}]
    (record,) = env.events
    assert record["provider"] == "microsoft365"

    # Mail and calendar shared ONE Graph client, built once for the tool's tenant.
    assert env.built == [TENANT]
    assert get_workspace(TENANT) is ws


async def test_guards_still_run_first_for_both_families(env: Env) -> None:
    mail = await env.call("gws_gmail_send", {"to": BLOCKED, "subject": "Offer", "body": "hi"})
    assert mail["guard"] == "do_not_contact"

    event = await env.call(
        "gws_calendar_create",
        {
            "summary": "Pitch",
            "start": "2026-10-09T14:00:00-04:00",
            "end": "2026-10-09T15:00:00-04:00",
            "attendees": [BLOCKED],
            "with_meet": False,
        },
    )
    assert "error" in event
    # Neither refusal reached Graph: not even a client was built.
    assert env.tenant.requests == []
    assert env.sent == [] and env.events == []
