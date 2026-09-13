"""``deliver()`` routes ANNOUNCE by channel name, not to Telegram unconditionally.

Before this, ``delivery.deliver()`` dispatched on the delivery *mode* alone and
called ``_deliver_telegram`` for every ANNOUNCE run. ``delivery_channel`` was
parsed from the manifest, stored on ``agent_schedules`` and reported on the
dashboard, and never once consulted to decide where the output went — so an
instance that configured ``channel: slack`` got Telegram and no error.

The tests here pin the two halves of the fix that can rot independently:

* a named channel is the one that receives the body, and
* a name nothing is registered under is a recorded failure, never a quiet
  fall-back to Telegram. A silent fall-back is the failure mode the sandbox
  seam was built to avoid (``sandbox.active_sandbox_backend``); delivery gets
  the same rule.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from robothor.engine.channels import SendReceipt, register_channel, reset_channels
from robothor.engine.delivery import deliver, set_telegram_sender
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _RecordingChannel:
    """A channel that records what it was handed and acknowledges all of it."""

    name = "fake"
    inbound_router = None

    def __init__(self, acknowledged: int = 1, expected: int = 1) -> None:
        self.sends: list[tuple[str, str]] = []
        self._acknowledged = acknowledged
        self._expected = expected

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"channel": self.name}

    async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
        self.sends.append((target, text))
        return SendReceipt(
            acknowledged=self._acknowledged,
            expected=self._expected,
            platform_ids=["1"],
            target=target,
            body=text,
        )


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_channels()
    yield
    reset_channels()


@pytest.fixture
def telegram_sender():
    sender = AsyncMock(return_value=[_FakeMessage(1)])
    set_telegram_sender(sender)
    yield sender
    set_telegram_sender(None)  # type: ignore[arg-type]


@pytest.fixture
def persisted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every status handed to the ``agent_runs`` writer, in order.

    Asserting only the in-memory field would pass with the DB write deleted —
    the column is what analytics and the heartbeat ping read.
    """
    seen: list[str] = []

    async def _record(run: AgentRun) -> None:
        seen.append(run.delivery_status or "")

    monkeypatch.setattr("robothor.engine.delivery._persist_delivery_status", _record)
    return seen


def _run(**kwargs: object) -> AgentRun:
    defaults: dict[str, object] = {
        "id": "run-1",
        "agent_id": "alice",
        "status": RunStatus.COMPLETED,
        "output_text": "Two invoices need approval.",
    }
    defaults.update(kwargs)
    return AgentRun(**defaults)  # type: ignore[arg-type]


def _config(**kwargs: object) -> AgentConfig:
    defaults: dict[str, object] = {
        "id": "alice",
        "name": "Alice",
        "delivery_mode": DeliveryMode.ANNOUNCE,
        "delivery_to": "42",
    }
    defaults.update(kwargs)
    return AgentConfig(**defaults)  # type: ignore[arg-type]


class TestAnnounceRoutesByChannel:
    @pytest.mark.asyncio
    async def test_announce_routes_to_the_named_channel(self, telegram_sender, persisted):
        channel = _RecordingChannel()
        register_channel("fake", channel)
        run = _run()

        result = await deliver(_config(delivery_channel="fake"), run)

        assert result is True
        assert len(channel.sends) == 1
        target, body = channel.sends[0]
        assert target == "42"
        assert "Two invoices need approval." in body
        telegram_sender.assert_not_called()
        assert run.delivery_channel == "fake"
        assert run.delivery_status == "delivered"
        assert run.delivered_at is not None
        assert persisted == ["delivered"]

    @pytest.mark.asyncio
    async def test_empty_delivery_channel_still_means_telegram(self, telegram_sender, persisted):
        """The migration must not change a single existing instance's behaviour."""
        run = _run()

        result = await deliver(_config(delivery_channel=""), run)

        assert result is True
        telegram_sender.assert_called_once()
        assert run.delivery_channel == "telegram"
        assert run.delivery_status == "delivered"
        assert run.delivered_at is not None
        assert persisted == ["delivered"]

    @pytest.mark.asyncio
    async def test_named_telegram_is_the_same_path_as_the_default(self, telegram_sender):
        run = _run()
        assert await deliver(_config(delivery_channel="telegram"), run) is True
        telegram_sender.assert_called_once()
        assert run.delivery_status == "delivered"


