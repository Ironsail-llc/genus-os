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
        return self.keys.get(key)

    async def set(self, key: str, value: str, nx: bool = False, **kw: Any) -> bool | None:
        if nx and key in self.keys:
            return None
        self.keys[key] = value.encode()
        return True

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
            if calls == 1:
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
