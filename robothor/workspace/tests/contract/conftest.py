"""One workspace, two providers: the fixture every contract scenario runs on.

``workspace_env`` is parameterised over ``google`` and ``microsoft365``. Both
drive the REAL ``gws_*`` handlers in ``gws.HANDLERS`` with a real
``ToolContext`` -- the path the engine's dispatcher takes -- so every guard
(do-not-contact, no-auto, dedup, duplicate-reply, reply-all, benchmark,
cancellation, CRM write-through) runs exactly as in production. Only the far
end of the transport is fake:

* Google: :class:`~robothor.workspace.tests.contract.fake_google.FakeGoogleWorkspace`
  behind ``gws._run_gws`` and ``calendar_attendees.CalendarTransport``;
* Microsoft 365: :class:`~robothor.workspace.tests.fake_graph.FakeGraphTenant`
  with the Exchange mail and calendar fakes installed.

A scenario talks to the env in provider-neutral words (deliver a message, seed
a meeting, count the writes) and reads results back through the tools
themselves. A difference between the providers is expressed through
``env.capabilities`` (what :class:`~robothor.workspace.protocols.Workspace`
declares), never by branching on the provider's name.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import gws

ASSISTANT = "assistant@example.com"
OPERATOR = "operator@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"
DAVE = "dave@example.com"
BLOCKED = "optout@example.com"
NO_AUTO = "noauto@example.com"
TENANT = "tenant-a"
RUN_ID = "run-contract"

#: The window every calendar read-back lists. Every seeded time is inside it.
WINDOW = {"time_min": "2026-10-01T00:00:00Z", "time_max": "2026-11-01T00:00:00Z"}

#: Neutral RSVP -> Exchange's word for it.
_GRAPH_RESPONSE = {
    "needsAction": "none",
    "accepted": "accepted",
    "declined": "declined",
    "tentative": "tentativelyAccepted",
}

PROVIDERS = ("google", "microsoft365")


# ── the CRM boundary: the real write-through, against a recording connection ──


class _Cursor:
    def __init__(self, log: list[dict[str, Any]]) -> None:
        self.log = log
        self._last = ""

    def execute(self, sql: str, params: Any = None) -> None:
        self._last = " ".join(sql.split())
        self.log.append({"sql": self._last, "params": list(params or ())})

    def fetchone(self) -> Any:
        if "RETURNING id, (xmax = 0) AS inserted" in self._last:
            return ("crm-message-1", True)
        if "RETURNING id" in self._last:
            return ("crm-row-1",)
        return None


class _Connection:
    def __init__(self, log: list[dict[str, Any]]) -> None:
        self.log = log

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self.log)

    def commit(self) -> None:
        return None


# ── the env ───────────────────────────────────────────────────────────


@dataclass
class WorkspaceEnv:
    """What a scenario may do. Subclassed once per provider."""

    provider: str
    dnc: set[str] = field(default_factory=lambda: {BLOCKED})
    no_auto: set[str] = field(default_factory=lambda: {NO_AUTO})
    crm_sql: list[dict[str, Any]] = field(default_factory=list)

    # tools ------------------------------------------------------------

    async def call(
        self, tool: str, args: dict[str, Any], *, benchmark: bool = False
    ) -> dict[str, Any]:
        ctx = ToolContext(agent_id="main", run_id=RUN_ID, tenant_id=TENANT, is_benchmark=benchmark)
        result: dict[str, Any] = await gws.HANDLERS[tool](args, ctx)
        return result

    async def call_cancelled_after_read(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """A calendar edit whose run is cancelled between its read and its write."""
        from robothor.workspace.bridge import bind_engine_loop

        class CancelAfterRead:
            def __init__(self) -> None:
                self.checks = 0

            def is_set(self) -> bool:
                self.checks += 1
                return self.checks > 1

        with bind_engine_loop(asyncio.get_running_loop()), gws._bind_tenant(TENANT):
            result: dict[str, Any] = await asyncio.to_thread(
                gws._CALENDAR_EDITS[tool],
                args,
                run_id=RUN_ID,
                tenant_id=TENANT,
                cancelled=CancelAfterRead(),
            )
        return result

    @property
    def capabilities(self) -> dict[str, Any]:
        from robothor.workspace import get_workspace

        return dict(get_workspace(TENANT).capabilities)

    async def read_event(self, event_id: str) -> dict[str, Any] | None:
        """The event as ``gws_calendar_list`` shows it now, or None if it is gone."""
        listed = await self.call("gws_calendar_list", dict(WINDOW))
        assert "error" not in listed, listed
        return next((e for e in listed["items"] if e["id"] == event_id), None)

    @staticmethod
    def responses(event: dict[str, Any]) -> dict[str, str]:
        return {a["email"].lower(): a.get("responseStatus", "") for a in event["attendees"]}

    def crm_rows(self, table: str) -> list[list[Any]]:
        return [r["params"] for r in self.crm_sql if f"INSERT INTO {table} " in r["sql"] + " "]

    # provider-specific halves ---------------------------------------------

    def deliver(
        self,
        *,
        sender: str,
        to: list[str],
        cc: list[str] | None = None,
        subject: str = "Hello",
        body: str = "Hi there",
        thread_id: str | None = None,
        from_assistant: bool = False,
    ) -> dict[str, str]:
        """Put a message in the assistant's mailbox. ``{"id", "thread_id"}``."""
        raise NotImplementedError

    def sent(self, message_id: str) -> dict[str, Any]:
        """A sent message: ``{"to": [...], "cc": [...], "thread_id", "body"}``."""
        raise NotImplementedError

    def seed_meeting(
        self,
        *,
        organizer: str,
        attendees: dict[str, str],
        summary: str = "Planning",
        start: str = "2026-10-08T10:00:00",
        end: str = "2026-10-08T10:30:00",
    ) -> str:
        """An event on the operator's calendar (times are New York wall clock)."""
        raise NotImplementedError

    def concurrent_edit_before_next_write(self, event_id: str, *, location: str) -> None:
        """Somebody else saves ``location`` between our next read and write."""
        raise NotImplementedError

    def requests(self) -> int:
        raise NotImplementedError

    def writes(self) -> int:
        """Requests to the provider that could change state."""
        raise NotImplementedError

    def event_patches(self) -> int:
        """Conditional event writes (each 412 retry is one more)."""
        raise NotImplementedError