class TestUnknownChannel:
    @pytest.mark.asyncio
    async def test_unknown_channel_is_failed_no_channel(self, telegram_sender, persisted):
        run = _run()

        result = await deliver(_config(delivery_channel="teams"), run)

        assert result is False
        assert run.delivery_status == "failed:no_channel:teams"
        assert run.delivered_at is None
        assert run.delivery_channel == "teams"
        assert persisted == ["failed:no_channel:teams"], (
            "the failure must reach the agent_runs column, not just the object"
        )

    @pytest.mark.asyncio
    async def test_no_channel_does_not_fall_back_to_telegram(self, telegram_sender):
        """A working Telegram sender must not rescue a misconfigured channel."""
        await deliver(_config(delivery_channel="teams"), _run())
        telegram_sender.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_failed_prefix_is_preserved_for_the_heartbeat_ping(self, telegram_sender):
        """``scheduler._maybe_emit_heartbeat_status_ping`` matches on ``failed:``."""
        run = _run()
        await deliver(_config(delivery_channel="teams"), run)
        assert (run.delivery_status or "").startswith("failed:")


class TestAChannelThatMisbehaves:
    @pytest.mark.asyncio
    async def test_a_raising_channel_is_recorded_not_propagated(self, telegram_sender, persisted):
        """A third-party channel's bug must not escape into run finalization,
        where it would be indistinguishable from the run itself failing."""

        class _Exploding:
            name = "boom"
            inbound_router = None

            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def health(self) -> dict[str, Any]:
                return {}

            async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
                raise RuntimeError("sdk exploded")

        register_channel("boom", _Exploding())
        run = _run()

        result = await deliver(_config(delivery_channel="boom"), run)

        assert result is False
        assert (run.delivery_status or "").startswith("failed:boom_exception")
        assert "sdk exploded" in (run.delivery_status or "")
        assert run.delivered_at is None
        assert persisted and persisted[0].startswith("failed:boom_exception")


class TestTheAnnounceBranchIsNotHardcoded:
    """AST, not a substring match.

    ``test_plugin_groups_are_consumed.py`` rejects greps as evidence for
    exactly this shape of guard: a text search for ``get_channel`` would pass
    with the call sitting in a comment or in an unrelated branch.
    """

    @staticmethod
    def _module_tree() -> ast.Module:
        module = importlib.import_module("robothor.engine.delivery")
        assert module.__file__ is not None
        return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

    @staticmethod
    def _calls(node: ast.AST) -> set[str | None]:
        return {
            getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
        }

    @classmethod
    def _announce_call_sets(cls) -> list[set[str | None]]:
        """The calls on each announce path through ``deliver()``.

        Two things changed here after this guard raised a false alarm:

        * **Every** ``If`` in ``deliver()`` testing ANNOUNCE is collected, not
          the first. Taking the first made the guard report on whichever
          announce-mode branch happened to sit highest: a thin-announce check
          added above the dispatch failed ``test_deliver_announce_resolves_a_
          channel`` with ``assert 'get_channel' in {'note_substitution'}`` — a
          complaint about a property that had not changed.
        * Each branch's call set is **extended with the bodies of same-module
          helpers it calls**, so extracting the send into
          ``_send_announcement`` cannot move the hardcoded-Telegram check out
          from under the guard. A guard that a refactor can walk out of is the
          inert kind this repo keeps finding.
        """
        tree = cls._module_tree()
        helpers = {
            n.name: n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        deliver_fn = helpers["deliver"]
        branches = []
        for node in ast.walk(deliver_fn):
            if isinstance(node, ast.If):
                names = {a.attr for a in ast.walk(node.test) if isinstance(a, ast.Attribute)}
                if "ANNOUNCE" in names:
                    branches.append(node)
        assert branches, "deliver() has no DeliveryMode.ANNOUNCE branch"
        every = []
        for branch in branches:
            calls = cls._calls(branch)
            for name in list(calls):
                if name in helpers and name != "deliver":
                    calls |= cls._calls(helpers[name])
            every.append(calls)
        return every

    def test_deliver_announce_resolves_a_channel(self):
        assert any("get_channel" in calls for calls in self._announce_call_sets()), (
            "no ANNOUNCE branch resolves a channel by name — "
            "delivery_channel is being parsed and ignored again"
        )

    def test_deliver_announce_does_not_hardcode_telegram(self):
        assert all("_deliver_telegram" not in calls for calls in self._announce_call_sets()), (
            "an ANNOUNCE branch still calls _deliver_telegram directly, so a "
            "named channel is decoration"
        )

    def test_the_guard_follows_the_helper_the_send_was_extracted_into(self):
        """Evidence that the transitive walk is load-bearing: the send itself
        lives in ``_send_announcement`` now, and ``channel.send`` has to be
        visible from the announce branch through it."""
        assert any("send" in calls for calls in self._announce_call_sets())
