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
OTHER_ROOT = "acme-other"
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
        self.on_xinfo: Any = None
        self.on_read: Any = None
        self.xinfo_calls = 0
        self.keys: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.expire_calls: list[tuple[str, int]] = []
        # Key expiry runs on this clock, which tests advance by hand.
        self.clock = 0.0
        self.expires_at: dict[str, float] = {}
        self.reads = 0
        # (key, value, expire calls made before it) for each compare-and-delete
        self.deletes: list[tuple[str, str, int]] = []
        self.block: Any = None  # async (read number) -> None, awaited inside a read
        # (stream, group) -> [{"name": ..., "idle": ms}], as XINFO CONSUMERS reports
        self.consumers: dict[tuple[str, str], list[dict[str, Any]]] = {}
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
        self.reads += 1
        if self.block is not None:
            await self.block(self.reads)
        # Reading registers (or refreshes) the consumer, as Redis does.
        table = self.consumers.setdefault((next(iter(streams)), group), [])
        table[:] = [c for c in table if c["name"] not in (consumer, consumer.encode())]
        table.append({"name": consumer, "idle": 0})
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
        if self.on_read is not None:
            self.on_read(len(self.read_by))
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

    async def xinfo_consumers(self, stream: str, group: str) -> list[dict[str, Any]]:
        self.xinfo_calls += 1
        if self.on_xinfo is not None:
            self.on_xinfo(self.xinfo_calls)
        return list(self.consumers.get((stream, group), []))

    async def get(self, key: str) -> bytes | None:
        if key in self.expires_at and self.clock >= self.expires_at[key]:
            self.lapse(key)
        return self.keys.get(key)

    async def set(
        self, key: str, value: str, nx: bool = False, ex: int | None = None, **kw: Any
    ) -> bool | None:
        if nx and key in self.keys:
            return None
        self.keys[key] = value.encode()
        if ex is not None:
            self.ttls[key] = ex
            self.expires_at[key] = self.clock + ex
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        self.expire_calls.append((key, seconds))
        if key not in self.keys:
            return False
        self.ttls[key] = seconds
        self.expires_at[key] = self.clock + seconds
        return True

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        """Only the compare-and-delete the hook loop uses: DEL key iff it holds value."""
        assert numkeys == 1 and "del" in script.lower(), script
        key, value = args
        if self.keys.get(key) == str(value).encode():
            self.lapse(key)
            self.deletes.append((key, str(value), len(self.expire_calls)))
            return 1
        return 0

    def lapse(self, key: str) -> None:
        """The key's TTL ran out."""
        self.keys.pop(key, None)
        self.ttls.pop(key, None)
        self.expires_at.pop(key, None)

    async def aclose(self) -> None:
        return None


def _engine(base: EngineConfig, tenant: str, group: str | None = None) -> EngineConfig:
    if group is None:
        return replace(base, tenant_id=tenant)
    return replace(base, tenant_id=tenant, event_group=group)


class FakeHierarchy:
    """``robothor.crm.dal.get_tenant`` over a fixed tenant tree."""

    def __init__(self) -> None:
        self.parents: dict[str, str | None] = {PARENT: None, OTHER_ROOT: None, CHILD: PARENT}
        self.down: set[str] = set()
        self.calls: list[str] = []

    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        self.calls.append(tenant_id)
        if tenant_id in self.down:
            raise ConnectionError("database unreachable")
        if tenant_id not in self.parents:
            return None
        return {"id": tenant_id, "parentTenantId": self.parents[tenant_id]}


@pytest.fixture(autouse=True)
def hierarchy():
    tree = FakeHierarchy()
    with patch("robothor.crm.dal.get_tenant", side_effect=tree.get_tenant):
        yield tree


