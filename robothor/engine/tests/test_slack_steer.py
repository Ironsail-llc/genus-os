"""Slack: ``/steer`` and ``/stop`` for the channel's live run, and a progress line.

Behind ``ROBOTHOR_STEER_EVERYWHERE``. Off, Slack registers what it always has
(``/status``, ``/clear``) and a message run passes no progress callback, so a
workspace sees exactly what it saw before.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from robothor.engine import live_control
from robothor.engine.channels import inbound
from robothor.engine.channels.access import AccessDecision
from robothor.engine.slack import SlackBot

TENANT = "test-tenant"
SESSION = "agent:main:slack:C123"


@pytest.fixture
def flag(monkeypatch):
    state = {"on": True}
    monkeypatch.setattr(live_control, "steer_everywhere_enabled", lambda: state["on"])
    return state


@pytest.fixture(autouse=True)
def gate(monkeypatch) -> dict[str, Any]:
    from robothor.engine.channels import access

    state: dict[str, Any] = {"decision": AccessDecision(allowed=True), "asked": []}

    async def _evaluate(channel: str, native_id: str, **kw: Any) -> AccessDecision:
        state["asked"].append({"channel": channel, "native_id": native_id, **kw})
        return state["decision"]

    monkeypatch.setattr(access, "evaluate", _evaluate)
    return state


@pytest.fixture
def controls(monkeypatch) -> dict[str, list]:
    calls: dict[str, list] = {"steer": [], "interrupt": [], "live": [True]}

    def _steer(key: str, text: str, *, tenant_id: str) -> bool:
        calls["steer"].append((key, text, tenant_id))
        return calls["live"][0]

    def _interrupt(key: str, message: str | None = None, *, tenant_id: str) -> bool:
        calls["interrupt"].append((key, tenant_id))
        return calls["live"][0]

    monkeypatch.setattr(live_control, "steer_conversation", _steer)
    monkeypatch.setattr(live_control, "interrupt_conversation", _interrupt)
    return calls


def _bot() -> SlackBot:
    return SlackBot(SimpleNamespace(), SimpleNamespace(tenant_id=TENANT))


class TestCommands:
    @pytest.mark.asyncio
    async def test_steer_reaches_the_channels_live_run(self, flag, controls):
        reply = await _bot()._steer_command("C123", user_id="U1", text="use the EU region")
        assert controls["steer"] == [(SESSION, "use the EU region", TENANT)]
        assert reply == "Steering the run."

    @pytest.mark.asyncio
    async def test_stop_interrupts_the_channels_live_run(self, flag, controls):
        reply = await _bot()._stop_command("C123", user_id="U1")
        assert controls["interrupt"] == [(SESSION, TENANT)]
        assert reply == "Stopping the run."

    @pytest.mark.asyncio
    async def test_nothing_running_is_said_plainly(self, flag, controls):
        controls["live"][0] = False
        assert await _bot()._steer_command("C123", user_id="U1", text="x") == (
            "Nothing is running here."
        )
        assert await _bot()._stop_command("C123", user_id="U1") == "Nothing is running here."

    @pytest.mark.asyncio
    async def test_a_control_that_fails_is_still_answered(self, flag, monkeypatch):
        def _boom(*_a, **_kw):
            raise ValueError("run not found in tenant")

        monkeypatch.setattr(live_control, "steer_conversation", _boom)
        monkeypatch.setattr(live_control, "interrupt_conversation", _boom)
        assert await _bot()._stop_command("C123", user_id="U1") == "Stop could not be confirmed."
        assert await _bot()._steer_command("C123", user_id="U1", text="x") == (
            "Steer could not be delivered."
        )

    @pytest.mark.asyncio
    async def test_steer_without_text_says_how(self, flag, controls):
        reply = await _bot()._steer_command("C123", user_id="U1", text="  ")
        assert controls["steer"] == []
        assert "/steer" in reply

    @pytest.mark.asyncio
    async def test_a_refused_sender_steers_nothing(self, flag, controls, gate):
        gate["decision"] = AccessDecision(allowed=False, refusal="Not paired.")
        assert await _bot()._steer_command("C123", user_id="U9", text="x") == "Not paired."
        assert await _bot()._stop_command("C123", user_id="U9") == "Not paired."
        assert controls["steer"] == [] and controls["interrupt"] == []

    @pytest.mark.asyncio
    async def test_steer_is_gated_like_a_message_in_that_channel(self, flag, controls, gate):
        await _bot()._steer_command("C123", user_id="U1", text="x")
        assert gate["asked"][0]["channel"] == "slack"
        assert gate["asked"][0]["native_id"] == "U1"


class _App:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, name: str):
        self.commands.append(name)
        return lambda fn: fn


def test_flag_on_registers_steer_and_stop(flag):
    app = _App()
    _bot()._register_run_commands(app)
    assert app.commands == ["/steer", "/stop"]


def test_flag_off_registers_neither(flag):
    flag["on"] = False
    app = _App()
    _bot()._register_run_commands(app)
    assert app.commands == []


def test_start_registers_the_run_commands_beside_status_and_clear():
    from robothor.engine.tests.astcheck import called_names, function_def

    assert "_register_run_commands" in called_names(function_def("robothor.engine.slack", "start"))


class _Client:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kw: Any) -> dict[str, Any]:  # noqa: N802 — Slack's name
        self.posted.append(kw)
        return {"ok": True, "ts": "1700000000.000100", "channel": kw["channel"]}

    async def chat_update(self, **kw: Any) -> dict[str, Any]:
        self.updated.append(kw)
        return {"ok": True}


def _run_with_events(seen: dict[str, Any]):
    async def _handle_message(**kw: Any) -> Any:
        seen.update(kw)
        on_progress = kw.get("on_progress")
        if on_progress is not None:
            await on_progress({"event": "iteration_start", "iteration": 1})
            await on_progress({"event": "tool_start", "tool": "web_search"})
            await asyncio.sleep(0.05)
            await on_progress({"event": "tool_end", "tool": "web_search", "duration_ms": 900})
        return inbound.InboundResult(reply="The answer.", ran=True, kind="answer")

    return _handle_message


async def _say_collector(said: list[dict[str, Any]]):
    async def _say(*args: Any, **kw: Any) -> None:
        said.append({"args": args, **kw})

    return _say


@pytest.mark.asyncio
async def test_flag_on_a_run_posts_one_progress_message_and_edits_it(flag, monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(inbound, "handle_message", _run_with_events(seen))
    bot = _bot()
    client = _Client()
    bot._app = SimpleNamespace(client=client)
    said: list[dict[str, Any]] = []
    await bot._on_message(
        {"text": "search it", "channel": "C123", "user": "U1"}, await _say_collector(said)
    )

    assert len(client.posted) == 1, "one progress message per run"
    assert client.posted[0]["channel"] == "C123"
    assert client.updated, "the progress message is edited in place"
    assert all(u["ts"] == "1700000000.000100" for u in client.updated)
    assert said == [{"args": (), "text": "The answer.", "mrkdwn": True}]


@pytest.mark.asyncio
async def test_flag_off_a_run_shows_no_progress(flag, monkeypatch):
    flag["on"] = False
    seen: dict[str, Any] = {}
    monkeypatch.setattr(inbound, "handle_message", _run_with_events(seen))
    bot = _bot()
    client = _Client()
    bot._app = SimpleNamespace(client=client)
    said: list[dict[str, Any]] = []
    await bot._on_message(
        {"text": "search it", "channel": "C123", "user": "U1"}, await _say_collector(said)
    )
    assert "on_progress" not in seen
    assert client.posted == [] and client.updated == []
