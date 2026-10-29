"""One engine per tenant, one shared Redis: every engine gets every event once.

Several engine processes (one per tenant) read the same ``robothor:events:*``
streams. Redis delivers each stream entry to ONE consumer of a consumer group,
so engines that share a group split the events between them at random. Each
engine therefore needs a group of its own, and must then ignore the events
that belong to another tenant, because it now sees all of them.

The root engine keeps the historical group name ``engine`` so the stream
offsets it already holds survive the upgrade; child engines are given
``engine-<tenant>`` by the deployment. Events published before tenant tagging
carry no ``tenant_id`` and belong to the root.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.config import EngineConfig
from robothor.engine.hooks import STREAM_PREFIX, EventHooks

PARENT = "acme-parent"
CHILD = "acme-child-a"
STREAM = f"{STREAM_PREFIX}crm"
TRIGGERS = {
    "crm": [
        {
            "event_type": "task.created",
            "agent_id": "task-worker",
            "message": "A task was created.",
        }
    ]
}


class FakeStreams:
    """Just enough of Redis Streams consumer-group semantics for the hook loop.

    Each group has its own read cursor, so an entry is delivered once per
    group -- to whichever consumer of that group reads it first.
    """

    def __init__(self) -> None:
        self.entries: dict[str, list[tuple[bytes, dict[bytes, bytes]]]] = {}
        self.cursors: dict[tuple[str, str], int] = {}
        self.acked: list[tuple[str, bytes]] = []
        self.created: list[tuple[str, str]] = []
        self.read_by: list[tuple[str, str]] = []
        self.on_idle: Any = None
        self._seq = 0

    def add(self, stream: str, fields: dict[str, str]) -> None:
        self._seq += 1
        encoded = {k.encode(): v.encode() for k, v in fields.items()}
        self.entries.setdefault(stream, []).append((f"{self._seq}-0".encode(), encoded))

    async def ping(self) -> bool:
        return True

    async def xgroup_create(  # redis-py's keyword is ``id``; the hook loop passes it by name
        self,
        stream: str,
        group: str,
        id: str = "$",  # noqa: A002
        mkstream: bool = False,
    ):
        if (stream, group) in self.cursors:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.created.append((stream, group))
        self.cursors[(stream, group)] = len(self.entries.setdefault(stream, [])) if id == "$" else 0
        return True

    async def xreadgroup(self, group: str, consumer: str, streams: dict[str, str], **kw: Any):
        count = kw.get("count") or 10
        out = []
        for stream in streams:
            if (stream, group) not in self.cursors:
                raise RuntimeError(f"NOGROUP no group {group!r} on {stream!r}")
            start = self.cursors[(stream, group)]
            batch = self.entries.get(stream, [])[start : start + count]
            self.cursors[(stream, group)] = start + len(batch)
            if batch:
                self.read_by.append((group, consumer))
                out.append((stream.encode(), batch))
        if not out and self.on_idle is not None:
            self.on_idle()
        return out

    async def xack(self, stream: str, group: str, msg_id: bytes) -> int:
        self.acked.append((group, msg_id))
        return 1

    async def xadd(self, stream: str, fields: dict[str, str], **kw: Any) -> bytes:
        self.add(stream, fields)
        return f"{self._seq}-0".encode()

    async def xpending_range(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def aclose(self) -> None:
        return None


def _engine(base: EngineConfig, tenant: str, group: str | None = None) -> EngineConfig:
    if group is None:
        return replace(base, tenant_id=tenant)
    return replace(base, tenant_id=tenant, event_group=group)


class _Recorder:
    """Stands in for ``_run_agent_trigger`` and records what each engine ran."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []

    def bind(self, hooks: EventHooks) -> None:
        tenant = hooks.config.tenant_id

        async def _run(trigger, bare_stream, event_type, decoded, failed, busy) -> None:
            self.runs.append((tenant, decoded.get("tenant_id", "")))

        hooks._run_agent_trigger = _run  # type: ignore[method-assign]


async def _consume_until_idle(hooks: EventHooks, fake: FakeStreams) -> None:
    fake.on_idle = hooks._stop_event.set
    with (
        patch("redis.asyncio.Redis", return_value=fake),
        patch("robothor.engine.hooks.build_event_triggers", return_value=TRIGGERS),
        patch("robothor.engine.leader.ha_leader_enabled", return_value=False),
    ):
        await asyncio.wait_for(hooks.start(), timeout=5)


def _event(tenant: str | None) -> dict[str, str]:
    fields = {"type": "task.created", "source": "crm_dal", "payload": "{}"}
    if tenant is not None:
        fields["tenant_id"] = tenant
    return fields


