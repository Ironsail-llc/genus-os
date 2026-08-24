"""Tests for the sustained-outage detectors (robothor/engine/detectors.py).

Two live outages ran for weeks with nothing alerting:

* ``apollo_search_people`` failed 32/32 (error_type=auth) over 14 days. The
  1-hour ``tool_degradation_detector`` cannot see it — a tool called twice a
  day never reaches 5 failures in an hour, so a *total* outage is invisible
  precisely because it is total.
* Agents whose configured primary model was unreachable ran on a fallback for
  ten days straight; ``agent_runs`` recorded it and nothing read the column.

The DB-backed tests here run the real SQL against real rows with real
timestamps rather than asserting on a hand-made row list, because the whole
point of this phase is that a green test over a mocked dependency certifies
nothing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import detectors


@pytest.fixture(autouse=True)
def _clear_dedup():
    detectors._dedup.clear()
    yield
    detectors._dedup.clear()


def _seed_tool_events(
    cur,
    tool_name: str,
    *,
    calls: int,
    failures: int,
    span_hours: float,
    error_type: str = "auth",
    oldest_success_days: float | None = None,
) -> None:
    """Insert ``calls`` events for ``tool_name``, the first ``failures`` failing.

    Events are spread evenly over the last ``span_hours``. ``oldest_success_days``
    optionally adds one successful call that far back, which is how a real
    outage looks: a last-known-good, then nothing but errors.
    """
    if oldest_success_days is not None:
        cur.execute(
            "INSERT INTO agent_tool_events (tool_name, success, error_type, created_at) "
            "VALUES (%s, TRUE, NULL, NOW() - make_interval(secs => %s))",
            (tool_name, oldest_success_days * 86400),
        )
    for i in range(calls):
        age_secs = span_hours * 3600 * (calls - i) / (calls + 1)
        failed = i < failures
        cur.execute(
            "INSERT INTO agent_tool_events (tool_name, success, error_type, created_at) "
            "VALUES (%s, %s, %s, NOW() - make_interval(secs => %s))",
            (tool_name, not failed, error_type if failed else None, age_secs),
        )


def _seed_runs(
    cur,
    agent_id: str,
    *,
    runs: int,
    model_used: str,
    models_attempted: list[str] | None = None,
    age_hours: float = 12.0,
) -> None:
    """Insert ``runs`` completed agent_runs rows served by ``model_used``."""
    for _ in range(runs):
        cur.execute(
            """
            INSERT INTO agent_runs
                (tenant_id, agent_id, trigger_type, status, started_at,
                 model_used, models_attempted)
            VALUES (%s, %s, 'cron', 'completed',
                    NOW() - make_interval(secs => %s), %s, %s)
            """,
            (
                "default",
                agent_id,
                age_hours * 3600,
                model_used,
                models_attempted if models_attempted is not None else [model_used],
            ),
        )


# ── Tool outage: the SQL, against a real database ───────────────────────


@pytest.mark.integration
class TestCheckToolOutage:
    def test_total_outage_over_24h_is_flagged(self, db_cursor, mock_get_connection) -> None:
        tool = f"t_{uuid.uuid4().hex[:8]}"
        _seed_tool_events(db_cursor, tool, calls=10, failures=10, span_hours=20)

        rows = {r["tool_name"]: r for r in detectors.check_tool_outage()}

        assert tool in rows, "10/10 failures in 24h is a total outage and must be flagged"
        assert rows[tool]["failures"] == 10
        assert rows[tool]["total"] == 10
        assert rows[tool]["error_type"] == "auth"
        assert rows[tool]["severity"] == "warning"

    def test_half_failing_is_not_an_outage(self, db_cursor, mock_get_connection) -> None:
        tool = f"t_{uuid.uuid4().hex[:8]}"
        _seed_tool_events(db_cursor, tool, calls=10, failures=5, span_hours=20)

        rows = {r["tool_name"] for r in detectors.check_tool_outage()}

        assert tool not in rows, "50% failures is degradation, not an outage — 1h detector owns it"

    def test_below_volume_floor_does_not_fire(self, db_cursor, mock_get_connection) -> None:
        tool = f"t_{uuid.uuid4().hex[:8]}"
        _seed_tool_events(db_cursor, tool, calls=3, failures=3, span_hours=20)

        rows = {r["tool_name"] for r in detectors.check_tool_outage()}

        assert tool not in rows, "3 calls is noise, not evidence"

    def test_three_day_persistence_escalates_to_critical(
        self, db_cursor, mock_get_connection
    ) -> None:
        tool = f"t_{uuid.uuid4().hex[:8]}"
        # Last known-good 8 days ago, nothing but failures since — the shape of
        # the live apollo_search_people outage.
        _seed_tool_events(
            db_cursor,
            tool,
            calls=10,
            failures=10,
            span_hours=120,
            oldest_success_days=8,
        )

        rows = {r["tool_name"]: r for r in detectors.check_tool_outage()}

        assert tool in rows
        assert rows[tool]["severity"] == "critical"
        assert rows[tool]["outage_days"] >= 3

    def test_recovered_tool_is_not_flagged(self, db_cursor, mock_get_connection) -> None:
        """Failures that stopped are history, not an outage."""
        tool = f"t_{uuid.uuid4().hex[:8]}"
        _seed_tool_events(db_cursor, tool, calls=10, failures=10, span_hours=20)
        _seed_tool_events(db_cursor, tool, calls=10, failures=0, span_hours=2)

        rows = {r["tool_name"] for r in detectors.check_tool_outage()}

        assert tool not in rows


# ── Tool outage: alerting, suppression, escalation ──────────────────────


class TestToolOutageDetector:
    @pytest.mark.asyncio
    async def test_warning_alert_names_tool_count_and_error_type(self) -> None:
        flagged = [
            {
                "tool_name": "apollo_search_people",
                "total": 18,
                "failures": 18,
                "failure_rate": 1.0,
                "error_type": "auth",
                "outage_days": 1.4,
                "severity": "warning",
            }
        ]
        with (
            patch("robothor.engine.detectors.check_tool_outage", return_value=flagged),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=True)) as mock_alert,
        ):
            fired = await detectors.tool_outage_detector()

        assert fired == 1
        level, title, body = mock_alert.await_args.args
        assert level == "warning"
        assert "apollo_search_people" in title
        assert "18" in body
        assert "auth" in body

    @pytest.mark.asyncio
    async def test_persistent_outage_pages_critical(self) -> None:
        flagged = [
            {
                "tool_name": "apollo_search_people",
                "total": 18,
                "failures": 18,
                "failure_rate": 1.0,
                "error_type": "auth",
                "outage_days": 13.2,
                "severity": "critical",
            }
        ]
        with (
            patch("robothor.engine.detectors.check_tool_outage", return_value=flagged),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=True)) as mock_alert,
        ):
            fired = await detectors.tool_outage_detector()

        assert fired == 1
        assert mock_alert.await_args.args[0] == "critical"

    @pytest.mark.asyncio
    async def test_declared_outage_is_suppressed_with_its_reason(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv(
            "ROBOTHOR_DECLARED_TOOL_OUTAGES",
            "sms_send:carrier contract ended 2026-08-01",
        )
        flagged = [
            {
                "tool_name": "sms_send",
                "total": 20,
                "failures": 20,
                "failure_rate": 1.0,
                "error_type": "auth",
                "outage_days": 9.0,
                "severity": "critical",
            },
            {
                "tool_name": "apollo_search_people",
                "total": 18,
                "failures": 18,
                "failure_rate": 1.0,
                "error_type": "auth",
                "outage_days": 1.0,
                "severity": "warning",
            },
        ]
        with (
            patch("robothor.engine.detectors.check_tool_outage", return_value=flagged),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=True)) as mock_alert,
            caplog.at_level(logging.INFO, logger="robothor.engine.detectors"),
        ):
            fired = await detectors.tool_outage_detector()

        assert fired == 1, "only the undeclared outage should alert"
        assert "apollo_search_people" in mock_alert.await_args.args[1]
        assert any("carrier contract ended" in rec.message for rec in caplog.records), (
            "a suppression must state the declared reason, not vanish silently"
        )

    @pytest.mark.asyncio
    async def test_vision_tools_suppressed_while_service_disabled(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ROBOTHOR_MEMORY_DIR", str(tmp_path))
        monkeypatch.delenv("STATE_DIR", raising=False)
        monkeypatch.delenv("ROBOTHOR_DECLARED_TOOL_OUTAGES", raising=False)
        (tmp_path / "vision_mode.txt").write_text("disabled\n")

        flagged = [
            {
                "tool_name": "who_is_here",
                "total": 20,
                "failures": 20,
                "failure_rate": 1.0,
                "error_type": "unknown",
                "outage_days": 5.0,
                "severity": "critical",
            }
        ]
        with (
            patch("robothor.engine.detectors.check_tool_outage", return_value=flagged),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=True)) as mock_alert,
        ):
            fired = await detectors.tool_outage_detector()

        assert fired == 0
        mock_alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_escalation_survives_the_warning_dedup(self) -> None:
        """A warning already sent must not swallow the critical escalation."""
        warning = [
            {
                "tool_name": "apollo_search_people",
                "total": 18,
                "failures": 18,
                "failure_rate": 1.0,
                "error_type": "auth",
                "outage_days": 1.0,
                "severity": "warning",
            }
        ]
        critical = [dict(warning[0], outage_days=3.5, severity="critical")]
        with patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=True)) as mock_alert:
            with patch("robothor.engine.detectors.check_tool_outage", return_value=warning):
                assert await detectors.tool_outage_detector() == 1
            with patch("robothor.engine.detectors.check_tool_outage", return_value=critical):
                assert await detectors.tool_outage_detector() == 1
        assert [c.args[0] for c in mock_alert.await_args_list] == ["warning", "critical"]

    @pytest.mark.asyncio
    async def test_kill_switch_short_circuits(self, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_DETECTORS_ENABLED", "0")
        with patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert:
            assert await detectors.tool_outage_detector() == 0
            assert await detectors.primary_model_unreached_detector() == 0
            mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_delivery_logs_a_warning(self, caplog) -> None:
        flagged = [
            {
                "tool_name": "apollo_search_people",
                "total": 18,
                "failures": 18,
                "failure_rate": 1.0,
                "error_type": "auth",
                "outage_days": 1.0,
                "severity": "warning",
            }
        ]
        with (
            patch("robothor.engine.detectors.check_tool_outage", return_value=flagged),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=False)),
            caplog.at_level(logging.WARNING, logger="robothor.engine.detectors"),
        ):
            assert await detectors.tool_outage_detector() == 1
        assert any("delivery failed" in rec.message.lower() for rec in caplog.records)


# ── Primary-model loss ──────────────────────────────────────────────────


@pytest.mark.integration
class TestCheckPrimaryModelUnreached:
    def test_majority_fallback_is_flagged(self, db_cursor, mock_get_connection) -> None:
        agent = f"a_{uuid.uuid4().hex[:8]}"
        _seed_runs(db_cursor, agent, runs=12, model_used="deepseek/deepseek-v4-pro")

        rows = {
            r["agent_id"]: r
            for r in detectors.check_primary_model_unreached(
                primaries={agent: "openrouter/xiaomi/mimo-v2.5"}
            )
        }

        assert agent in rows
        assert rows[agent]["unreached_runs"] == 12
        assert rows[agent]["total_runs"] == 12
        assert rows[agent]["served"]["deepseek/deepseek-v4-pro"] == 12

    def test_primary_served_under_a_route_prefix_is_not_a_fallback(
        self, db_cursor, mock_get_connection
    ) -> None:
        """`model_used` is the provider's id — the `openrouter/` prefix is gone.

        Comparing the raw strings marks every healthy run a fallback, which
        would make the detector page constantly and get muted.
        """
        agent = f"a_{uuid.uuid4().hex[:8]}"
        _seed_runs(db_cursor, agent, runs=12, model_used="xiaomi/mimo-v2.5")

        rows = {
            r["agent_id"]
            for r in detectors.check_primary_model_unreached(
                primaries={agent: "openrouter/xiaomi/mimo-v2.5"}
            )
        }

        assert agent not in rows

    def test_dated_release_slug_is_not_a_fallback(self, db_cursor, mock_get_connection) -> None:
        agent = f"a_{uuid.uuid4().hex[:8]}"
        _seed_runs(db_cursor, agent, runs=12, model_used="xiaomi/mimo-v2.5-20260422")

        rows = {
            r["agent_id"]
            for r in detectors.check_primary_model_unreached(
                primaries={agent: "openrouter/xiaomi/mimo-v2.5"}
            )
        }

        assert agent not in rows

    def test_run_that_started_on_fallback_and_recovered_counts_as_reached(
        self, db_cursor, mock_get_connection
    ) -> None:
        agent = f"a_{uuid.uuid4().hex[:8]}"
        _seed_runs(
            db_cursor,
            agent,
            runs=12,
            model_used="xiaomi/mimo-v2.5",
            models_attempted=["deepseek/deepseek-v4-pro", "xiaomi/mimo-v2.5"],
        )

        rows = {
            r["agent_id"]
            for r in detectors.check_primary_model_unreached(
                primaries={agent: "openrouter/xiaomi/mimo-v2.5"}
            )
        }

        assert agent not in rows

    def test_below_run_floor_does_not_fire(self, db_cursor, mock_get_connection) -> None:
        agent = f"a_{uuid.uuid4().hex[:8]}"
        _seed_runs(db_cursor, agent, runs=4, model_used="deepseek/deepseek-v4-pro")

        rows = {
            r["agent_id"]
            for r in detectors.check_primary_model_unreached(
                primaries={agent: "openrouter/xiaomi/mimo-v2.5"}
            )
        }

        assert agent not in rows

    def test_minority_fallback_does_not_fire(self, db_cursor, mock_get_connection) -> None:
        agent = f"a_{uuid.uuid4().hex[:8]}"
        _seed_runs(db_cursor, agent, runs=18, model_used="xiaomi/mimo-v2.5")
        _seed_runs(db_cursor, agent, runs=2, model_used="deepseek/deepseek-v4-pro")

        rows = {
            r["agent_id"]
            for r in detectors.check_primary_model_unreached(
                primaries={agent: "openrouter/xiaomi/mimo-v2.5"}
            )
        }

        assert agent not in rows

    def test_agent_without_a_declared_primary_is_skipped(
        self, db_cursor, mock_get_connection
    ) -> None:
        agent = f"a_{uuid.uuid4().hex[:8]}"
        _seed_runs(db_cursor, agent, runs=12, model_used="deepseek/deepseek-v4-pro")

        rows = {r["agent_id"] for r in detectors.check_primary_model_unreached(primaries={})}

        assert agent not in rows

    def test_runs_predating_a_primary_switch_are_excluded(
        self, db_cursor, mock_get_connection
    ) -> None:
        """A fleet-wide model switch must not be judged by pre-switch traffic.

        2026-08-23: after every manifest primary moved to Ox Alpha, this
        detector spent its whole 7-day window flagging agents whose
        pre-switch runs never touched the new model. Runs older than the
        agent's recorded switch time are excluded from totals and misses.
        """
        agent = f"a_{uuid.uuid4().hex[:8]}"
        _seed_runs(
            db_cursor,
            agent,
            runs=12,
            model_used="deepseek/deepseek-v4-pro",
            age_hours=96.0,
        )

        switch_at = (datetime.now(UTC) - timedelta(hours=24)).isoformat()

        rows = {
            r["agent_id"]: r
            for r in detectors.check_primary_model_unreached(
                primaries={agent: "openrouter/stealth/ox-alpha"},
                primaries_changed_at={agent: switch_at},
            )
        }

        assert agent not in rows

    def test_post_switch_fallback_still_flags(self, db_cursor, mock_get_connection) -> None:
        """Only pre-switch runs are excused -- a live fallback still pages."""
        agent = f"a_{uuid.uuid4().hex[:8]}"
        _seed_runs(
            db_cursor,
            agent,
            runs=12,
            model_used="deepseek/deepseek-v4-pro",
            age_hours=2.0,
        )

        switch_at = (datetime.now(UTC) - timedelta(hours=24)).isoformat()

        rows = {
            r["agent_id"]: r
            for r in detectors.check_primary_model_unreached(
                primaries={agent: "openrouter/stealth/ox-alpha"},
                primaries_changed_at={agent: switch_at},
            )
        }

        assert rows[agent]["unreached_runs"] == 12


class TestPrimaryModelUnreachedDetector:
    @pytest.mark.asyncio
    async def test_alert_names_agent_primary_and_per_model_counts(self) -> None:
        flagged = [
            {
                "agent_id": "curiosity-engine",
                "primary": "openrouter/deepseek/deepseek-v4-pro",
                "total_runs": 234,
                "unreached_runs": 179,
                "unreached_share": 0.765,
                "served": {"xiaomi/mimo-v2.5": 180, "deepseek/deepseek-v4-pro": 54},
            }
        ]
        with (
            patch(
                "robothor.engine.detectors.check_primary_model_unreached",
                return_value=flagged,
            ),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=True)) as mock_alert,
        ):
            fired = await detectors.primary_model_unreached_detector()

        assert fired == 1
        level, title, body = mock_alert.await_args.args
        assert level == "warning"
        assert "curiosity-engine" in title
        assert "deepseek/deepseek-v4-pro" in body
        assert "xiaomi/mimo-v2.5=180" in body
        assert "179" in body

    @pytest.mark.asyncio
    async def test_dedup_suppresses_the_second_tick(self) -> None:
        flagged = [
            {
                "agent_id": "curiosity-engine",
                "primary": "openrouter/deepseek/deepseek-v4-pro",
                "total_runs": 234,
                "unreached_runs": 179,
                "unreached_share": 0.765,
                "served": {"xiaomi/mimo-v2.5": 180},
            }
        ]
        with (
            patch(
                "robothor.engine.detectors.check_primary_model_unreached",
                return_value=flagged,
            ),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=True)) as mock_alert,
        ):
            assert await detectors.primary_model_unreached_detector() == 1
            assert await detectors.primary_model_unreached_detector() == 0
        assert mock_alert.await_count == 1


class TestDetectorsAreActuallyScheduled:
    """A detector nobody calls is the exact failure this phase exists to end."""

    def test_daemon_watchdog_invokes_both_new_detectors(self) -> None:
        from pathlib import Path

        import robothor.engine.daemon as daemon_mod

        src = Path(daemon_mod.__file__).read_text()
        for name in ("tool_outage_detector", "primary_model_unreached_detector"):
            # `name(` is a call site; the import line ends the name with a comma.
            assert f"{name}(" in src, f"{name} is imported or defined but never called"