class GoogleEnv(WorkspaceEnv):
    def __init__(self, google: Any) -> None:
        super().__init__(provider="google")
        self.google = google

    def deliver(self, *, sender, to, cc=None, subject="Hello", body="Hi there",
                thread_id=None, from_assistant=False):  # fmt: skip
        message = self.google.deliver(
            sender=sender,
            to=to,
            cc=cc or [],
            subject=subject,
            body=body,
            thread_id=thread_id,
            labels=["SENT"] if from_assistant else None,
        )
        return {"id": message["id"], "thread_id": message["threadId"]}

    def sent(self, message_id: str) -> dict[str, Any]:
        import base64

        message = self.google.messages[message_id]
        headers = {h["name"]: h["value"] for h in message["payload"]["headers"]}
        split = lambda v: sorted(re.findall(r"[\w.+-]+@[\w.-]+", v or ""))  # noqa: E731
        return {
            "to": split(headers.get("To")),
            "cc": split(headers.get("Cc")),
            "thread_id": message["threadId"],
            "body": base64.urlsafe_b64decode(message["payload"]["body"]["data"]).decode(),
        }

    def seed_meeting(self, *, organizer, attendees, summary="Planning",
                     start="2026-10-08T10:00:00", end="2026-10-08T10:30:00"):  # fmt: skip
        zone = "America/New_York"
        event = self.google.add_event(
            OPERATOR,
            {
                "summary": summary,
                "start": {"dateTime": f"{start}-04:00", "timeZone": zone},
                "end": {"dateTime": f"{end}-04:00", "timeZone": zone},
                "organizer": {
                    "email": organizer,
                    **({"self": True} if organizer == OPERATOR else {}),
                },
                "attendees": [
                    {"email": a, "responseStatus": r, **({"self": True} if a == OPERATOR else {})}
                    for a, r in attendees.items()
                ],
            },
        )
        return str(event["id"])

    def concurrent_edit_before_next_write(self, event_id: str, *, location: str) -> None:
        self.google.concurrent_edits[event_id] = {"location": location}

    def requests(self) -> int:
        return int(self.google.requests())

    def writes(self) -> int:
        return int(self.google.writes())

    def event_patches(self) -> int:
        return len(self.google.patches())