class TestConsumerGroupName:
    def test_default_group_is_the_historical_engine_group(self):
        assert EngineConfig().event_group == "engine"

    def test_from_env_reads_the_group_override(self, monkeypatch):
        from robothor.settings import reset_settings

        monkeypatch.setenv("GENUS_EVENT_CONSUMER_GROUP", f"engine-{CHILD}")
        reset_settings()
        try:
            assert EngineConfig.from_env().event_group == f"engine-{CHILD}"
        finally:
            reset_settings()

    def test_from_env_defaults_to_engine(self, monkeypatch):
        from robothor.settings import reset_settings

        monkeypatch.delenv("GENUS_EVENT_CONSUMER_GROUP", raising=False)
        monkeypatch.delenv("ROBOTHOR_EVENT_CONSUMER_GROUP", raising=False)
        reset_settings()
        try:
            assert EngineConfig.from_env().event_group == "engine"
        finally:
            reset_settings()

    @pytest.mark.asyncio
    async def test_the_loop_creates_and_reads_the_configured_group(self, engine_config):
        fake = FakeStreams()
        hooks = EventHooks(_engine(engine_config, CHILD, f"engine-{CHILD}"), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(hooks)

        await _consume_until_idle(hooks, fake)

        assert fake.created == [(STREAM, f"engine-{CHILD}")]
        assert {group for group, _ in fake.read_by} <= {f"engine-{CHILD}"}

    @pytest.mark.asyncio
    async def test_the_root_engine_keeps_the_engine_group(self, engine_config):
        fake = FakeStreams()
        hooks = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(hooks)

        await _consume_until_idle(hooks, fake)

        assert fake.created == [(STREAM, "engine")]


class TestEveryEngineSeesEveryEventOnce:
    @pytest.mark.asyncio
    async def test_two_engines_on_one_stream_each_handle_their_own_event(self, engine_config):
        fake = FakeStreams()
        parent = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        child = EventHooks(_engine(engine_config, CHILD, f"engine-{CHILD}"), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(parent)
        recorder.bind(child)
        # Both engines are already running: their groups exist before the events.
        await fake.xgroup_create(STREAM, "engine")
        await fake.xgroup_create(STREAM, f"engine-{CHILD}")
        fake.add(STREAM, _event(PARENT))
        fake.add(STREAM, _event(CHILD))

        await _consume_until_idle(parent, fake)
        await _consume_until_idle(child, fake)

        assert sorted(recorder.runs) == sorted([(PARENT, PARENT), (CHILD, CHILD)])
        # Each group read and acknowledged both entries: nothing left pending.
        assert sorted(g for g, _ in fake.acked) == ["engine", "engine", *[f"engine-{CHILD}"] * 2]


class TestForeignEventsAreDropped:
    @pytest.fixture
    def recorder(self) -> _Recorder:
        return _Recorder()

    def _hooks(self, cfg: EngineConfig, recorder: _Recorder) -> EventHooks:
        hooks = EventHooks(cfg, runner=None)  # type: ignore[arg-type]
        hooks._event_triggers = dict(TRIGGERS)
        hooks._prefixed_to_bare = {STREAM: "crm"}
        recorder.bind(hooks)
        return hooks

    @pytest.mark.asyncio
    async def test_child_drops_a_root_tagged_event_and_counts_it(
        self, engine_config, recorder, caplog
    ):
        fake = FakeStreams()
        hooks = self._hooks(_engine(engine_config, CHILD, f"engine-{CHILD}"), recorder)
        data = {k.encode(): v.encode() for k, v in _event(PARENT).items()}

        with caplog.at_level(logging.DEBUG, logger="robothor.engine.hooks"):
            await hooks._handle_event(STREAM, b"1-0", data, fake, f"engine-{CHILD}")

        assert recorder.runs == []
        assert hooks.foreign_events_dropped == 1
        assert fake.acked == [(f"engine-{CHILD}", b"1-0")], "a dropped event is still acked"
        dropped = [
            r for r in caplog.records if r.levelno == logging.DEBUG and PARENT in r.getMessage()
        ]
        assert dropped, "the drop is logged at DEBUG naming the event's tenant"

    @pytest.mark.asyncio
    async def test_child_handles_its_own_tagged_event(self, engine_config, recorder):
        fake = FakeStreams()
        hooks = self._hooks(_engine(engine_config, CHILD, f"engine-{CHILD}"), recorder)
        data = {k.encode(): v.encode() for k, v in _event(CHILD).items()}

        await hooks._handle_event(STREAM, b"1-0", data, fake, f"engine-{CHILD}")

        assert recorder.runs == [(CHILD, CHILD)]
        assert hooks.foreign_events_dropped == 0

    @pytest.mark.asyncio
    async def test_root_drops_a_child_tagged_event(self, engine_config, recorder):
        fake = FakeStreams()
        hooks = self._hooks(_engine(engine_config, PARENT), recorder)
        data = {k.encode(): v.encode() for k, v in _event(CHILD).items()}

        await hooks._handle_event(STREAM, b"1-0", data, fake, "engine")

        assert recorder.runs == []
        assert hooks.foreign_events_dropped == 1

    @pytest.mark.asyncio
    async def test_untagged_event_is_delivered_to_the_root_engine(self, engine_config, recorder):
        fake = FakeStreams()
        hooks = self._hooks(_engine(engine_config, PARENT), recorder)
        data = {k.encode(): v.encode() for k, v in _event(None).items()}

        await hooks._handle_event(STREAM, b"1-0", data, fake, "engine")

        assert recorder.runs == [(PARENT, "")]
        assert hooks.foreign_events_dropped == 0

    @pytest.mark.asyncio
    async def test_untagged_event_is_dropped_by_a_child_engine(self, engine_config, recorder):
        fake = FakeStreams()
        hooks = self._hooks(_engine(engine_config, CHILD, f"engine-{CHILD}"), recorder)
        data = {k.encode(): v.encode() for k, v in _event(None).items()}

        await hooks._handle_event(STREAM, b"1-0", data, fake, f"engine-{CHILD}")

        assert recorder.runs == []
        assert hooks.foreign_events_dropped == 1

    @pytest.mark.asyncio
    async def test_empty_tenant_tag_counts_as_untagged(self, engine_config, recorder):
        """The envelope always had a ``tenant_id`` field; it used to be ``""``."""
        fake = FakeStreams()
        hooks = self._hooks(_engine(engine_config, PARENT), recorder)
        data = {k.encode(): v.encode() for k, v in _event("").items()}

        await hooks._handle_event(STREAM, b"1-0", data, fake, "engine")

        assert recorder.runs == [(PARENT, "")]
