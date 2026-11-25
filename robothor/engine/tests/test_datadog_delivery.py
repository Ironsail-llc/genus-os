"""Datadog alerts reach people, in the tenant they belong to and nowhere else.

The root engine consumes every Datadog webhook (it carries no tenant), routes
it by the monitor's ``org:`` tag, and either delivers it itself (its own
tenant's alert) or hands it to the routed tenant's own engine through a
tenant-addressed event on the same stream. Only that engine delivers it, and
only after routing it again from the alert's own tags.

These tests drive real :class:`EventHooks` instances for three tenants over
one shared fake Redis, so every safety property is a statement about what the
engines actually did, not about a helper.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.engine import datadog_delivery as dlv
from robothor.engine.event_writer import get_event_writer
from robothor.engine.hooks import STREAM_PREFIX, EventHooks

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig

ROOT, A, B = DEFAULT_TENANT, "acme-child-a", "acme-child-b"
STREAM = f"{STREAM_PREFIX}datadog"
TENANTS = {A: {"id": A, "active": True}, B: {"id": B, "active": True}}
#: Text that a webhook body may carry beside the title and must never be sent.
QUERY_TEXT = 'logs("service:shop-api status:error card 4111").rollup("count") > 10'


def _payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "monitor_id": "12345",
        "event_id": "777",
        "title": "[Triggered] shop-api error rate high",
        "transition": "Triggered",
        "status": "Alert",
        "priority": "P2",
        "query": QUERY_TEXT,
        "text_only_message": f"Last log line: {QUERY_TEXT}",
        "tags": f"env:prod,org:{A},service:shop-api",
        "link": "https://app.example.com/monitors/12345",
        "date": "1764000000000",
    }
    base.update(over)
    return base


def _webhook(payload: dict[str, Any], received_by: str = ROOT) -> dict[bytes, bytes]:
    """What the hooks receiver publishes: tagged with the receiving engine's tenant."""
    fields = {
        "type": "datadog.alert",
        "event_type": "datadog.alert",
        "source": "webhook",
        "actor": "external",
        "payload": json.dumps(payload),
        "tenant_id": received_by,
    }
    return {k.encode(): v.encode() for k, v in fields.items()}


class SharedRedis:
    """One Redis for every engine: keys, the stream, and who reads it."""

    def __init__(self, running: tuple[str, ...] = (A, B)) -> None:
        self.keys: dict[str, Any] = {}
        self.added: list[tuple[str, dict[str, Any]]] = []
        self.acked: list[tuple[str, str, Any]] = []
        self.running = set(running)
        self.fail_xadd = False
        self.root_running = True

    async def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> Any:
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def get(self, key: str) -> Any:
        return self.keys.get(key)

    async def exists(self, *keys: str) -> int:
        return sum(1 for k in keys if k in self.keys)

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.keys.pop(k, None) is not None)

    async def xack(self, stream: str, group: str, msg_id: Any) -> int:
        self.acked.append((stream, group, msg_id))
        return 1

    async def xadd(self, stream: str, fields: dict[str, Any], **kw: Any) -> bytes:
        if self.fail_xadd and not stream.endswith(":dlq"):
            raise ConnectionError("redis went away")
        self.added.append((stream, dict(fields)))
        return f"{len(self.added)}-0".encode()

    async def xinfo_groups(self, stream: str) -> list[dict[str, Any]]:
        groups = [{"name": "engine"}]
        groups += [{"name": f"engine-{t}"} for t in sorted(self.running)]
        return groups

    async def xinfo_consumers(self, stream: str, group: str) -> list[dict[str, Any]]:
        if group == "engine":
            return [{"name": f"engine-{ROOT}", "idle": 100}] if self.root_running else []
        tenant = group.removeprefix("engine-")
        return [{"name": f"engine-{tenant}", "idle": 100}] if tenant in self.running else []

    def handoffs(self) -> list[dict[bytes, bytes]]:
        return [
            {k.encode(): str(v).encode() for k, v in fields.items()}
            for stream, fields in self.added
            if stream == STREAM
        ]


