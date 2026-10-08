"""get_workspace(): Google by default; Microsoft 365 mail and calendar when selected."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from robothor.workspace import Workspace, get_workspace
from robothor.workspace.bridge import bind_engine_loop, blocking
from robothor.workspace.errors import NotFound, Unsupported
from robothor.workspace.google.adapter import GoogleCalendar, GoogleMail


@pytest.fixture
def provider_env(monkeypatch: pytest.MonkeyPatch):
    from robothor.settings import reset_settings

    def use(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("ROBOTHOR_WORKSPACE_PROVIDER", raising=False)
        else:
            monkeypatch.setenv("ROBOTHOR_WORKSPACE_PROVIDER", value)
        reset_settings()

    yield use
    reset_settings()


def test_defaults_to_google(provider_env) -> None:
    provider_env(None)
    ws = get_workspace()
    assert isinstance(ws, Workspace)
    assert ws.provider == "google"
    assert isinstance(ws.mail, GoogleMail)
    assert isinstance(ws.calendar, GoogleCalendar)
    assert ws.capabilities["provider"] == "google"
    # Stateless and shared: building one per tool call costs nothing.
    assert get_workspace("tenant-a").mail is ws.mail


def test_explicit_google(provider_env) -> None:
    provider_env("google")
    assert get_workspace().provider == "google"


def test_microsoft365_without_an_assistant_mailbox_is_refused(provider_env, monkeypatch) -> None:
    monkeypatch.delenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", raising=False)
    provider_env("microsoft365")
    with pytest.raises(Unsupported, match="m365_assistant_mailbox") as caught:
        get_workspace()
    assert caught.value.code == "not_configured"


def test_microsoft365_misconfigured_mailbox_is_not_configured(provider_env, monkeypatch) -> None:
    from robothor.workspace import reset_workspace_cache

    monkeypatch.setenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", "not-an-address")
    reset_workspace_cache()
    provider_env("microsoft365")
    with pytest.raises(Unsupported, match="misconfigured") as caught:
        get_workspace("tenant-a")
    assert caught.value.code == "not_configured"
    reset_workspace_cache()


def test_microsoft365_serves_mail_and_calendar(provider_env, monkeypatch) -> None:
    from robothor.workspace import reset_workspace_cache
    from robothor.workspace.microsoft.calendar import GraphCalendar
    from robothor.workspace.microsoft.mail import GraphMail

    monkeypatch.setenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", "Assistant@example.com")
    monkeypatch.setenv("ROBOTHOR_M365_OWNER_MAILBOX", "owner@example.com")
    reset_workspace_cache()
    provider_env("microsoft365")
    ws = get_workspace("tenant-a")
    assert ws.provider == "microsoft365"
    assert ws.capabilities["send_updates_modes"] == ("all",)
    assert ws.capabilities["labels"] == "categories"
    assert ws.capabilities["online_meeting"] == "teams_meeting"
    # Both families are real Graph providers: nothing is dark any more.
    assert ws.unavailable == {}
    assert isinstance(ws.mail, GraphMail)
    assert ws.mail.mailbox == "assistant@example.com"
    assert isinstance(ws.calendar, GraphCalendar)
    assert ws.calendar.resolve("own").mailbox == "assistant@example.com"
    # Cached per platform tenant; a different tenant gets its own credentials.
    assert get_workspace("tenant-a") is ws
    assert get_workspace("tenant-b") is not ws
    assert get_workspace("tenant-b").mail is not ws.mail
    reset_workspace_cache()


def test_microsoft365_owner_mailbox_change_rebuilds(provider_env, monkeypatch) -> None:
    from robothor.workspace import reset_workspace_cache

    monkeypatch.setenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", "assistant@example.com")
    monkeypatch.setenv("ROBOTHOR_M365_OWNER_MAILBOX", "owner@example.com")
    reset_workspace_cache()
    provider_env("microsoft365")
    first = get_workspace("tenant-a")
    monkeypatch.setenv("ROBOTHOR_M365_OWNER_MAILBOX", "other-owner@example.com")
    provider_env("microsoft365")
    second = get_workspace("tenant-a")
    assert second is not first
    assert second.calendar.owner_mailbox == "other-owner@example.com"
    reset_workspace_cache()


def test_microsoft365_mail_and_calendar_share_one_graph_client(provider_env, monkeypatch) -> None:
    from robothor.workspace import microsoft, reset_workspace_cache

    built: list[object] = []

    async def fake_from_vault(tenant_id: str) -> object:
        client = object()
        built.append(client)
        return client

    monkeypatch.setattr(microsoft, "graph_client_from_vault", fake_from_vault)
    monkeypatch.setenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", "assistant@example.com")
    reset_workspace_cache()
    provider_env("microsoft365")
    ws = get_workspace("tenant-a")

    async def both() -> tuple[object, object]:
        return await ws.mail._factory(), await ws.calendar._factory()

    mail_client, calendar_client = asyncio.run(both())
    assert mail_client is calendar_client
    assert len(built) == 1
    reset_workspace_cache()
    reset_workspace_cache()


# ── the worker-thread bridge ──────────────────────────────────────────


class AsyncOnly:
    """A provider with no blocking twin (the shape Microsoft 365 will have)."""

    def __init__(self) -> None:
        self.loops: list[Any] = []

    async def get_message(self, message_id: str, *, fmt: str) -> dict[str, Any]:
        self.loops.append(asyncio.get_running_loop())
        return {"id": message_id, "fmt": fmt}

    async def get_thread(self, thread_id: str, *, fmt: str) -> dict[str, Any]:
        raise NotFound("no such thread", status=404, code="ErrorItemNotFound")

    def shape_envelope(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {"id": raw["id"]}


def test_blocking_prefers_the_twin() -> None:
    mail = GoogleMail()
    assert blocking(mail) is mail.blocking


async def test_async_provider_runs_on_the_engine_loop_from_a_worker_thread() -> None:
    provider = AsyncOnly()
    loop = asyncio.get_running_loop()
    with bind_engine_loop(loop):
        out = await asyncio.to_thread(lambda: blocking(provider).get_message("m1", fmt="full"))
    assert out == {"id": "m1", "fmt": "full"}
    assert provider.loops == [loop]


async def test_workspace_errors_come_back_as_error_dicts() -> None:
    provider = AsyncOnly()
    with bind_engine_loop(asyncio.get_running_loop()):
        out = await asyncio.to_thread(lambda: blocking(provider).get_thread("t1", fmt="full"))
    assert out == {"error": "no such thread", "hint": "not_found", "status_code": 404}


def test_sync_attributes_pass_straight_through() -> None:
    assert blocking(AsyncOnly()).shape_envelope({"id": "x"}) == {"id": "x"}


def test_async_provider_without_an_engine_loop_is_refused() -> None:
    with pytest.raises(Unsupported, match="engine event loop"):
        blocking(AsyncOnly()).get_message("m1", fmt="full")


async def test_calling_from_the_loop_thread_itself_is_refused() -> None:
    with bind_engine_loop(asyncio.get_running_loop()), pytest.raises(Unsupported):
        blocking(AsyncOnly()).get_message("m1", fmt="full")