class MicrosoftEnv(WorkspaceEnv):
    def __init__(self, tenant: Any, mail: Any, calendar: Any) -> None:
        super().__init__(provider="microsoft365")
        self.tenant = tenant
        self.mail = mail
        self.calendar = calendar

    def deliver(self, *, sender, to, cc=None, subject="Hello", body="Hi there",
                thread_id=None, from_assistant=False):  # fmt: skip
        extra: dict[str, Any] = {"folder": "sentitems", "is_read": True} if from_assistant else {}
        message = self.mail.deliver(
            ASSISTANT,
            sender=sender,
            to=to,
            cc=cc or [],
            subject=subject,
            body=body,
            conversation_id=thread_id,
            **extra,
        )
        return {"id": message["id"], "thread_id": message["conversationId"]}

    def sent(self, message_id: str) -> dict[str, Any]:
        message = self.mail.message(ASSISTANT, message_id)
        assert self.mail.folder_of(ASSISTANT, message) == "sentitems"
        people = lambda key: sorted(r["emailAddress"]["address"] for r in message[key])  # noqa: E731
        return {
            "to": people("toRecipients"),
            "cc": people("ccRecipients"),
            "thread_id": message["conversationId"],
            "body": message["body"]["content"],
        }

    def seed_meeting(self, *, organizer, attendees, summary="Planning",
                     start="2026-10-08T10:00:00", end="2026-10-08T10:30:00"):  # fmt: skip
        zone = "Eastern Standard Time"
        event = self.calendar.add(
            OPERATOR,
            {
                "subject": summary,
                "start": {"dateTime": start, "timeZone": zone},
                "end": {"dateTime": end, "timeZone": zone},
                "organizer": {"emailAddress": {"address": organizer}},
                "attendees": [
                    {
                        "type": "required",
                        "status": {"response": _GRAPH_RESPONSE[r]},
                        "emailAddress": {"address": a},
                    }
                    for a, r in attendees.items()
                ],
            },
        )
        return str(event["id"])

    def concurrent_edit_before_next_write(self, event_id: str, *, location: str) -> None:
        from robothor.workspace.tests.fake_graph_calendar import _EVENT

        original = next(
            r
            for r in reversed(self.tenant._routes)
            if r.method == "PATCH" and r.pattern.pattern == f"^{_EVENT}$"
        )
        fired: list[bool] = []
        exchange = self.calendar

        @self.tenant.route("PATCH", _EVENT)
        async def racing(tenant, request, match):
            if not fired and unquote(match["id"]) == event_id:
                fired.append(True)
                event = exchange.find(OPERATOR, event_id)
                event["location"] = {"displayName": location}
                exchange._touch(event)
            return await original.handler(tenant, request, match)

    def requests(self) -> int:
        return len(self.tenant.requests)

    def writes(self) -> int:
        return sum(
            1 for r in self.tenant.requests if r.method in ("POST", "PATCH", "PUT", "DELETE")
        )

    def event_patches(self) -> int:
        return len(self.tenant.requests_matching("PATCH", f"/users/{OPERATOR}/events/.*"))


# ── fixtures ──────────────────────────────────────────────────────────


class _StaticToken:
    def __init__(self, tenant: Any) -> None:
        tenant.issued_tokens.append("contract-token")

    async def token(self) -> str:
        return "contract-token"