class Fleet:
    """Three engines on one Redis, and everything each of them sent or started."""

    def __init__(self, engine_config: EngineConfig, redis: SharedRedis) -> None:
        self.redis = redis
        self.engines: dict[str, EventHooks] = {}
        self.runners: dict[str, MagicMock] = {}
        for tenant in (ROOT, A, B):
            runner = MagicMock()
            run = MagicMock()
            run.status.value = "completed"
            runner.execute = AsyncMock(return_value=run)
            group = "engine" if tenant == ROOT else f"engine-{tenant}"
            hooks = EventHooks(
                replace(engine_config, tenant_id=tenant, event_group=group), runner=runner
            )
            hooks._event_triggers = {"datadog": []}
            hooks._prefixed_to_bare = {STREAM: "datadog"}
            self.engines[tenant] = hooks
            self.runners[tenant] = runner
        self.current = ""
        #: (engine tenant, level, title, body, kwargs) for every alerts.alert call.
        self.sent: list[tuple[str, str, str, str, dict[str, Any]]] = []
        self.result: Any = True
        self.entries = 0
        #: Levels whose send fails (a tenant without a working pager).
        self.failing_levels: set[str] = set()
        #: Awaited inside the next send, before it returns: work that lands meanwhile.
        self.during_send: Any = None

    async def alert(self, level: str, title: str, body: str, **kw: Any) -> bool:
        self.sent.append((self.current, level, title, body, kw))
        if self.during_send is not None:
            hook, self.during_send = self.during_send, None
            await hook()
        if isinstance(self.result, BaseException):
            raise self.result
        if level in self.failing_levels:
            return False
        return bool(self.result)

    async def feed(
        self, tenant: str, envelope: dict[bytes, bytes], entry: bytes | None = None
    ) -> None:
        """One engine handles one stream entry; a new entry id unless ``entry`` replays one."""
        self.current = tenant
        hooks = self.engines[tenant]
        group = hooks.config.event_group
        if entry is None:
            self.entries += 1
            entry = f"{self.entries}-0".encode()
        await hooks._handle_event(STREAM, entry, envelope, self.redis, group)
        await dlv.wait_for_triage()

    async def broadcast(self, envelope: dict[bytes, bytes]) -> None:
        """Every engine reads every event on the stream (one group each)."""
        for tenant in (ROOT, A, B):
            await self.feed(tenant, envelope)

    async def deliver_webhook(self, payload: dict[str, Any], received_by: str = ROOT) -> None:
        before = len(self.redis.handoffs())
        await self.broadcast(_webhook(payload, received_by))
        for handoff in self.redis.handoffs()[before:]:
            await self.broadcast(handoff)

    def triage_runs(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (tenant, call.kwargs)
            for tenant, runner in self.runners.items()
            for call in runner.execute.await_args_list
        ]


class Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_event(self, event_type: str, action: str, **kw: Any) -> dict[str, Any]:
        self.rows.append({"event_type": event_type, "action": action, **kw})
        return {"id": len(self.rows), "timestamp": "2026-11-25T00:00:00+00:00"}

    def alerts(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["event_type"] == "datadog.alert_received"]


@pytest.fixture
def audit():
    fake = Audit()
    with patch("robothor.audit.logger.log_event", side_effect=fake.log_event):
        yield fake


@pytest.fixture
def fleet(engine_config: EngineConfig, audit: Audit, monkeypatch: pytest.MonkeyPatch):
    dlv.reset_state()
    # A busy main agent is waited for; in tests, without the wall-clock pauses.
    monkeypatch.setattr(dlv, "TRIAGE_WAIT_SECONDS", (0, 0, 0))
    redis = SharedRedis()
    f = Fleet(engine_config, redis)
    with (
        patch("robothor.crm.dal.get_tenant", side_effect=lambda t: TENANTS.get(t)),
        patch("robothor.engine.alerts.alert", side_effect=f.alert),
        patch("robothor.engine.manifest_store.main_agent_id", return_value="main"),
        patch("robothor.engine.config.load_agent_config", return_value=MagicMock(name="cfg")),
        patch("robothor.engine.delivery.deliver", new_callable=AsyncMock) as deliver,
        patch("robothor.engine.dedup.try_acquire", new_callable=AsyncMock, return_value=True),
        patch("robothor.engine.dedup.release", new_callable=AsyncMock),
    ):
        f.deliver = deliver  # type: ignore[attr-defined]
        yield f
    dlv.reset_state()


# ── where an alert is delivered ─────────────────────────────────────


async def test_the_routed_tenants_own_engine_delivers_it(fleet: Fleet, audit: Audit) -> None:
    await fleet.deliver_webhook(_payload(priority="P1"))

    assert [(t, level) for t, level, *_ in fleet.sent] == [(A, "critical")]
    (row,) = audit.alerts()
    assert row["tenant_id"] == A
    d = row["details"]
    assert d["mode"] == "delivered"
    assert d["delivery"]["level"] == "critical"
    assert d["delivery"]["route"] == "page"
    assert d["triage"]["started"] is True
    assert [t for t, _ in fleet.triage_runs()] == [A]
    # Readable by the existing dashboard reader: who was told, and the run.
    assert d["would_notify"]["level"] == "critical"
    assert d["would_notify"]["route"] == "page"
    assert d["would_run"]["agent"] == "main"
    assert d["would_run"]["read_only"] is True
    assert "12345" in d["would_run"]["prompt"]


@pytest.mark.parametrize("priority", ["P1", "p1", "1", " P1 "])
async def test_a_p1_trigger_pages(fleet: Fleet, priority: str) -> None:
    await fleet.deliver_webhook(_payload(priority=priority))
    assert [level for _, level, *_ in fleet.sent] == ["critical"]


@pytest.mark.parametrize("priority", ["P2", "P3", "p5", "", "P10", "11", "high"])
async def test_any_other_trigger_goes_to_the_digest_and_still_triages(
    fleet: Fleet, audit: Audit, priority: str
) -> None:
    await fleet.deliver_webhook(_payload(priority=priority))
    assert [level for _, level, *_ in fleet.sent] == ["warning"]
    (row,) = audit.alerts()
    assert row["details"]["delivery"]["route"] == "digest"
    assert row["details"]["would_notify"]["route"] == "digest"
    assert len(fleet.triage_runs()) == 1


async def test_the_root_hands_off_and_delivers_nothing_itself(fleet: Fleet, audit: Audit) -> None:
    await fleet.feed(ROOT, _webhook(_payload()))

    assert fleet.sent == [] and fleet.triage_runs() == [] and audit.alerts() == []
    (handoff,) = fleet.redis.handoffs()
    assert handoff[b"tenant_id"] == A.encode()
    assert handoff[b"type"] == dlv.HANDOFF_TYPE.encode()
    assert handoff[b"source"] == dlv.HANDOFF_SOURCE.encode()


