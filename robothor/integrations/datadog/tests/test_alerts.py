"""Datadog monitor alerts into Genus: parse, route by ``org:`` tag, record the delivery outcome."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.integrations.datadog import alerts as dd

CHILD = "acme-child-a"
OTHER = "acme-child-b"
INACTIVE = "acme-parent"

TENANTS: dict[str, dict[str, Any]] = {
    CHILD: {"id": CHILD, "active": True},
    OTHER: {"id": OTHER, "active": True},
    INACTIVE: {"id": INACTIVE, "active": False},
}


def _payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "monitor_id": "12345",
        "event_id": "777",
        "title": "[Triggered] shop-api error rate high",
        "transition": "Triggered",
        "status": "Alert",
        "priority": "P2",
        "query": 'logs("service:shop-api status:error").rollup("count").last("5m") > 10',
        "tags": "env:prod, org:acme-child-a,service:shop-api",
        "link": "https://app.example.com/monitors/12345",
        "date": "1764000000000",
        "hostname": "",
    }
    base.update(over)
    return base


class FakeRedis:
    """``SET NX EX`` and ``DELETE``: all the alert recorder asks of Redis."""

    def __init__(self) -> None:
        self.keys: dict[str, Any] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> Any:
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.keys.pop(key, None) is not None else 0

    async def exists(self, key: str) -> int:
        return 1 if key in self.keys else 0

    async def get(self, key: str) -> bytes | None:
        """As redis-py answers: bytes, or ``None`` for no key."""
        value = self.keys.get(key)
        return value.encode() if isinstance(value, str) else value


class FakeAudit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.fail = False

    def log_event(self, event_type: str, action: str, **kw: Any) -> dict[str, Any] | None:
        if self.fail:
            return None
        self.rows.append({"event_type": event_type, "action": action, **kw})
        return {"id": len(self.rows), "timestamp": "2026-11-25T00:00:00+00:00"}


@pytest.fixture
def audit():
    fake = FakeAudit()
    with patch("robothor.audit.logger.log_event", side_effect=fake.log_event):
        yield fake


@pytest.fixture(autouse=True)
def tenants():
    with patch("robothor.crm.dal.get_tenant", side_effect=lambda t: TENANTS.get(t)):
        yield


@pytest.fixture
def no_delivery():
    """Every way the alerts path could reach a person, watched."""
    with (
        patch("robothor.engine.alerts.alert", new_callable=AsyncMock) as alert,
        patch("robothor.engine.alerts._write_notification", new_callable=AsyncMock) as notify,
        patch("robothor.engine.alerts._send_telegram", new_callable=AsyncMock) as telegram,
        patch("robothor.engine.alerts._send_webhook", new_callable=AsyncMock) as webhook,
    ):
        yield (alert, notify, telegram, webhook)


# ── parsing ────────────────────────────────────────────────────────


class TestParse:
    def test_full_payload(self) -> None:
        a = dd.parse_alert(_payload())
        assert a.monitor_id == "12345"
        assert a.transition == "triggered"
        assert a.priority == "P2"
        assert a.tags == ("env:prod", "org:acme-child-a", "service:shop-api")
        assert a.link == "https://app.example.com/monitors/12345"
        assert a.date == "1764000000000"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Triggered", "triggered"),
            ("Re-Triggered", "re_triggered"),
            ("Recovered", "recovered"),
            ("Warn", "warn"),
            ("No Data", "no_data"),
            ("", ""),
        ],
    )
    def test_transition_is_normalised(self, raw: str, expected: str) -> None:
        assert dd.parse_alert(_payload(transition=raw)).transition == expected

    def test_tags_as_a_list(self) -> None:
        a = dd.parse_alert(_payload(tags=["org:acme-child-a", " env:prod "]))
        assert a.tags == ("org:acme-child-a", "env:prod")

    @pytest.mark.parametrize("body", [{}, [], "text", None, 42, {"monitor_id": None}])
    def test_missing_fields_are_empty_never_an_error(self, body: Any) -> None:
        a = dd.parse_alert(body)
        assert a.monitor_id == ""
        assert a.title == ""
        assert a.transition == ""
        assert a.tags == ()

    def test_monitor_id_falls_back_to_alert_id(self) -> None:
        body = _payload()
        del body["monitor_id"]
        body["alert_id"] = "999"
        assert dd.parse_alert(body).monitor_id == "999"


# ── tenant routing ─────────────────────────────────────────────────


class TestRouting:
    def test_org_tag_of_an_active_tenant(self) -> None:
        assert dd.route_tenant(("env:prod", f"org:{CHILD}")) == (CHILD, "org")

    def test_no_org_tag_goes_to_the_root_marked_untagged(self) -> None:
        assert dd.route_tenant(("env:prod",)) == (DEFAULT_TENANT, "untagged")

    def test_unknown_org_goes_to_the_root_marked(self) -> None:
        assert dd.route_tenant(("org:nobody",)) == (DEFAULT_TENANT, "unknown_org:nobody")

    def test_an_inactive_tenant_is_unknown(self) -> None:
        assert dd.route_tenant((f"org:{INACTIVE}",)) == (
            DEFAULT_TENANT,
            f"unknown_org:{INACTIVE}",
        )

    def test_two_different_org_tags_go_to_the_root(self) -> None:
        tenant, routing = dd.route_tenant((f"org:{CHILD}", f"org:{OTHER}"))
        assert tenant == DEFAULT_TENANT
        assert routing.startswith("ambiguous_org:")

    def test_the_same_org_tag_twice_is_one_org(self) -> None:
        assert dd.route_tenant((f"org:{CHILD}", f"org:{CHILD}")) == (CHILD, "org")

    def test_a_tenant_lookup_failure_goes_to_the_root(self) -> None:
        with patch("robothor.crm.dal.get_tenant", side_effect=ConnectionError("down")):
            tenant, routing = dd.route_tenant((f"org:{CHILD}",))
        assert tenant == DEFAULT_TENANT
        assert routing == f"unknown_org:{CHILD}"


# ── recording ──────────────────────────────────────────────────────


class TestRecord:
    @pytest.mark.asyncio
    async def test_without_a_deliverer_it_is_recorded_as_not_delivered(
        self, audit, no_delivery
    ) -> None:
        """This module alone cannot deliver: the engine hands it the deliverer."""
        outcome = await dd.handle_alert_event(_payload(), FakeRedis())

        assert outcome == "recorded"
        assert len(audit.rows) == 1
        row = audit.rows[0]
        assert row["event_type"] == "datadog.alert_received"
        assert row["tenant_id"] == CHILD
        d = row["details"]
        assert d["monitor_id"] == "12345"
        assert d["transition"] == "triggered"
        assert d["priority"] == "P2"
        assert d["tags"] == ["env:prod", "org:acme-child-a", "service:shop-api"]
        assert d["link"] == "https://app.example.com/monitors/12345"
        assert d["mode"] == "delivery_failed"
        assert d["delivery"] == {"reason": "not_available"}
        assert d["routing"] == "org"
        assert d["would_notify"]["route"] == "digest"
        assert d["would_run"]["agent"] is None

        for watched in no_delivery:
            watched.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_agent_run_is_started(self, audit, no_delivery) -> None:
        with patch("robothor.engine.runner.AgentRunner.execute", new_callable=AsyncMock) as execute:
            await dd.handle_alert_event(_payload(), FakeRedis())
        execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_untagged_alert_is_recorded_at_the_root(self, audit, no_delivery) -> None:
        await dd.handle_alert_event(_payload(tags="env:prod"), FakeRedis())
        assert audit.rows[0]["tenant_id"] == DEFAULT_TENANT
        assert audit.rows[0]["details"]["routing"] == "untagged"

    @pytest.mark.asyncio
    async def test_unknown_org_is_recorded_at_the_root(self, audit, no_delivery) -> None:
        await dd.handle_alert_event(_payload(tags="org:nobody"), FakeRedis())
        assert audit.rows[0]["tenant_id"] == DEFAULT_TENANT
        assert audit.rows[0]["details"]["routing"] == "unknown_org:nobody"

    @pytest.mark.asyncio
    async def test_no_other_field_decides_the_tenant(self, audit, no_delivery) -> None:
        body = _payload(tags="env:prod", tenant_id=CHILD, org=CHILD, hostname=f"org:{CHILD}")
        await dd.handle_alert_event(body, FakeRedis())
        assert audit.rows[0]["tenant_id"] == DEFAULT_TENANT

    @pytest.mark.asyncio
    async def test_recovery_is_its_own_event_with_the_same_monitor(
        self, audit, no_delivery
    ) -> None:
        redis = FakeRedis()
        await dd.handle_alert_event(_payload(), redis)
        await dd.handle_alert_event(
            _payload(transition="Recovered", title="[Recovered] shop-api error rate high"), redis
        )
        assert [r["details"]["transition"] for r in audit.rows] == ["triggered", "recovered"]
        assert {r["details"]["monitor_id"] for r in audit.rows} == {"12345"}
        assert {r["tenant_id"] for r in audit.rows} == {CHILD}

    @pytest.mark.asyncio
    async def test_a_retried_delivery_records_once(self, audit, no_delivery) -> None:
        redis = FakeRedis()
        assert await dd.handle_alert_event(_payload(), redis) == "recorded"
        assert await dd.handle_alert_event(_payload(), redis) == "duplicate"
        assert len(audit.rows) == 1

    @pytest.mark.asyncio
    async def test_two_groups_of_one_monitor_in_one_evaluation_are_two_events(
        self, audit, no_delivery
    ) -> None:
        """A multi-alert monitor: same monitor, transition and date, a different ``$ID``."""
        redis = FakeRedis()
        await dd.handle_alert_event(_payload(event_id="777"), redis)
        await dd.handle_alert_event(_payload(event_id="778"), redis)
        assert len(audit.rows) == 2

    @pytest.mark.asyncio
    async def test_an_empty_date_does_not_merge_later_notifications(
        self, audit, no_delivery
    ) -> None:
        redis = FakeRedis()
        await dd.handle_alert_event(_payload(date="", event_id="777"), redis)
        await dd.handle_alert_event(_payload(date="", event_id="901"), redis)
        assert len(audit.rows) == 2

    @pytest.mark.asyncio
    async def test_a_retry_carries_the_same_event_id_and_records_once(
        self, audit, no_delivery
    ) -> None:
        redis = FakeRedis()
        await dd.handle_alert_event(_payload(date=""), redis)
        assert await dd.handle_alert_event(_payload(date=""), redis) == "duplicate"
        assert len(audit.rows) == 1

    @pytest.mark.asyncio
    async def test_without_event_id_or_date_one_transition_per_monitor_is_kept(
        self, audit, no_delivery
    ) -> None:
        """Pinned: with neither ``$ID`` nor ``$DATE`` only (monitor, transition) is left."""
        redis = FakeRedis()
        await dd.handle_alert_event(_payload(date="", event_id=""), redis)
        assert await dd.handle_alert_event(_payload(date="", event_id=""), redis) == "duplicate"
        assert len(audit.rows) == 1

    @pytest.mark.asyncio
    async def test_the_same_transition_at_another_date_is_a_new_event(
        self, audit, no_delivery
    ) -> None:
        redis = FakeRedis()
        await dd.handle_alert_event(_payload(), redis)
        await dd.handle_alert_event(_payload(date="1764000999000"), redis)
        assert len(audit.rows) == 2

    @pytest.mark.asyncio
    async def test_an_email_in_the_title_is_masked(self, audit, no_delivery) -> None:
        await dd.handle_alert_event(
            _payload(title="Login failures for agent@example.com"), FakeRedis()
        )
        stored = audit.rows[0]
        assert "agent@example.com" not in repr(stored)
        assert "<email>" in stored["details"]["title"]

    @pytest.mark.asyncio
    async def test_an_empty_body_is_recorded_with_empty_fields(self, audit, no_delivery) -> None:
        assert await dd.handle_alert_event({}, FakeRedis()) == "recorded"
        row = audit.rows[0]
        assert row["tenant_id"] == DEFAULT_TENANT
        assert row["details"]["monitor_id"] == ""
        assert row["details"]["routing"] == "untagged"

    @pytest.mark.asyncio
    async def test_a_failed_write_raises_and_lets_the_retry_record(
        self, audit, no_delivery
    ) -> None:
        redis = FakeRedis()
        audit.fail = True
        with pytest.raises(dd.AlertRecordError):
            await dd.handle_alert_event(_payload(), redis)
        audit.fail = False
        assert await dd.handle_alert_event(_payload(), redis) == "recorded"
        assert len(audit.rows) == 1

    @pytest.mark.asyncio
    async def test_redis_unavailable_still_records(self, audit, no_delivery) -> None:
        """No dedup store is a weaker filter, not a lost alert."""
        assert await dd.handle_alert_event(_payload(), None) == "recorded"
        assert len(audit.rows) == 1

    @pytest.mark.asyncio
    async def test_one_log_line_with_no_body(self, audit, no_delivery, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="robothor.integrations.datadog.alerts"):
            await dd.handle_alert_event(
                _payload(title="Login failures for agent@example.com"), FakeRedis()
            )
        lines = [r.getMessage() for r in caplog.records if "event=datadog_alert" in r.getMessage()]
        assert len(lines) == 1
        line = lines[0]
        assert f"tenant={CHILD}" in line
        assert "monitor_id=12345" in line
        assert "transition=triggered" in line
        assert "mode=delivery_failed" in line
        assert "agent@example.com" not in line
        assert "service:shop-api status:error" not in line, "the monitor query is not logged"


# ── would_notify / would_run ───────────────────────────────────────


class TestPlan:
    @pytest.mark.parametrize("priority", ["P1", "p1", "1"])
    @pytest.mark.parametrize("transition", ["triggered", "re_triggered"])
    def test_a_p1_trigger_would_page(self, transition: str, priority: str) -> None:
        plan = dd.would_notify(transition, priority)
        assert plan["level"] == "critical"
        assert plan["route"] == "page"
        assert "telegram" in plan["channels"]

    @pytest.mark.parametrize("priority", ["P2", "P5", "", "P11", "urgent"])
    def test_any_other_trigger_would_go_to_the_digest(self, priority: str) -> None:
        plan = dd.would_notify("triggered", priority)
        assert plan["level"] == "warning"
        assert plan["route"] == "digest"

    @pytest.mark.parametrize(
        ("transition", "level"),
        [("warn", "warning"), ("no_data", "warning"), ("recovered", "info")],
    )
    def test_priority_changes_nothing_else(self, transition: str, level: str) -> None:
        assert dd.level_for(transition, "P1") == level

    @pytest.mark.parametrize("transition", ["warn", "no_data", "recovered"])
    def test_the_rest_would_go_to_the_digest(self, transition: str) -> None:
        plan = dd.would_notify(transition)
        assert plan["route"] == "digest"
        assert plan["level"] in ("warning", "info")

    def test_fallback_channels_are_named_not_their_targets(self, monkeypatch) -> None:
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_ALERT_FALLBACK_CHANNELS", "email:agent@example.com")
        reset_settings()
        try:
            plan = dd.would_notify("triggered", "P1")
        finally:
            reset_settings()
        assert "email" in plan["channels"]
        assert "agent@example.com" not in repr(plan)

    def test_the_triage_prompt_is_fixed_short_and_names_the_monitor(self) -> None:
        alert = dd.parse_alert(_payload())
        prompt = dd.triage_prompt(alert, title="shop-api error rate high")
        assert "12345" in prompt and "shop-api error rate high" in prompt
        assert "read-only" in prompt
        assert "Datadog tools" in prompt
        assert "do not have the Datadog tools" in prompt, "a missing tool set is reported"
        assert len(prompt) < 600
        assert "service:shop-api status:error" not in prompt, "never the monitor query"


# ── the mode setting ───────────────────────────────────────────────


class TestNoModeSetting:
    """Delivery is the only code path; no setting switches it."""

    def test_no_mode_setting_is_declared(self) -> None:
        from robothor.settings.model import IntegrationSettings
        from robothor.settings.registry import declared_env_names

        assert "datadog_alerts_mode" not in IntegrationSettings.model_fields
        assert "ROBOTHOR_DATADOG_ALERTS_MODE" not in declared_env_names()

    @pytest.mark.asyncio
    async def test_an_old_mode_variable_changes_nothing(self, audit, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_DATADOG_ALERTS_MODE", "dry_run")
        deliver = AsyncMock(return_value={"mode": "delivered", "delivery": {"level": "critical"}})
        await dd.handle_alert_event(_payload(), FakeRedis(), deliver=deliver)
        deliver.assert_awaited_once()
        assert audit.rows[0]["details"]["mode"] == "delivered"


class TestDeliverer:
    """The engine's deliverer decides the outcome; this module records it."""

    @pytest.mark.asyncio
    async def test_the_outcome_is_recorded_in_the_routed_tenant(self, audit) -> None:
        outcome = {
            "mode": "delivered",
            "delivery": {"level": "critical", "route": "page"},
            "triage": {"started": True},
        }
        deliver = AsyncMock(return_value=outcome)
        assert await dd.handle_alert_event(_payload(), FakeRedis(), deliver=deliver) == "recorded"
        alert, tenant, routing, title = deliver.await_args.args
        assert (alert.monitor_id, tenant, routing) == ("12345", CHILD, "org")
        d = audit.rows[0]["details"]
        assert d["mode"] == "delivered"
        assert d["delivery"] == outcome["delivery"]
        assert d["triage"] == outcome["triage"]
        # The fields the existing dashboard reader shows, kept.
        assert d["would_notify"] == dd.would_notify("triggered", "P2")
        assert d["would_run"]["tenant"] == CHILD
        assert d["would_run"]["read_only"] is True
        assert "12345" in d["would_run"]["prompt"]

    @pytest.mark.asyncio
    async def test_a_raising_deliverer_is_recorded_as_failed(self, audit) -> None:
        deliver = AsyncMock(side_effect=RuntimeError("boom"))
        assert await dd.handle_alert_event(_payload(), FakeRedis(), deliver=deliver) == "recorded"
        d = audit.rows[0]["details"]
        assert d["mode"] == "delivery_failed"
        assert d["delivery"]["error"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_a_hand_off_is_not_recorded_here_and_is_remembered(self, audit) -> None:
        redis = FakeRedis()
        deliver = AsyncMock(return_value={"mode": "handed_off", "handoff": {"tenant": CHILD}})
        assert await dd.handle_alert_event(_payload(), redis, deliver=deliver) == "handed_off"
        assert audit.rows == []
        assert await dd.handle_alert_event(_payload(), redis, deliver=deliver) == "duplicate"
        deliver.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_refused_alert_is_neither_recorded_nor_remembered(self, audit) -> None:
        redis = FakeRedis()
        deliver = AsyncMock(return_value={"mode": "refused"})
        assert await dd.handle_alert_event(_payload(), redis, deliver=deliver) == "refused"
        assert audit.rows == [] and redis.keys == {}

    @pytest.mark.asyncio
    async def test_a_dedup_scope_keeps_its_own_keys(self, audit) -> None:
        redis = FakeRedis()
        deliver = AsyncMock(return_value={"mode": "handed_off"})
        await dd.handle_alert_event(_payload(), redis, deliver=deliver)
        here = AsyncMock(return_value={"mode": "delivered", "delivery": {}})
        assert (
            await dd.handle_alert_event(_payload(), redis, deliver=here, dedup_scope="t:" + CHILD)
            == "recorded"
        )
        here.assert_awaited_once()


class TestClaimWriteExtend:
    """Claim with a short pending key, write the event, then keep the key for 24 hours.

    A second copy that finds the claim is a duplicate, even while the first is
    still writing. A failed write deletes the claim so a retry can record.
    """

    @pytest.mark.asyncio
    async def test_two_copies_at_once_record_once(self, audit, no_delivery) -> None:
        redis = FakeRedis()
        outcomes = await asyncio.gather(
            dd.handle_alert_event(_payload(), redis), dd.handle_alert_event(_payload(), redis)
        )
        assert sorted(outcomes) == ["duplicate", "recorded"]
        assert len(audit.rows) == 1

    @pytest.mark.asyncio
    async def test_the_claim_is_short_lived_while_the_event_is_written(self, no_delivery) -> None:
        redis = FakeRedis()
        seen: list[dict[str, int]] = []

        def _log_event(event_type: str, action: str, **kw: Any) -> dict[str, Any]:
            seen.append(dict(redis.ttls))
            return {"id": 1, "timestamp": "2026-11-25T00:00:00+00:00"}

        with patch("robothor.audit.logger.log_event", side_effect=_log_event):
            await dd.handle_alert_event(_payload(), redis)

        assert [list(t.values()) for t in seen] == [[dd.PENDING_TTL_SECONDS]]
        assert dd.PENDING_TTL_SECONDS < dd.DEDUP_TTL_SECONDS
        assert list(redis.ttls.values()) == [dd.DEDUP_TTL_SECONDS], "extended after the write"

    @pytest.mark.asyncio
    async def test_redis_down_records_every_copy(self, audit, no_delivery) -> None:
        """No dedup store: each copy is recorded, none is lost."""

        class SetFails(FakeRedis):
            async def set(self, *a: Any, **kw: Any) -> Any:
                raise ConnectionError("redis unreachable")

        redis = SetFails()
        assert await dd.handle_alert_event(_payload(), redis) == "recorded"
        assert await dd.handle_alert_event(_payload(), redis) == "recorded"
        assert len(audit.rows) == 2, "a duplicate, not a lost alert"

    @pytest.mark.asyncio
    async def test_a_crash_between_the_write_and_the_extension(self, audit, no_delivery) -> None:
        """The claim works, the extension never happens: the pending claim still holds
        for its window, and once it lapses one more copy can be recorded."""

        class ExtendFails(FakeRedis):
            async def set(self, key: str, value: Any, nx: bool = False, ex: Any = None) -> Any:
                if not nx:
                    raise ConnectionError("engine went away before the extension")
                return await super().set(key, value, nx=nx, ex=ex)

        redis = ExtendFails()
        assert await dd.handle_alert_event(_payload(), redis, message_id="1-0") == "recorded"
        assert list(redis.ttls.values()) == [dd.PENDING_TTL_SECONDS], "never extended"
        # A Datadog retry inside the pending window is another stream message.
        assert await dd.handle_alert_event(_payload(), redis, message_id="2-0") == "duplicate"
        # The pending window passes and the claim lapses.
        redis.keys.clear()
        redis.ttls.clear()
        assert await dd.handle_alert_event(_payload(), redis, message_id="3-0") == "recorded"
        assert len(audit.rows) == 2, "at most one duplicate after the window"

    @pytest.mark.asyncio
    async def test_a_failed_write_sets_no_key(self, audit, no_delivery) -> None:
        redis = FakeRedis()
        audit.fail = True
        with pytest.raises(dd.AlertRecordError):
            await dd.handle_alert_event(_payload(), redis)
        assert redis.keys == {}


class TestClaimOwnedByTheStreamMessage:
    """The pending claim names the stream message that made it.

    A crash between the claim and the write, followed by the consumer group
    redelivering THAT message, records the alert; any other copy (a Datadog
    retry is another stream message) is a duplicate.
    """

    @pytest.mark.asyncio
    async def test_redelivery_after_a_crash_before_the_write_records_once(
        self, audit, no_delivery
    ) -> None:
        redis = FakeRedis()
        # The engine claimed message 1-0, then died before the audit write:
        # the claim is still there, pending, and nothing was recorded.
        key = dd._dedup_key(dd.parse_alert(_payload()), _payload())
        redis.keys[key] = "1-0"
        redis.ttls[key] = dd.PENDING_TTL_SECONDS

        # On restart the consumer group redelivers the same message.
        assert await dd.handle_alert_event(_payload(), redis, message_id="1-0") == "recorded"
        assert await dd.handle_alert_event(_payload(), redis, message_id="1-0") == "duplicate"
        assert len(audit.rows) == 1
        assert list(redis.keys.values()) == ["done"]
        assert list(redis.ttls.values()) == [dd.DEDUP_TTL_SECONDS]

    @pytest.mark.asyncio
    async def test_another_message_with_the_same_key_is_a_duplicate(
        self, audit, no_delivery
    ) -> None:
        redis = FakeRedis()
        key = dd._dedup_key(dd.parse_alert(_payload()), _payload())
        redis.keys[key] = "1-0"  # claimed by another message, still pending
        assert await dd.handle_alert_event(_payload(), redis, message_id="2-0") == "duplicate"
        assert audit.rows == []

    @pytest.mark.asyncio
    async def test_a_done_key_is_a_duplicate_even_for_the_same_message(
        self, audit, no_delivery
    ) -> None:
        redis = FakeRedis()
        assert await dd.handle_alert_event(_payload(), redis, message_id="1-0") == "recorded"
        assert await dd.handle_alert_event(_payload(), redis, message_id="2-0") == "duplicate"
        assert await dd.handle_alert_event(_payload(), redis, message_id="1-0") == "duplicate"
        assert len(audit.rows) == 1

    @pytest.mark.asyncio
    async def test_the_pending_claim_holds_the_message_id(self, no_delivery) -> None:
        redis = FakeRedis()
        seen: list[dict[str, Any]] = []

        def _log_event(event_type: str, action: str, **kw: Any) -> dict[str, Any]:
            seen.append(dict(redis.keys))
            return {"id": 1, "timestamp": "2026-11-25T00:00:00+00:00"}

        with patch("robothor.audit.logger.log_event", side_effect=_log_event):
            await dd.handle_alert_event(_payload(), redis, message_id="7-3")
        assert [list(k.values()) for k in seen] == [["7-3"]]

    @pytest.mark.asyncio
    async def test_two_messages_at_once_record_once(self, audit, no_delivery) -> None:
        redis = FakeRedis()
        outcomes = await asyncio.gather(
            dd.handle_alert_event(_payload(), redis, message_id="1-0"),
            dd.handle_alert_event(_payload(), redis, message_id="2-0"),
        )
        assert sorted(outcomes) == ["duplicate", "recorded"]
        assert len(audit.rows) == 1

    @pytest.mark.asyncio
    async def test_a_failed_write_releases_the_claim(self, audit, no_delivery) -> None:
        redis = FakeRedis()
        audit.fail = True
        with pytest.raises(dd.AlertRecordError):
            await dd.handle_alert_event(_payload(), redis, message_id="1-0")
        assert redis.keys == {}
        audit.fail = False
        assert await dd.handle_alert_event(_payload(), redis, message_id="2-0") == "recorded"
