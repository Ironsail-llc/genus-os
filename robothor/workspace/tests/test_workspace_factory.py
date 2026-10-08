"""get_workspace(): Google by default, Microsoft 365 dark until its transport ships."""

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


def test_microsoft365_is_dark(provider_env) -> None:
    provider_env("microsoft365")
    with pytest.raises(Unsupported, match="microsoft365"):
        get_workspace()


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