async def test_the_roots_own_alert_is_delivered_by_the_root(fleet: Fleet, audit: Audit) -> None:
    await fleet.deliver_webhook(_payload(tags="env:prod"))

    assert [(t, level) for t, level, *_ in fleet.sent] == [(ROOT, "warning")]
    (row,) = audit.alerts()
    assert row["tenant_id"] == ROOT and row["details"]["routing"] == "untagged"
    assert [t for t, _ in fleet.triage_runs()] == [ROOT]
    assert fleet.redis.handoffs() == []


TAGS = {
    "org_a": (f"org:{A},service:shop-api", A),
    "org_b": (f"org:{B}", B),
    "untagged": ("env:prod", ROOT),
    "unknown": ("org:acme-nobody", ROOT),
    "ambiguous": (f"org:{A},org:{B}", ROOT),
}


@pytest.mark.parametrize(
    ("tags", "transition"),
    list(itertools.product(TAGS, ["Triggered", "Warn", "Recovered"])),
)
async def test_an_alert_is_delivered_only_in_the_tenant_it_routes_to(
    fleet: Fleet, audit: Audit, tags: str, transition: str
) -> None:
    raw, expected = TAGS[tags]
    await fleet.deliver_webhook(_payload(tags=raw, transition=transition))

    assert {t for t, *_ in fleet.sent} == {expected}
    assert len(fleet.sent) == 1
    assert {r["tenant_id"] for r in audit.alerts()} == {expected}
    assert {t for t, _ in fleet.triage_runs()} <= {expected}
    for _, kwargs in fleet.triage_runs():
        assert kwargs.get("tenant_id") in (None, expected)


@pytest.mark.parametrize("received_by", [A, B])
@pytest.mark.parametrize("tags", list(TAGS))
async def test_a_webhook_received_by_a_child_engine_routes_the_same(
    fleet: Fleet, audit: Audit, received_by: str, tags: str
) -> None:
    """The receiver may run beside any engine; the root's alerts still reach only the root."""
    raw, expected = TAGS[tags]
    await fleet.deliver_webhook(_payload(tags=raw), received_by=received_by)

    assert [t for t, *_ in fleet.sent] == [expected]
    assert [r["tenant_id"] for r in audit.alerts()] == [expected]
    assert {t for t, _ in fleet.triage_runs()} == {expected}


async def test_a_root_alert_a_child_received_is_not_recorded_in_the_child(
    fleet: Fleet, audit: Audit
) -> None:
    """No root engine to hand it to: logged, and never kept in the child's tenant."""
    fleet.redis.root_running = False
    await fleet.deliver_webhook(_payload(tags="env:prod"), received_by=A)
    assert fleet.sent == [] and audit.alerts() == [] and fleet.triage_runs() == []


@pytest.mark.parametrize(
    ("addressed_to", "tags"),
    [
        (A, f"org:{B}"),  # addressed to A, belongs to B
        (A, "env:prod"),  # untagged: only the root's
        (A, "org:acme-nobody"),
        (A, f"org:{A},org:{B}"),
    ],
)
async def test_a_hand_off_that_routes_elsewhere_is_refused(
    fleet: Fleet, audit: Audit, addressed_to: str, tags: str
) -> None:
    forged = {
        "type": dlv.HANDOFF_TYPE,
        "source": dlv.HANDOFF_SOURCE,
        "payload": json.dumps(_payload(tags=tags)),
        "tenant_id": addressed_to,
    }
    await fleet.broadcast({k.encode(): v.encode() for k, v in forged.items()})

    assert fleet.sent == []
    assert fleet.triage_runs() == []
    assert audit.alerts() == []


async def test_a_webhook_typed_as_a_hand_off_is_not_one(fleet: Fleet, audit: Audit) -> None:
    """Only the engine's own hand-off source counts, whatever the type says."""
    fake = {
        "type": dlv.HANDOFF_TYPE,
        "source": "webhook",
        "payload": json.dumps(_payload()),
        "tenant_id": A,
    }
    await fleet.feed(A, {k.encode(): v.encode() for k, v in fake.items()})
    assert fleet.sent == [] and audit.alerts() == []


# ── a tenant without an engine ──────────────────────────────────────


async def test_no_engine_for_the_tenant_is_recorded_there_and_the_root_is_told(
    fleet: Fleet, audit: Audit
) -> None:
    fleet.redis.running = {B}
    await fleet.deliver_webhook(_payload())

    (row,) = audit.alerts()
    assert row["tenant_id"] == A
    assert row["details"]["mode"] == "delivery_failed"
    assert row["details"]["delivery"]["reason"] == "no_engine"
    assert fleet.triage_runs() == []
    # One quiet notice at the root, naming the tenant and not the alert.
    ((tenant, level, title, body, kw),) = fleet.sent
    assert (tenant, level) == (ROOT, "info")
    assert A in title
    assert "shop-api error rate" not in title + body
    assert kw["alert_id"] == f"datadog_undelivered:{A}"


async def test_a_failed_hand_off_is_recorded_as_failed(fleet: Fleet, audit: Audit) -> None:
    fleet.redis.fail_xadd = True
    await fleet.deliver_webhook(_payload())
    (row,) = audit.alerts()
    assert row["tenant_id"] == A
    assert row["details"]["mode"] == "delivery_failed"
    assert row["details"]["delivery"]["reason"] == "handoff_failed"
    assert [(t, level) for t, level, *_ in fleet.sent] == [(ROOT, "info")]


