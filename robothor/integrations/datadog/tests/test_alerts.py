"""Datadog monitor alerts into Genus: parse, route by ``org:`` tag, record (dry run)."""

from __future__ import annotations

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
    async def test_one_dry_run_event_in_the_tagged_tenant(self, audit, no_delivery) -> None:
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
        assert d["mode"] == "dry_run"
        assert d["routing"] == "org"
        assert d["would_notify"]
        assert d["would_run"]["prompt"]
        assert "12345" in d["would_run"]["prompt"]
        assert "read-only" in d["would_run"]["prompt"]
        assert "agent" in d["would_run"]

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
        assert "mode=dry_run" in line
        assert "agent@example.com" not in line
        assert "service:shop-api status:error" not in line, "the monitor query is not logged"


# ── would_notify / would_run ───────────────────────────────────────


class TestPlan:
    def test_a_trigger_would_page(self) -> None:
        plan = dd.would_notify("triggered")
        assert plan["level"] == "critical"
        assert plan["route"] == "page"
        assert "telegram" in plan["channels"]

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
            plan = dd.would_notify("triggered")
        finally:
            reset_settings()
        assert "email" in plan["channels"]
        assert "agent@example.com" not in repr(plan)

    def test_triage_plan_is_read_only_and_names_the_monitor(self) -> None:
        alert = dd.parse_alert(_payload())
        plan = dd.triage_plan(CHILD, alert)
        assert plan["tenant"] == CHILD
        assert "12345" in plan["prompt"]
        assert "read-only" in plan["prompt"]
        assert plan["read_only"] is True


# ── the mode setting ───────────────────────────────────────────────


class TestMode:
    def test_default_is_dry_run(self) -> None:
        from robothor.settings.model import IntegrationSettings

        assert IntegrationSettings().datadog_alerts_mode == "dry_run"
        extra = IntegrationSettings.model_fields["datadog_alerts_mode"].json_schema_extra
        assert extra["env"] == "ROBOTHOR_DATADOG_ALERTS_MODE"

    def test_deliver_is_refused_plainly(self) -> None:
        from pydantic import ValidationError

        from robothor.settings.model import IntegrationSettings

        with pytest.raises(ValidationError, match="delivery is not available yet"):
            IntegrationSettings(datadog_alerts_mode="deliver")

    def test_anything_else_is_refused(self) -> None:
        from pydantic import ValidationError

        from robothor.settings.model import IntegrationSettings

        with pytest.raises(ValidationError):
            IntegrationSettings(datadog_alerts_mode="observe")