class _Recorder:
    """Stands in for ``_run_agent_trigger`` and records what each engine ran."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []

    def bind(self, hooks: EventHooks) -> None:
        tenant = hooks.config.tenant_id

        async def _run(trigger, bare_stream, event_type, decoded, failed, busy) -> None:
            self.runs.append((tenant, decoded.get("tenant_id", "")))

        hooks._run_agent_trigger = _run  # type: ignore[method-assign]


async def _consume_until_idle(
    hooks: EventHooks, fake: FakeStreams, *, stop_when_idle: bool = True
) -> None:
    fake.on_idle = hooks._stop_event.set if stop_when_idle else None
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


class TestAnotherTenantOnMyGroup:
    """Two tenants' engines on one group split the events and drop each other's.

    The group comes from configuration, so a child engine whose deployment
    forgot to set it lands on the root's ``engine`` group, where Redis would
    hand it about half the root's events to ack and drop. The two engines are
    not equals: the root group has an owner, recorded in Redis by the first
    engine that reads it with no other tenant reading and with its tenant
    variables in agreement. Only the owner reads ``engine``, so the root never
    yields to a child that forgot its setting. An engine always reads its own
    ``engine-<tenant>`` group. Every engine looks again every
    ``GROUP_RIVAL_RECHECK_SECONDS`` while it runs, not only at start.
    """

    OWNER_KEY = "robothor:events:group-owner:engine"

    @pytest.fixture(autouse=True)
    def _agreeing_tenant_env(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)

    @staticmethod
    def _stop_after_checks(hooks: EventHooks, fake: FakeStreams, n: int) -> None:
        fake.on_xinfo = lambda calls: hooks._stop_event.set() if calls >= n else None

    @pytest.mark.asyncio
    async def test_a_child_left_on_the_root_group_does_not_read_while_the_root_does(
        self, engine_config, caplog
    ):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.consumers[(STREAM, "engine")] = [{"name": f"engine-{PARENT}".encode(), "idle": 800}]
        fake.add(STREAM, _event(PARENT))
        hooks = EventHooks(_engine(engine_config, CHILD), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(hooks)
        self._stop_after_checks(hooks, fake, 1)

        with caplog.at_level(logging.ERROR, logger="robothor.engine.hooks"):
            await _consume_until_idle(hooks, fake)

        assert fake.read_by == [], "it must not take the root's events off the shared group"
        assert self.OWNER_KEY not in fake.keys, "a reader is active: nobody may claim the group"
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(f"GENUS_EVENT_CONSUMER_GROUP=engine-{CHILD}" in m for m in errors)

    @pytest.mark.asyncio
    async def test_the_root_never_yields_to_a_child_left_on_its_group(self, engine_config, caplog):
        """The root restarts while a misconfigured child is reading: it keeps
        the group, and the error names the child as the one to fix."""
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.keys[self.OWNER_KEY] = PARENT.encode()
        fake.consumers[(STREAM, "engine")] = [{"name": f"engine-{CHILD}", "idle": 10}]
        fake.add(STREAM, _event(PARENT))
        hooks = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(hooks)

        with caplog.at_level(logging.ERROR, logger="robothor.engine.hooks"):
            await _consume_until_idle(hooks, fake)

        assert recorder.runs == [(PARENT, PARENT)]
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(f"engine-{CHILD}" in m and "GENUS_EVENT_CONSUMER_GROUP" in m for m in errors)

    @pytest.mark.asyncio
    async def test_the_owner_is_recorded_and_a_later_child_cannot_take_the_group(
        self, engine_config
    ):
        """Both start after a long outage: no live reader to see, so the record
        decides -- the child stays off even with the root down."""
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(root)
        await _consume_until_idle(root, fake)
        assert fake.keys[self.OWNER_KEY] == PARENT.encode()

        # The root has been gone long enough to look stale.
        fake.consumers[(STREAM, "engine")] = [{"name": f"engine-{PARENT}", "idle": 600_000}]
        fake.add(STREAM, _event(PARENT))
        child = EventHooks(_engine(engine_config, CHILD), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(child)
        reads_before = len(fake.read_by)
        self._stop_after_checks(child, fake, fake.xinfo_calls + 1)

        await _consume_until_idle(child, fake)

        assert len(fake.read_by) == reads_before
        assert recorder.runs == []

    @pytest.mark.asyncio
    async def test_an_engine_whose_tenant_variables_disagree_never_claims_the_root_group(
        self, engine_config, monkeypatch
    ):
        """The documented child setup: ROBOTHOR_TENANT_ID names the child while
        ROBOTHOR_DEFAULT_TENANT still names the root. Started first, alone."""
        monkeypatch.setenv("ROBOTHOR_TENANT_ID", CHILD)
        monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", PARENT)
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(PARENT))
        hooks = EventHooks(_engine(engine_config, CHILD), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(hooks)
        self._stop_after_checks(hooks, fake, 1)

        await _consume_until_idle(hooks, fake)

        assert fake.read_by == []
        assert self.OWNER_KEY not in fake.keys

    @pytest.mark.asyncio
    async def test_an_engine_keeps_its_own_tenant_group(self, engine_config, caplog):
        fake = FakeStreams()
        group = f"engine-{CHILD}"
        await fake.xgroup_create(STREAM, group)
        fake.consumers[(STREAM, group)] = [{"name": f"engine-{PARENT}", "idle": 10}]
        fake.add(STREAM, _event(CHILD))
        hooks = EventHooks(_engine(engine_config, CHILD, group), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(hooks)

        with caplog.at_level(logging.ERROR, logger="robothor.engine.hooks"):
            await _consume_until_idle(hooks, fake)

        assert recorder.runs == [(CHILD, CHILD)]
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(f"engine-{PARENT}" in m for m in errors)

    @pytest.mark.asyncio
    async def test_a_late_joining_foreign_consumer_stops_a_non_owner(self, engine_config):
        """The check runs again while reading, not only before the first read."""
        fake = FakeStreams()
        group = "shared-events"  # neither the root group nor this tenant's own
        await fake.xgroup_create(STREAM, group)
        fake.add(STREAM, _event(CHILD))
        hooks = EventHooks(_engine(engine_config, CHILD, group), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(hooks)

        def _late_joiner(reads: int) -> None:
            if reads == 1:  # just after the first batch, another tenant starts reading
                fake.consumers[(STREAM, group)].append({"name": f"engine-{PARENT}", "idle": 0})
                fake.add(STREAM, _event(CHILD))

        fake.on_read = _late_joiner
        self._stop_after_checks(hooks, fake, 4)

        with patch("robothor.engine.hooks.GROUP_RIVAL_RECHECK_SECONDS", 0):
            await _consume_until_idle(hooks, fake)

        assert recorder.runs == [(CHILD, CHILD)], "it stops before reading the second event"

    @pytest.mark.asyncio
    async def test_reads_once_the_other_tenant_has_gone_quiet(self, engine_config):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        # Last seen ten minutes ago: a consumer entry outlives its engine.
        fake.consumers[(STREAM, "engine")] = [{"name": f"engine-{CHILD}", "idle": 600_000}]
        fake.add(STREAM, _event(PARENT))
        hooks = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(hooks)

        await _consume_until_idle(hooks, fake)

        assert recorder.runs == [(PARENT, PARENT)]
        assert fake.keys[self.OWNER_KEY] == PARENT.encode()

    @pytest.mark.asyncio
    async def test_replicas_of_the_same_tenant_share_the_group(self, engine_config):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.consumers[(STREAM, "engine")] = [{"name": f"engine-{PARENT}", "idle": 10}]
        fake.add(STREAM, _event(PARENT))
        hooks = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(hooks)

        await _consume_until_idle(hooks, fake)

        assert recorder.runs == [(PARENT, PARENT)]


class TestOnlyARootTenantHoldsTheRootGroup:
    """Who may hold ``engine`` is decided by the tenant tree, not by start order.

    A tenant with a parent (every cell the chart renders) can never claim or
    read the root group, whatever its setting says; the owner record only
    breaks ties between legitimate roots.
    """

    OWNER_KEY = "robothor:events:group-owner:engine"

    @pytest.fixture(autouse=True)
    def _agreeing_tenant_env(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)

    @staticmethod
    def _stop_after_checks(hooks: EventHooks, fake: FakeStreams, n: int) -> None:
        fake.on_xinfo = lambda calls: hooks._stop_event.set() if calls >= n else None

    @staticmethod
    def _errors(caplog) -> list[str]:
        return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]

    @pytest.mark.asyncio
    async def test_a_child_that_starts_first_never_claims_the_root_group(
        self, engine_config, caplog
    ):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(PARENT))
        child = EventHooks(_engine(engine_config, CHILD), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(child)
        self._stop_after_checks(child, fake, 1)

        with caplog.at_level(logging.ERROR, logger="robothor.engine.hooks"):
            await _consume_until_idle(child, fake)

        assert fake.read_by == []
        assert self.OWNER_KEY not in fake.keys
        assert any(f"GENUS_EVENT_CONSUMER_GROUP=engine-{CHILD}" in m for m in self._errors(caplog))

    @pytest.mark.asyncio
    async def test_the_root_claims_the_group_even_when_it_starts_last(self, engine_config):
        """An existing install: a child left on ``engine`` is still reading
        (it ran the old code) when the root restarts. The root takes it."""
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.consumers[(STREAM, "engine")] = [{"name": f"engine-{CHILD}", "idle": 10}]
        fake.add(STREAM, _event(PARENT))
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(root)

        await _consume_until_idle(root, fake)

        assert fake.keys[self.OWNER_KEY] == PARENT.encode()
        assert recorder.runs == [(PARENT, PARENT)]

    @pytest.mark.asyncio
    async def test_a_child_whose_setting_is_engine_is_told_to_set_its_own_group(
        self, engine_config, caplog
    ):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(PARENT))
        child = EventHooks(_engine(engine_config, CHILD, "engine"), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(child)
        self._stop_after_checks(child, fake, 1)

        with caplog.at_level(logging.ERROR, logger="robothor.engine.hooks"):
            await _consume_until_idle(child, fake)

        assert fake.read_by == []
        errors = self._errors(caplog)
        assert any(
            f"GENUS_EVENT_CONSUMER_GROUP=engine-{CHILD}" in m and PARENT in m for m in errors
        ), "the error names the child's parent and the setting to change"

    @pytest.mark.asyncio
    async def test_database_unreachable_does_not_claim_and_retries(
        self, engine_config, hierarchy, caplog
    ):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(PARENT))
        hierarchy.down.add(PARENT)
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(root)

        def _db_back(calls: int) -> None:
            # Each check reads XINFO before the tree, so the database returns
            # at the start of the second check, after the first lookup failed.
            if calls == 2:
                assert self.OWNER_KEY not in fake.keys, "no claim without knowing the tree"
                hierarchy.down.clear()

        fake.on_xinfo = _db_back

        with (
            caplog.at_level(logging.WARNING, logger="robothor.engine.hooks"),
            patch("robothor.engine.hooks.GROUP_RIVAL_RECHECK_SECONDS", 0),
        ):
            await _consume_until_idle(root, fake)

        assert hierarchy.calls.count(PARENT) >= 2, "the lookup is retried on the re-check"
        assert fake.keys[self.OWNER_KEY] == PARENT.encode()
        assert recorder.runs == [(PARENT, PARENT)]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(PARENT in m for m in warnings)

    @pytest.mark.asyncio
    async def test_the_error_names_the_owner_and_the_record(self, engine_config, caplog):
        """Two roots: the record breaks the tie, and the loser is told where
        the record lives so an operator can hand the group over."""
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.keys[self.OWNER_KEY] = OTHER_ROOT.encode()
        fake.add(STREAM, _event(PARENT))
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(root)
        self._stop_after_checks(root, fake, 1)

        with caplog.at_level(logging.ERROR, logger="robothor.engine.hooks"):
            await _consume_until_idle(root, fake)

        assert fake.read_by == []
        assert any(OTHER_ROOT in m and self.OWNER_KEY in m for m in self._errors(caplog))


class TestTheRootGroupVerdictIsNotForever:
    """Neither the tree lookup nor the owner record outlives the facts behind it.

    The tree is read again on every check, so a tenant row created or
    re-parented later takes effect without a restart. The owner record
    carries a TTL that the owning root refreshes on each check, so a dead or
    re-parented owner's claim lapses and another root can take the group
    within minutes; deleting the key stays the fast path.
    """

    OWNER_KEY = "robothor:events:group-owner:engine"
    LEGACY = "acme-legacy"  # an install whose tenant has no crm_tenants row

    @pytest.fixture(autouse=True)
    def _agreeing_tenant_env(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)

    @pytest.mark.asyncio
    async def test_a_no_row_tenant_that_later_gains_a_parent_stops_reading(
        self, engine_config, hierarchy, caplog
    ):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(self.LEGACY))
        hooks = EventHooks(_engine(engine_config, self.LEGACY), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(hooks)

        def _reparented(reads: int) -> None:
            if reads == 1:  # someone creates the row, under a parent
                hierarchy.parents[self.LEGACY] = PARENT
                fake.add(STREAM, _event(self.LEGACY))

        fake.on_read = _reparented
        fake.on_xinfo = lambda calls: hooks._stop_event.set() if calls >= 4 else None

        with (
            caplog.at_level(logging.WARNING, logger="robothor.engine.hooks"),
            patch("robothor.engine.hooks.GROUP_RIVAL_RECHECK_SECONDS", 0),
        ):
            await _consume_until_idle(hooks, fake)

        assert recorder.runs == [(self.LEGACY, self.LEGACY)], "it stops on the next check"
        no_row = [
            r for r in caplog.records if r.levelno == logging.WARNING and "no row" in r.getMessage()
        ]
        assert len(no_row) == 1, "treating a missing row as a root is said once, not every check"

    @pytest.mark.asyncio
    async def test_the_owner_record_has_a_ttl_and_the_owner_refreshes_it(self, engine_config):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(PARENT))
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(root)

        shortened: list[bool] = []

        def _nearly_lapsed(reads: int) -> None:
            # ``reads`` counts batches that returned entries, so it stays 1 on
            # the empty reads that follow: shorten the TTL only the first time.
            if reads == 1 and not shortened:
                shortened.append(True)
                assert fake.ttls[self.OWNER_KEY] == 180, "the claim carries a TTL"
                fake.ttls[self.OWNER_KEY] = 5

        fake.on_read = _nearly_lapsed
        fake.on_xinfo = lambda calls: root._stop_event.set() if calls >= 3 else None

        with patch("robothor.engine.hooks.GROUP_RIVAL_RECHECK_SECONDS", 0):
            await _consume_until_idle(root, fake)

        assert (self.OWNER_KEY, 180) in fake.expire_calls
        assert fake.ttls[self.OWNER_KEY] == 180, "the owner refreshed it on a later check"

    @pytest.mark.asyncio
    async def test_after_the_ttl_lapses_another_root_claims_it(self, engine_config):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.keys[self.OWNER_KEY] = OTHER_ROOT.encode()
        fake.ttls[self.OWNER_KEY] = 180
        fake.add(STREAM, _event(PARENT))
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(root)

        def _owner_gone(calls: int) -> None:
            if calls == 2:  # the other root died; nobody refreshed its claim
                assert fake.read_by == [], "it waited while the claim was live"
                fake.lapse(self.OWNER_KEY)

        fake.on_xinfo = _owner_gone

        with patch("robothor.engine.hooks.GROUP_RIVAL_RECHECK_SECONDS", 0):
            await _consume_until_idle(root, fake)

        assert fake.keys[self.OWNER_KEY] == PARENT.encode()
        assert recorder.runs == [(PARENT, PARENT)]


class TestARunningRootRidesOutShortOutages:
    """A root that is already reading is not stopped by a blip.

    The tree is read on every check, so a single failed lookup used to stop a
    verified root for a whole interval. Now the last verdict that was read
    successfully stands through up to ``TREE_LOOKUP_FAILURES_TOLERATED - 1``
    consecutive failures; the engine fails closed on the next one, or at once
    if it was never verified. The owner's claim is refreshed by its own task,
    so a read that blocks past the TTL does not lose the group.
    """

    OWNER_KEY = "robothor:events:group-owner:engine"

    @pytest.fixture(autouse=True)
    def _agreeing_tenant_env(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)

    @pytest.mark.asyncio
    async def test_a_failed_lookup_does_not_stop_a_verified_root(
        self, engine_config, hierarchy, caplog
    ):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(PARENT))
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(root)
        blipped: list[bool] = []

        def _blip(reads: int) -> None:
            if reads == 1 and not blipped:  # verified and reading; then the DB blips
                blipped.append(True)
                hierarchy.down.add(PARENT)
                fake.add(STREAM, _event(PARENT))

        fake.on_read = _blip
        fake.on_xinfo = lambda calls: root._stop_event.set() if calls >= 4 else None

        with (
            caplog.at_level(logging.WARNING, logger="robothor.engine.hooks"),
            patch("robothor.engine.hooks.GROUP_RIVAL_RECHECK_SECONDS", 0),
        ):
            await _consume_until_idle(root, fake, stop_when_idle=False)

        assert recorder.runs == [(PARENT, PARENT), (PARENT, PARENT)]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("last verdict" in m for m in warnings)

    @pytest.mark.asyncio
    async def test_three_consecutive_failures_fail_closed(self, engine_config, hierarchy):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(PARENT))
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(root)
        blipped: list[bool] = []

        def _db_down(reads: int) -> None:
            if reads == 1 and not blipped:
                blipped.append(True)
                hierarchy.down.add(PARENT)

        def _checks(calls: int) -> None:
            # Check 1 verified the root. Checks 2 and 3 fail and keep the
            # verdict; check 4 is the third consecutive failure.
            if calls == 4:
                fake.add(STREAM, _event(PARENT))
            if calls >= 6:
                root._stop_event.set()

        fake.on_read = _db_down
        fake.on_xinfo = _checks

        with patch("robothor.engine.hooks.GROUP_RIVAL_RECHECK_SECONDS", 0):
            await _consume_until_idle(root, fake, stop_when_idle=False)

        assert recorder.runs == [(PARENT, PARENT)], "after the third failure it reads nothing"

    @pytest.mark.asyncio
    async def test_a_read_blocked_past_the_ttl_does_not_lose_ownership(self, engine_config):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(PARENT))
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(root)

        async def _long_block(read: int) -> None:
            if read != 2:
                return
            # One read blocks for 200 s of Redis time, past the 180 s TTL,
            # while the event loop keeps running other tasks.
            for _ in range(20):
                fake.clock += 10
                await asyncio.sleep(0.01)
            assert await fake.get(self.OWNER_KEY) == PARENT.encode(), "ownership survived"
            root._stop_event.set()

        fake.block = _long_block

        with patch("robothor.engine.hooks.ROOT_GROUP_OWNER_REFRESH_SECONDS", 0.005):
            await _consume_until_idle(root, fake, stop_when_idle=False)

        assert recorder.runs == [(PARENT, PARENT)]
        assert await fake.get(self.OWNER_KEY) == PARENT.encode()

    def test_the_ttl_is_at_least_three_refreshes(self):
        from robothor.engine import hooks

        assert hooks.ROOT_GROUP_OWNER_TTL_SECONDS >= 3 * hooks.ROOT_GROUP_OWNER_REFRESH_SECONDS


class TestAnOwnerThatLosesTheGroupLetsItGo:
    """An owner that is no longer entitled to the root group gives up its claim.

    Re-parented, or failing closed after repeated lookup failures, the engine
    stops reading ``engine``; it must also stop refreshing its claim and
    delete the record -- only if it still holds this engine's tenant -- so
    another root can claim at once instead of after a TTL that would never
    lapse while the refresher kept extending it.
    """

    OWNER_KEY = "robothor:events:group-owner:engine"

    @pytest.fixture(autouse=True)
    def _agreeing_tenant_env(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)

    @pytest.fixture(autouse=True)
    def _fast_cadence(self):
        with (
            patch("robothor.engine.hooks.GROUP_RIVAL_RECHECK_SECONDS", 0.01),
            patch("robothor.engine.hooks.ROOT_GROUP_OWNER_REFRESH_SECONDS", 0.002),
        ):
            yield

    @staticmethod
    def _owner_then(fake: FakeStreams, change: Any) -> Any:
        """After the owner's first batch, apply ``change`` once."""
        done: list[bool] = []

        def _hook(reads: int) -> None:
            if reads == 1 and not done:
                done.append(True)
                change()

        return _hook

    async def _run(self, hooks: EventHooks, fake: FakeStreams, checks: int) -> None:
        fake.on_xinfo = lambda calls: hooks._stop_event.set() if calls >= checks else None
        await _consume_until_idle(hooks, fake, stop_when_idle=False)

    @pytest.mark.asyncio
    async def test_a_re_parented_owner_deletes_its_claim_and_stops_refreshing(
        self, engine_config, hierarchy
    ):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(PARENT))
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(root)
        fake.on_read = self._owner_then(
            fake, lambda: hierarchy.parents.update({PARENT: OTHER_ROOT})
        )

        await self._run(root, fake, checks=8)

        assert self.OWNER_KEY not in fake.keys, "the claim is released on the next check"
        assert [d[:2] for d in fake.deletes] == [(self.OWNER_KEY, PARENT)]
        released_after = fake.deletes[0][2]
        assert all(k != self.OWNER_KEY for k, _ in fake.expire_calls[released_after:]), (
            "the refresher stops extending a claim it gave up"
        )

    @pytest.mark.asyncio
    async def test_another_root_claims_within_one_ttl(self, engine_config, hierarchy):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(PARENT))
        old = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(old)
        fake.on_read = self._owner_then(
            fake, lambda: hierarchy.parents.update({PARENT: OTHER_ROOT})
        )
        await self._run(old, fake, checks=4)

        fake.clock += 1  # well inside one TTL
        fake.on_read = None
        fake.add(STREAM, _event(OTHER_ROOT))
        new = EventHooks(_engine(engine_config, OTHER_ROOT), runner=None)  # type: ignore[arg-type]
        recorder = _Recorder()
        recorder.bind(new)
        await self._run(new, fake, checks=fake.xinfo_calls + 3)

        assert fake.keys[self.OWNER_KEY] == OTHER_ROOT.encode()
        assert (OTHER_ROOT, OTHER_ROOT) in recorder.runs

    @pytest.mark.asyncio
    async def test_failing_closed_releases_the_claim(self, engine_config, hierarchy):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.add(STREAM, _event(PARENT))
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(root)
        fake.on_read = self._owner_then(fake, lambda: hierarchy.down.add(PARENT))

        await self._run(root, fake, checks=8)

        assert self.OWNER_KEY not in fake.keys
        assert [d[:2] for d in fake.deletes] == [(self.OWNER_KEY, PARENT)]

    @pytest.mark.asyncio
    async def test_a_non_owner_never_deletes_the_claim(self, engine_config, hierarchy):
        fake = FakeStreams()
        await fake.xgroup_create(STREAM, "engine")
        fake.keys[self.OWNER_KEY] = OTHER_ROOT.encode()
        fake.add(STREAM, _event(PARENT))
        child = EventHooks(_engine(engine_config, CHILD), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(child)
        await self._run(child, fake, checks=3)

        hierarchy.down.add(PARENT)  # a root that was never verified fails closed
        root = EventHooks(_engine(engine_config, PARENT), runner=None)  # type: ignore[arg-type]
        _Recorder().bind(root)
        await self._run(root, fake, checks=fake.xinfo_calls + 3)

        assert fake.keys[self.OWNER_KEY] == OTHER_ROOT.encode()
        assert fake.deletes == []