# ── once per notification, once per episode ─────────────────────────


async def test_a_retried_webhook_and_a_replayed_hand_off_deliver_once(
    fleet: Fleet, audit: Audit
) -> None:
    await fleet.deliver_webhook(_payload())
    await fleet.deliver_webhook(_payload())  # Datadog's retry: same $ID
    (handoff,) = fleet.redis.handoffs()
    await fleet.broadcast(handoff)  # the stream entry read again after a restart

    assert len(fleet.sent) == 1
    assert len(audit.alerts()) == 1
    assert len(fleet.triage_runs()) == 1


async def test_a_storm_for_one_monitor_is_one_alert_and_one_triage(
    fleet: Fleet, audit: Audit
) -> None:
    for n in range(20):
        transition = "Triggered" if n == 0 else "Re-Triggered"
        await fleet.deliver_webhook(
            _payload(event_id=str(1000 + n), date=str(1764000000000 + n), transition=transition)
        )

    assert len(fleet.sent) == 1
    assert len(fleet.triage_runs()) == 1
    modes = [r["details"]["mode"] for r in audit.alerts()]
    assert modes == ["delivered"] + ["suppressed"] * 19
    assert {r["details"]["delivery"].get("reason") for r in audit.alerts()[1:]} == {"episode_open"}


@pytest.mark.parametrize(("priority", "level"), [("P1", "critical"), ("P3", "warning")])
async def test_warn_then_trigger_escalates_once_and_triages_once(
    fleet: Fleet, audit: Audit, priority: str, level: str
) -> None:
    """A trigger is a step above Warn even when, below P1, it is sent at the same level."""
    for n, transition in enumerate(["Warn", "Re-Warn", "Triggered", "Warn"]):
        await fleet.deliver_webhook(
            _payload(transition=transition, event_id=str(n), date=str(n), priority=priority)
        )

    assert [lvl for _, lvl, *_ in fleet.sent] == ["warning", level]
    assert len(fleet.triage_runs()) == 1
    assert [r["details"]["mode"] for r in audit.alerts()] == [
        "delivered",
        "suppressed",
        "delivered",
        "suppressed",
    ]


async def test_warn_and_no_data_start_no_run(fleet: Fleet, audit: Audit) -> None:
    await fleet.deliver_webhook(_payload(transition="Warn"))
    await fleet.deliver_webhook(_payload(transition="No Data", event_id="2", monitor_id="2"))
    assert fleet.triage_runs() == []
    assert all(r["details"]["triage"]["started"] is False for r in audit.alerts())


async def test_a_recovery_sends_one_info_and_starts_no_run(fleet: Fleet, audit: Audit) -> None:
    await fleet.deliver_webhook(_payload(date="8"))
    await fleet.deliver_webhook(_payload(transition="Recovered", event_id="9", date="9"))
    await fleet.deliver_webhook(_payload(transition="Recovered", event_id="10", date="10"))

    assert [level for _, level, *_ in fleet.sent] == ["warning", "info"]
    assert len(fleet.triage_runs()) == 1
    # The recovery closed the episode: the next trigger is a new one.
    await fleet.deliver_webhook(_payload(event_id="11", date="11"))
    assert [level for _, level, *_ in fleet.sent] == ["warning", "info", "warning"]
    assert len(fleet.triage_runs()) == 2


async def test_a_lone_recovery_sends_one_info(fleet: Fleet, audit: Audit) -> None:
    await fleet.deliver_webhook(_payload(transition="Recovered"))
    assert [level for _, level, *_ in fleet.sent] == ["info"]
    assert fleet.triage_runs() == []


async def _concurrently(fleet: Fleet, tenant: str, envelope: dict[bytes, bytes]) -> None:
    """Two copies in one stream batch: EventHooks handles them side by side."""
    import asyncio

    fleet.current = tenant
    hooks = fleet.engines[tenant]
    group = hooks.config.event_group
    await asyncio.gather(
        hooks._handle_event(STREAM, b"1-0", dict(envelope), fleet.redis, group),
        hooks._handle_event(STREAM, b"2-0", dict(envelope), fleet.redis, group),
    )
    await dlv.wait_for_triage()


async def test_two_copies_handled_at_once_deliver_and_record_once(
    fleet: Fleet, audit: Audit
) -> None:
    """A Datadog retry read in the same batch as the original."""
    await _concurrently(fleet, ROOT, _webhook(_payload(tags="env:prod")))

    assert len(fleet.sent) == 1
    assert len(fleet.triage_runs()) == 1
    assert len(audit.alerts()) == 1


async def test_two_copies_of_a_hand_off_handled_at_once_deliver_and_record_once(
    fleet: Fleet, audit: Audit
) -> None:
    await _concurrently(fleet, ROOT, _webhook(_payload()))
    (handoff,) = fleet.redis.handoffs()
    await _concurrently(fleet, A, handoff)

    assert [t for t, *_ in fleet.sent] == [A]
    assert [t for t, _ in fleet.triage_runs()] == [A]
    assert len(audit.alerts()) == 1