def _common(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, env: WorkspaceEnv) -> None:
    """Everything both providers share: identity, guards' data, the CRM."""
    import yaml

    owner = tmp_path / "owner.yaml"
    owner.write_text(yaml.safe_dump({"first_name": "Alice", "email": OPERATOR}))
    monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(owner))
    monkeypatch.delenv("ROBOTHOR_OWNER_EMAIL", raising=False)
    monkeypatch.setenv("ROBOTHOR_TIMEZONE", "America/New_York")
    monkeypatch.setattr(gws, "ROBOTHOR_EMAIL", ASSISTANT)
    monkeypatch.setattr(gws, "_dnc_mode", lambda: "enforce")
    monkeypatch.setattr(
        "robothor.crm.dal.do_not_contact_emails",
        lambda emails, tenant_id="default": {e.lower() for e in emails if e.lower() in env.dnc},
    )
    monkeypatch.setattr("robothor.engine.tracking.log_guardrail_event", lambda *a, **k: None)
    monkeypatch.setattr(
        "robothor.engine.guardrails._lookup_scheduling_policies",
        lambda emails: {e: "no_auto" for e in emails if e in env.no_auto},
    )
    monkeypatch.setattr("robothor.engine.feature_flags.calendar_send_updates", lambda: "all")
    monkeypatch.setattr("robothor.db.connection.get_connection", lambda: _Connection(env.crm_sql))
    monkeypatch.setattr(gws, "_resolve_person_by_email", lambda _email: None)
    monkeypatch.setattr("robothor.engine.tools.verification.tool_verify_mode", lambda: "enforce")
    from robothor.engine.tools.verification import reset_verification_budget

    reset_verification_budget()


async def build_env(
    provider: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, closers: list[Any]
) -> WorkspaceEnv:
    from robothor import workspace
    from robothor.settings import reset_settings

    env: WorkspaceEnv
    if provider == "google":
        from robothor.engine import calendar_attendees
        from robothor.workspace.tests.contract.fake_google import FakeGoogleWorkspace

        google = FakeGoogleWorkspace(assistant=ASSISTANT)
        env = GoogleEnv(google)
        monkeypatch.setenv("ROBOTHOR_WORKSPACE_PROVIDER", "google")
        monkeypatch.setattr(gws, "_run_gws", google.run_gws)
        monkeypatch.setattr(calendar_attendees, "CalendarTransport", google.transport)
    else:
        from robothor.workspace.microsoft.graph import GraphClient
        from robothor.workspace.tests.fake_graph import FakeGraphTenant
        from robothor.workspace.tests.fake_graph_calendar import install_calendar
        from robothor.workspace.tests.fake_graph_mail import install_mail

        tenant = FakeGraphTenant()
        env = MicrosoftEnv(tenant, install_mail(tenant), install_calendar(tenant))
        tenant.mailbox(OPERATOR).time_zone = "Eastern Standard Time"
        monkeypatch.setenv("ROBOTHOR_WORKSPACE_PROVIDER", "microsoft365")
        monkeypatch.setenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", ASSISTANT)
        monkeypatch.setenv("ROBOTHOR_M365_OWNER_MAILBOX", OPERATOR)

        async def from_vault(tenant_id: str = "default", **_: Any) -> GraphClient:
            client = GraphClient(_StaticToken(tenant), transport=tenant.transport())
            closers.append(client)
            return client

        monkeypatch.setattr("robothor.workspace.microsoft.graph_client_from_vault", from_vault)

        def no_cli(*a: Any, **k: Any) -> Any:
            raise AssertionError("a Microsoft 365 call reached the gws CLI")

        monkeypatch.setattr(gws, "_run_gws", no_cli)
    _common(monkeypatch, tmp_path, env)
    reset_settings()
    workspace.reset_workspace_cache()
    return env


@pytest.fixture(params=PROVIDERS)
async def workspace_env(request: pytest.FixtureRequest, monkeypatch, tmp_path):
    """The same workspace on each provider in turn."""
    from robothor import workspace
    from robothor.settings import reset_settings

    closers: list[Any] = []
    env = await build_env(request.param, monkeypatch, tmp_path, closers)
    yield env
    for client in closers:
        await client.aclose()
    workspace.reset_workspace_cache()
    reset_settings()


@pytest.fixture
async def m365_env(monkeypatch, tmp_path):
    """The Microsoft 365 half only (the full-flow and no-Google tests)."""
    from robothor import workspace
    from robothor.settings import reset_settings

    closers: list[Any] = []
    env = await build_env("microsoft365", monkeypatch, tmp_path, closers)
    yield env
    for client in closers:
        await client.aclose()
    workspace.reset_workspace_cache()
    reset_settings()