async def test_a_failed_write_after_delivery_never_delivers_again(
    fleet: Fleet, audit: Audit
) -> None:
    """The claim stays for its pending window, and the event goes to the DLQ."""
    with patch("robothor.audit.logger.log_event", return_value=None):
        await fleet.feed(ROOT, _webhook(_payload(tags="env:prod")))
    assert [s for s, _ in fleet.redis.added] == [f"{STREAM}:dlq"]
    await fleet.feed(ROOT, _webhook(_payload(tags="env:prod")))  # Datadog's retry
    assert len(fleet.sent) == 1
    assert len(fleet.triage_runs()) == 1


async def test_a_failed_write_before_anything_was_sent_accepts_the_retry(
    fleet: Fleet, audit: Audit
) -> None:
    fleet.result = False  # the send fails, so nothing left this engine
    with patch("robothor.audit.logger.log_event", return_value=None):
        await fleet.feed(ROOT, _webhook(_payload(tags="env:prod")))
    fleet.result = True
    await fleet.feed(ROOT, _webhook(_payload(tags="env:prod")))
    (row,) = audit.alerts()
    assert row["details"]["mode"] == "delivered"
    assert len(fleet.triage_runs()) == 1


class Crash(BaseException):
    """The engine process dies here: nothing after this line runs."""


def _sends(fleet: Fleet) -> int:
    return len(fleet.sent) - fleet.crashed_sends  # type: ignore[attr-defined]


async def test_a_crash_before_the_send_is_finished_by_the_replayed_entry(
    fleet: Fleet, audit: Audit
) -> None:
    """The consumer group redelivers the unacknowledged entry: it is delivered once."""
    fleet.crashed_sends = 1  # type: ignore[attr-defined]
    fleet.result = Crash()
    envelope = _webhook(_payload(tags="env:prod"))
    with pytest.raises(Crash):
        await fleet.feed(ROOT, envelope, entry=b"7-0")
    assert audit.alerts() == [] and fleet.triage_runs() == []

    fleet.result = True
    await fleet.feed(ROOT, envelope, entry=b"7-0")  # the same entry, after the restart

    assert _sends(fleet) == 1
    assert len(fleet.triage_runs()) == 1
    (row,) = audit.alerts()
    assert row["details"]["mode"] == "delivered"


@pytest.mark.parametrize("tags", ["env:prod", f"org:{A}"])
async def test_a_crash_after_the_send_never_sends_twice_and_records_once(
    fleet: Fleet, audit: Audit, tags: str
) -> None:
    """Sent and triaged, then the engine died before the audit write; its entry is replayed."""
    from robothor.integrations.datadog import alerts as dd

    real_record = dd._record
    crashes = [Crash()]

    def record_then_crash(*args: Any) -> None:
        if crashes:
            raise crashes.pop()
        real_record(*args)

    expected = ROOT if tags == "env:prod" else A
    with patch.object(dd, "_record", side_effect=record_then_crash):
        if expected == ROOT:
            envelope = _webhook(_payload(tags=tags))
        else:
            await fleet.feed(ROOT, _webhook(_payload(tags=tags)))
            (envelope,) = fleet.redis.handoffs()
        with pytest.raises(Crash):
            await fleet.feed(expected, envelope, entry=b"8-0")
        await fleet.feed(expected, envelope, entry=b"8-0")

    assert [t for t, *_ in fleet.sent] == [expected]
    assert len(fleet.triage_runs()) == 1
    (row,) = audit.alerts()
    assert row["tenant_id"] == expected
    assert row["details"]["mode"] == "delivered", "it was sent: the record says so"
    assert row["details"]["triage"]["started"] is True


async def test_another_entry_of_the_same_notification_is_still_a_duplicate(
    fleet: Fleet, audit: Audit
) -> None:
    envelope = _webhook(_payload(tags="env:prod"))
    await fleet.feed(ROOT, envelope, entry=b"9-0")
    await fleet.feed(ROOT, envelope, entry=b"9-1")  # Datadog's retry: a new entry
    assert len(fleet.sent) == 1 and len(audit.alerts()) == 1


# ── failures are recorded, never raised ─────────────────────────────


@pytest.mark.parametrize("transition", ["Triggered", "Warn"])
async def test_a_failed_digest_send_is_tried_again_by_the_next_notification(
    fleet: Fleet, audit: Audit, transition: str
) -> None:
    """Not only a page: a failed warning is not counted as the episode's alert."""
    re = "Re-Triggered" if transition == "Triggered" else "Re-Warn"
    fleet.result = False
    await fleet.deliver_webhook(_payload(transition=transition, event_id="1", date="1"))
    fleet.result = True
    await fleet.deliver_webhook(_payload(transition=re, event_id="2", date="2"))
    await fleet.deliver_webhook(_payload(transition=re, event_id="3", date="3"))

    assert [lvl for _, lvl, *_ in fleet.sent] == ["warning", "warning"]
    assert [r["details"]["mode"] for r in audit.alerts()] == [
        "delivery_failed",
        "delivered",
        "suppressed",
    ]
    assert len({kw["alert_id"] for *_, kw in fleet.sent}) == 1


@pytest.mark.parametrize(
    ("result", "error"), [(RuntimeError("telegram down"), "RuntimeError"), (False, "not_delivered")]
)
async def test_a_failed_send_is_recorded_and_the_trigger_is_still_triaged(
    fleet: Fleet, audit: Audit, result: Any, error: str
) -> None:
    """When paging is broken, the read-only look matters most: once per episode."""
    fleet.result = result
    await fleet.deliver_webhook(_payload(priority="P1"))
    await fleet.deliver_webhook(_payload(priority="P1", transition="Re-Triggered", event_id="2"))

    rows = audit.alerts()
    assert [r["details"]["mode"] for r in rows] == ["delivery_failed", "delivery_failed"]
    assert rows[0]["details"]["delivery"]["error"] == error
    assert rows[0]["details"]["triage"]["started"] is True
    assert len(fleet.triage_runs()) == 1
    assert len(fleet.sent) == 2, "the failed page is tried again"
    assert all(stream != f"{STREAM}:dlq" for stream, _ in fleet.redis.added)


async def test_a_failed_warn_starts_no_run(fleet: Fleet, audit: Audit) -> None:
    fleet.result = False
    await fleet.deliver_webhook(_payload(transition="Warn"))
    assert fleet.triage_runs() == []


async def test_a_run_that_cannot_start_is_recorded_and_raises_nothing(
    fleet: Fleet, audit: Audit
) -> None:
    fleet.runners[A].execute.side_effect = RuntimeError("no model")
    await fleet.deliver_webhook(_payload())
    await get_event_writer().flush()

    (row,) = audit.alerts()
    assert row["details"]["mode"] == "delivered"
    runs = [r for r in audit.rows if r["event_type"] == "datadog.triage_run"]
    assert [(r["tenant_id"], r["details"]["status"]) for r in runs] == [(A, "failed")]
    assert runs[0]["details"]["error"] == "RuntimeError"
    assert all(stream != f"{STREAM}:dlq" for stream, _ in fleet.redis.added)


async def test_a_main_agent_busy_for_good_is_recorded_and_frees_the_episode(
    fleet: Fleet, audit: Audit
) -> None:
    busy = AsyncMock(return_value=False)
    with patch("robothor.engine.dedup.try_acquire", busy):
        await fleet.deliver_webhook(_payload())
    await get_event_writer().flush()
    assert fleet.triage_runs() == []
    assert busy.await_count == 1 + len(dlv.TRIAGE_WAIT_SECONDS), "it waited before giving up"
    runs = [r for r in audit.rows if r["event_type"] == "datadog.triage_run"]
    assert [r["details"]["status"] for r in runs] == ["agent_busy"]
    # The episode is not left without a run: the next trigger of it may start one.
    await fleet.deliver_webhook(_payload(transition="Re-Triggered", event_id="2"))
    assert len(fleet.triage_runs()) == 1


async def test_a_main_agent_busy_at_first_is_waited_for(fleet: Fleet, audit: Audit) -> None:
    """Two monitors of one incident: the second run waits for the first instead of dropping."""
    turns = AsyncMock(side_effect=[False, False, True])
    with patch("robothor.engine.dedup.try_acquire", turns):
        await fleet.deliver_webhook(_payload())
    assert len(fleet.triage_runs()) == 1


# ── the episode moves on whatever the send did ──────────────────────


async def test_a_step_held_by_the_cooldown_still_moves_the_episode(
    fleet: Fleet, audit: Audit
) -> None:
    """Fire, recover, fire inside the cooldown, recover: the next day's trigger is sent."""
    from robothor.engine import alerts

    await fleet.deliver_webhook(_payload(event_id="1", date="1"))
    await fleet.deliver_webhook(_payload(transition="Recovered", event_id="2", date="2"))
    with patch.object(alerts, "_suppressed", return_value=True):
        await fleet.deliver_webhook(_payload(event_id="3", date="3"))
        await fleet.deliver_webhook(_payload(transition="Recovered", event_id="4", date="4"))
    await fleet.deliver_webhook(_payload(event_id="5", date="5"))  # the next day

    modes = [r["details"]["mode"] for r in audit.alerts()]
    assert modes == ["delivered", "delivered", "suppressed", "suppressed", "delivered"]
    assert [r["details"]["delivery"].get("reason") for r in audit.alerts()][2:4] == [
        "cooldown",
        "cooldown",
    ]
    assert [lvl for _, lvl, *_ in fleet.sent] == ["warning", "info", "warning"]


async def test_a_recovery_whose_send_fails_still_closes_the_episode(
    fleet: Fleet, audit: Audit
) -> None:
    await fleet.deliver_webhook(_payload(event_id="1", date="1"))
    fleet.result = False
    await fleet.deliver_webhook(_payload(transition="Recovered", event_id="2", date="2"))
    fleet.result = True
    await fleet.deliver_webhook(_payload(event_id="3", date="3"))  # a new incident

    assert [r["details"]["mode"] for r in audit.alerts()] == [
        "delivered",
        "delivery_failed",
        "delivered",
    ]
    assert len(fleet.triage_runs()) == 2


# ── a child engine never acts for another tenant ────────────────────


@pytest.mark.parametrize(("tags", "absent"), [(f"org:{B}", B), ("env:prod", ROOT)])
async def test_a_child_that_cannot_hand_off_tells_and_records_nothing_for_others(
    fleet: Fleet, audit: Audit, tags: str, absent: str
) -> None:
    if absent == ROOT:
        fleet.redis.root_running = False
    else:
        fleet.redis.running = {A}
    await fleet.deliver_webhook(_payload(tags=tags), received_by=A)

    assert fleet.sent == [], "no notice about another tenant in this tenant's digest"
    assert audit.alerts() == [], "and no record written for another tenant from here"
    assert fleet.triage_runs() == []


async def test_no_main_agent_is_recorded_on_the_alert(fleet: Fleet, audit: Audit) -> None:
    with patch("robothor.engine.manifest_store.main_agent_id", return_value=""):
        await fleet.deliver_webhook(_payload())
    (row,) = audit.alerts()
    assert row["details"]["mode"] == "delivered"
    assert row["details"]["triage"] == {"started": False, "reason": "no_main_agent"}
    assert fleet.triage_runs() == []


# ── what is sent ────────────────────────────────────────────────────


async def test_only_names_and_the_masked_title_are_sent(fleet: Fleet, audit: Audit) -> None:
    await fleet.deliver_webhook(_payload(title="[Triggered] login failures for agent@example.com"))

    sent = json.dumps([s[1:] for s in fleet.sent], default=str)
    assert "agent@example.com" not in sent
    assert "<email>" in sent and "login failures" in sent
    assert "card 4111" not in sent and "status:error" not in sent
    assert "12345" in sent
    prompts = json.dumps([kw["message"] for _, kw in fleet.triage_runs()])
    assert "agent@example.com" not in prompts and "card 4111" not in prompts


# ── the triage run ──────────────────────────────────────────────────


async def test_the_triage_run_is_read_only_for_the_main_agent(fleet: Fleet) -> None:
    await fleet.deliver_webhook(_payload())

    ((tenant, kw),) = fleet.triage_runs()
    assert tenant == A
    assert kw["agent_id"] == "main"
    assert kw["readonly_mode"] is True
    assert kw["tenant_id"] == A
    assert "12345" in kw["message"]
    assert "do not have the Datadog tools" in kw["message"]
    # Its result goes out through the agent's normal delivery, once.
    fleet.deliver.assert_awaited_once()


def test_a_read_only_run_offers_no_write_tool() -> None:
    """The restriction the triage run relies on: plan mode's read-only tool set."""
    from robothor.engine.models import AgentConfig
    from robothor.engine.tools import get_registry
    from robothor.engine.tools.constants import READONLY_TOOLS

    registry = get_registry()
    main = AgentConfig(
        id="main",
        name="main",
        tools_allowed=["read_file", "web_search", "write_file", "exec", "create_task"],
    )
    full = set(registry.get_tool_names(main))
    offered = set(registry.get_readonly_tool_names(main))
    writes = full - READONLY_TOOLS
    assert writes, "the agent holds write tools to withhold"
    assert offered, "and read-only tools to offer"
    assert not offered & writes
    assert offered <= READONLY_TOOLS


# ── closing an episode, in any order ────────────────────────────────


def _triage_key(tenant: str, monitor: str = "12345") -> str:
    return f"{dlv.EPISODE_PREFIX}{tenant}:{monitor}:triage"


async def test_a_recovery_closes_the_episode_even_after_a_failed_page(
    fleet: Fleet, audit: Audit
) -> None:
    """No working pager: every P1 page fails, and still each episode recovers and triages."""
    fleet.failing_levels = {"critical"}
    for episode in range(3):
        t, r = str(10 * episode + 1), str(10 * episode + 2)
        await fleet.deliver_webhook(_payload(priority="P1", event_id=t, date=t))
        await fleet.deliver_webhook(_payload(transition="Recovered", event_id=r, date=r))

    recoveries = [r for r in audit.alerts() if r["details"]["transition"] == "recovered"]
    assert [r["details"]["mode"] for r in recoveries] == ["delivered"] * 3
    assert len(fleet.triage_runs()) == 3


async def test_a_trigger_older_than_the_recovery_does_not_reopen_the_episode(
    fleet: Fleet, audit: Audit
) -> None:
    await fleet.deliver_webhook(_payload(transition="Recovered", event_id="2", date="20"))
    await fleet.deliver_webhook(_payload(event_id="1", date="10"))  # read late
    await fleet.deliver_webhook(_payload(event_id="3", date="30"))  # the next incident

    modes = [(r["details"]["date"], r["details"]["mode"]) for r in audit.alerts()]
    assert modes == [("20", "delivered"), ("10", "suppressed"), ("30", "delivered")]
    assert audit.alerts()[1]["details"]["delivery"]["reason"] == "superseded_by_recovery"
    assert [lvl for _, lvl, *_ in fleet.sent] == ["info", "warning"]
    assert len(fleet.triage_runs()) == 1


async def test_a_recovery_that_lands_during_the_triggers_send_wins(
    fleet: Fleet, audit: Audit
) -> None:
    """The trigger's page returns after its recovery was handled: the episode stays closed."""
    recovery = _webhook(_payload(transition="Recovered", event_id="2", date="20", tags="env:prod"))

    async def recovery_lands() -> None:
        await fleet.feed(ROOT, recovery)

    fleet.during_send = recovery_lands
    await fleet.feed(
        ROOT, _webhook(_payload(priority="P1", event_id="1", date="10", tags="env:prod"))
    )
    await fleet.feed(
        ROOT, _webhook(_payload(priority="P1", event_id="3", date="30", tags="env:prod"))
    )

    assert [lvl for _, lvl, *_ in fleet.sent] == ["critical", "info", "critical"]
    assert [r["details"]["mode"] for r in audit.alerts()][-1] == "delivered"
    assert len(fleet.triage_runs()) == 1, "the stale trigger starts no run; the new one does"


# ── the triage wait gives its claim back, and only its own ──────────


async def test_a_triage_wait_cancelled_by_shutdown_gives_the_claim_back(
    fleet: Fleet, audit: Audit, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setattr(dlv, "TRIAGE_WAIT_SECONDS", (30,))
    hooks = fleet.engines[ROOT]
    fleet.current = ROOT
    with patch("robothor.engine.dedup.try_acquire", new_callable=AsyncMock, return_value=False):
        await hooks._handle_event(
            STREAM, b"1-0", _webhook(_payload(tags="env:prod")), fleet.redis, "engine"
        )
        await asyncio.sleep(0)
        for task in list(dlv._triage_tasks):
            task.cancel()
        await asyncio.gather(*dlv._triage_tasks, return_exceptions=True)
    await get_event_writer().flush()

    assert _triage_key(ROOT) not in fleet.redis.keys
    runs = [r["details"]["status"] for r in audit.rows if r["event_type"] == "datadog.triage_run"]
    assert runs == ["cancelled"]


async def test_a_triage_wait_never_deletes_another_entrys_claim(fleet: Fleet, audit: Audit) -> None:
    key = _triage_key(ROOT)

    async def busy_while_the_episode_moves_on(_agent: str) -> bool:
        fleet.redis.keys[key] = "started:another-entry"
        return False

    with patch("robothor.engine.dedup.try_acquire", side_effect=busy_while_the_episode_moves_on):
        await fleet.deliver_webhook(_payload(tags="env:prod"))
    assert fleet.redis.keys[key] == "started:another-entry"


# ── a new incident of a P1 monitor always pages ─────────────────────


@pytest.fixture
def cooldown(fleet: Fleet):
    """The alerts cooldown, as the alerts path keeps it: an id sent is held."""
    from robothor.engine import alerts

    def held(alert_id: str) -> bool:
        return any(kw.get("alert_id") == alert_id for *_, kw in fleet.sent)

    with patch.object(alerts, "_suppressed", side_effect=held):
        yield


async def _incident_twice(fleet: Fleet, priority: str) -> None:
    for n, transition in enumerate(["Triggered", "Recovered", "Triggered"]):
        d = str(n + 1)
        await fleet.deliver_webhook(
            _payload(transition=transition, priority=priority, event_id=d, date=d)
        )


async def test_a_p1_monitor_pages_for_a_second_incident_inside_the_cooldown(
    fleet: Fleet, audit: Audit, cooldown: None
) -> None:
    await _incident_twice(fleet, "P1")

    assert [lvl for _, lvl, *_ in fleet.sent] == ["critical", "info", "critical"]
    pages = [kw["alert_id"] for _, lvl, _, _, kw in fleet.sent if lvl == "critical"]
    assert len(set(pages)) == 2, "each episode of a P1 monitor has its own alert id"
    assert [r["details"]["mode"] for r in audit.alerts()] == ["delivered"] * 3
    assert len(fleet.triage_runs()) == 2


async def test_a_non_p1_monitor_keeps_the_cooldown_for_a_second_incident(
    fleet: Fleet, audit: Audit, cooldown: None
) -> None:
    await _incident_twice(fleet, "P2")

    assert [lvl for _, lvl, *_ in fleet.sent] == ["warning", "info"]
    third = audit.alerts()[2]["details"]
    assert (third["mode"], third["delivery"]["reason"]) == ("suppressed", "cooldown")


async def test_a_p1_storm_inside_one_episode_still_pages_once(
    fleet: Fleet, audit: Audit, cooldown: None
) -> None:
    for n in range(10):
        transition = "Triggered" if n == 0 else "Re-Triggered"
        await fleet.deliver_webhook(
            _payload(transition=transition, priority="P1", event_id=str(n), date=str(n + 1))
        )
    assert [lvl for _, lvl, *_ in fleet.sent] == ["critical"]
    assert len(fleet.triage_runs()) == 1


async def test_a_recovery_older_than_the_latest_trigger_does_not_close_the_episode(
    fleet: Fleet, audit: Audit
) -> None:
    """Datadog's retry of a recovery lands after the next incident's trigger."""
    await fleet.deliver_webhook(_payload(event_id="1", date="10"))
    await fleet.deliver_webhook(_payload(transition="Re-Triggered", event_id="3", date="30"))
    await fleet.deliver_webhook(_payload(transition="Recovered", event_id="2", date="20"))
    await fleet.deliver_webhook(_payload(transition="Re-Triggered", event_id="4", date="40"))

    rows = audit.alerts()
    late = rows[2]["details"]
    assert (late["mode"], late["delivery"]["reason"]) == ("suppressed", "superseded_by_trigger")
    assert [lvl for _, lvl, *_ in fleet.sent] == ["warning"], "no false 'recovered'"
    assert rows[3]["details"]["mode"] == "suppressed", "the episode stayed open"
    assert len(fleet.triage_runs()) == 1

    await fleet.deliver_webhook(_payload(transition="Recovered", event_id="5", date="50"))
    assert [lvl for _, lvl, *_ in fleet.sent] == ["warning", "info"]


async def test_without_dates_a_new_p1_incident_still_pages(
    fleet: Fleet, audit: Audit, cooldown: None
) -> None:
    """A webhook template without $DATE: the recovery that closed the episode names it."""
    for n, transition in enumerate(["Triggered", "Recovered", "Triggered"]):
        await fleet.deliver_webhook(
            _payload(transition=transition, priority="P1", event_id=str(n), date="")
        )
    assert [lvl for _, lvl, *_ in fleet.sent] == ["critical", "info", "critical"]
