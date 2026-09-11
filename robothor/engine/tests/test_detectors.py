"""Tests for the failure-mode detectors (robothor/engine/detectors.py).

The detectors are read-only observers that fire Telegram alerts. Tests
cover: dedup behavior, env kill-switch, and the alerting logic paired with
mocked-out DB queries.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import detectors


@pytest.fixture(autouse=True)
def _clear_dedup():
    detectors._dedup.clear()
    yield
    detectors._dedup.clear()


class TestShouldFire:
    def test_first_fire_returns_true(self) -> None:
        assert detectors._should_fire("abc") is True

    def test_same_fingerprint_within_ttl_suppressed(self) -> None:
        detectors._should_fire("abc")
        assert detectors._should_fire("abc") is False

    def test_different_fingerprints_independent(self) -> None:
        detectors._should_fire("a")
        assert detectors._should_fire("b") is True

    def test_after_ttl_expiry_fires_again(self) -> None:
        detectors._should_fire("x")
        # Fast-forward the recorded time past the TTL
        detectors._dedup["x"] = time.time() - detectors._DEDUP_TTL_SECONDS - 1
        assert detectors._should_fire("x") is True


class TestEnvKillSwitch:
    @pytest.mark.asyncio
    async def test_env_disable_short_circuits_all(self, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_DETECTORS_ENABLED", "0")
        assert detectors.detectors_enabled() is False
        # With the kill switch on, no detector should call alert() or query
        with patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert:
            assert await detectors.repeat_error_detector() == 0
            assert await detectors.tool_degradation_detector() == 0
            assert await detectors.runaway_burn_detector() == 0
            assert await detectors.zombie_runner_detector() == 0
            mock_alert.assert_not_called()


class TestRepeatErrorDetector:
    @pytest.mark.asyncio
    async def test_fires_on_cluster_above_threshold(self) -> None:
        patterns = {
            "patterns": [
                {
                    "agent_id": "main",
                    "error_type": "timeout",
                    "count": 5,
                    "last_occurrence": "2026-04-23T00:00:00",
                    "sample_messages": ["some error msg"],
                },
                {
                    "agent_id": "crm-enrichment",
                    "error_type": "other",
                    "count": 1,  # below threshold=3
                },
            ]
        }
        with (
            patch(
                "robothor.engine.analytics.get_failure_patterns",
                return_value=patterns,
            ),
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
        ):
            fired = await detectors.repeat_error_detector()
        assert fired == 1
        mock_alert.assert_awaited_once()
        (level, title, body) = mock_alert.await_args.args
        assert level == "warning"
        assert "main" in title
        assert "timeout" in body

    @pytest.mark.asyncio
    async def test_dedup_suppresses_repeat_alerts(self) -> None:
        patterns = {
            "patterns": [
                {
                    "agent_id": "main",
                    "error_type": "timeout",
                    "count": 5,
                    "sample_messages": ["x"],
                },
            ]
        }
        with (
            patch(
                "robothor.engine.analytics.get_failure_patterns",
                return_value=patterns,
            ),
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
        ):
            # First call fires
            assert await detectors.repeat_error_detector() == 1
            # Second call within TTL is deduped
            assert await detectors.repeat_error_detector() == 0
        assert mock_alert.await_count == 1


class TestToolDegradationDetector:
    @pytest.mark.asyncio
    async def test_fires_on_volume_threshold(self) -> None:
        flagged = [
            {
                "tool_name": "log_interaction",
                "total": 8,
                "failures": 7,
                "failure_rate": 0.875,
            },
        ]
        with (
            patch(
                "robothor.engine.detectors.check_tool_degradation",
                return_value=flagged,
            ),
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
        ):
            fired = await detectors.tool_degradation_detector()
        assert fired == 1
        mock_alert.assert_awaited_once()
        assert "log_interaction" in mock_alert.await_args.args[1]

    @pytest.mark.asyncio
    async def test_vision_tools_suppressed_when_vision_disabled(
        self, tmp_path, monkeypatch
    ) -> None:
        """No alerts for tools whose backing service is administratively off.

        The vision service persists its mode to <state_dir>/vision_mode.txt;
        'disabled' means the operator turned it off on purpose (thermal), so
        paging about who_is_here failing 5/5 is noise the operator cannot act
        on. Non-vision tools must still fire.
        """
        monkeypatch.setenv("ROBOTHOR_MEMORY_DIR", str(tmp_path))
        monkeypatch.delenv("STATE_DIR", raising=False)
        (tmp_path / "vision_mode.txt").write_text("disabled\n")

        flagged = [
            {"tool_name": "who_is_here", "total": 5, "failures": 5, "failure_rate": 1.0},
            {"tool_name": "look", "total": 6, "failures": 6, "failure_rate": 1.0},
            {"tool_name": "read_file", "total": 63, "failures": 40, "failure_rate": 0.635},
        ]
        with (
            patch("robothor.engine.detectors.check_tool_degradation", return_value=flagged),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=True)) as mock_alert,
        ):
            fired = await detectors.tool_degradation_detector()

        assert fired == 1, "only the non-vision tool should alert"
        mock_alert.assert_awaited_once()
        assert "read_file" in mock_alert.await_args.args[1]

    @pytest.mark.asyncio
    async def test_vision_tools_fire_when_vision_not_disabled(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_MEMORY_DIR", str(tmp_path))
        monkeypatch.delenv("STATE_DIR", raising=False)
        (tmp_path / "vision_mode.txt").write_text("armed")

        flagged = [
            {"tool_name": "who_is_here", "total": 5, "failures": 5, "failure_rate": 1.0},
        ]
        with (
            patch("robothor.engine.detectors.check_tool_degradation", return_value=flagged),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=True)) as mock_alert,
        ):
            fired = await detectors.tool_degradation_detector()

        assert fired == 1
        mock_alert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_vision_tools_fire_when_mode_file_absent(self, tmp_path, monkeypatch) -> None:
        """No mode file = service state unknown = do not suppress."""
        monkeypatch.setenv("ROBOTHOR_MEMORY_DIR", str(tmp_path))
        monkeypatch.delenv("STATE_DIR", raising=False)

        flagged = [
            {"tool_name": "who_is_here", "total": 5, "failures": 5, "failure_rate": 1.0},
        ]
        with (
            patch("robothor.engine.detectors.check_tool_degradation", return_value=flagged),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=True)) as mock_alert,
        ):
            fired = await detectors.tool_degradation_detector()

        assert fired == 1
        mock_alert.assert_awaited_once()


class TestFailedDeliveryLogsWarning:
    """alert() returning False must not vanish — detectors log a warning."""

    @pytest.mark.asyncio
    async def test_tool_degradation_warns_when_alert_delivery_fails(self, caplog) -> None:
        import logging

        flagged = [
            {"tool_name": "read_file", "total": 20, "failures": 15, "failure_rate": 0.75},
        ]
        with (
            patch("robothor.engine.detectors.check_tool_degradation", return_value=flagged),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=False)),
            caplog.at_level(logging.WARNING, logger="robothor.engine.detectors"),
        ):
            fired = await detectors.tool_degradation_detector()

        assert fired == 1
        assert any("delivery failed" in rec.message.lower() for rec in caplog.records), (
            "a dropped alert must at least leave a warning in the journal"
        )

    @pytest.mark.asyncio
    async def test_repeat_error_warns_when_alert_delivery_fails(self, caplog) -> None:
        import logging

        patterns = {
            "patterns": [
                {"agent_id": "main", "error_type": "timeout", "count": 5},
            ]
        }
        with (
            patch("robothor.engine.analytics.get_failure_patterns", return_value=patterns),
            patch("robothor.engine.alerts.alert", new=AsyncMock(return_value=False)),
            caplog.at_level(logging.WARNING, logger="robothor.engine.detectors"),
        ):
            fired = await detectors.repeat_error_detector()

        assert fired == 1
        assert any("delivery failed" in rec.message.lower() for rec in caplog.records)


class TestRunawayBurnDetector:
    @pytest.mark.asyncio
    async def test_fires_for_each_hot_run_once(self) -> None:
        hot = [
            {
                "id": "run-1",
                "agent_id": "main",
                "model_used": "claude-sonnet",
                "input_tokens": 600_000,
                "output_tokens": 50_000,
                "started_at": "2026-04-23T00:00:00",
                "elapsed_s": 300,
            },
        ]
        with (
            patch("robothor.engine.detectors.check_runaway_burn", return_value=hot),
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
        ):
            assert await detectors.runaway_burn_detector() == 1
            assert await detectors.runaway_burn_detector() == 0  # deduped
        assert mock_alert.await_count == 1
        assert (
            "650,000" in mock_alert.await_args.args[2] or "run-1" in mock_alert.await_args.args[2]
        )


class TestZombieRunnerDetector:
    @pytest.mark.asyncio
    async def test_fires_for_zombie_runs(self) -> None:
        zombies = [
            {
                "id": "run-xyz",
                "agent_id": "buddy",
                "started_at": "2026-04-23T09:00:00",
                "age_s": 1200,
                "last_step_at": None,
            }
        ]
        with (
            patch("robothor.engine.detectors.check_zombie_runners", return_value=zombies),
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
        ):
            assert await detectors.zombie_runner_detector() == 1
        mock_alert.assert_awaited_once()
        body = mock_alert.await_args.args[2]
        assert "run-xyz" in body
        assert "buddy" in body


class TestCheckToolDegradationExcludesSandboxDenials:
    """Sandbox refusals are the benchmark harness working as designed, not a
    broken tool: measured 2026-09-11 15:08, ``agent_tool_events`` in the last
    75 minutes showed ``create_task`` 10 ok / 14 failed, every failure's
    ``error_message`` = "benchmark sandbox: create_task writes are disabled"
    (robothor/engine/tools/handlers/crm.py:88). ``check_tool_degradation``
    counted every ``success = false`` row and paged "Tool degradation:
    create_task" for a tool that was never broken.
    """

    class _Cur:
        def __init__(self, rows, sink):
            self._rows = rows
            self._sink = sink

        def execute(self, sql, params=None):
            self._sink.append((sql, params))

        def fetchall(self):
            return self._rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self, rows, sink):
            self._rows = rows
            self._sink = sink

        def cursor(self, *a, **k):
            return TestCheckToolDegradationExcludesSandboxDenials._Cur(self._rows, self._sink)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_the_sql_excludes_sandbox_denied_rows(self) -> None:
        sink: list = []
        conn = self._Conn([], sink)
        with patch("robothor.db.connection.get_connection", return_value=conn):
            detectors.check_tool_degradation()

        sql, params = sink[0]
        assert "error_type" in sql and ("<>" in sql or "!=" in sql or "NOT IN" in sql.upper()), (
            "the SQL must filter agent_tool_events on error_type, or a tool "
            "correctly refused by the benchmark sandbox pages as broken"
        )
        assert "sandbox_denied" in params, (
            "the excluded error_type must be sandbox_denied — bound as a "
            "parameter, per this codebase's parameterized-query convention"
        )

    def test_a_tool_whose_only_failures_are_sandbox_denials_is_not_flagged(self) -> None:
        """The row the (fixed) SQL returns for create_task once sandbox_denied
        rows are excluded from both COUNT(*) and the failure SUM: 10 real
        calls, all successful, 14 sandbox refusals nowhere in the aggregate."""
        sink: list = []
        rows = [{"tool_name": "create_task", "total": 10, "failures": 0}]
        conn = self._Conn(rows, sink)
        with patch("robothor.db.connection.get_connection", return_value=conn):
            flagged = detectors.check_tool_degradation()

        assert flagged == [], "a tool with zero real failures must never be flagged"

    def test_a_tool_with_real_failures_alongside_sandbox_denials_still_flags(self) -> None:
        """Excluding sandbox denials must not blind the detector to a tool
        that is genuinely degraded in the same window."""
        sink: list = []
        rows = [{"tool_name": "write_file", "total": 10, "failures": 6}]
        conn = self._Conn(rows, sink)
        with patch("robothor.db.connection.get_connection", return_value=conn):
            flagged = detectors.check_tool_degradation()

        assert len(flagged) == 1
        assert flagged[0]["tool_name"] == "write_file"


class TestCheckToolOutageExcludesSandboxDenials:
    """Same false alarm as TestCheckToolDegradationExcludesSandboxDenials, but
    over check_tool_outage's 7-day window: a low-traffic tool whose only
    calls in the window are nightly benchmark-sandbox refusals would read as
    ~totally dead and escalate to a critical page, for a tool nothing is
    actually wrong with.
    """

    class _Cur:
        def __init__(self, rows, sink):
            self._rows = rows
            self._sink = sink

        def execute(self, sql, params=None):
            self._sink.append((sql, params))

        def fetchall(self):
            return self._rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self, rows, sink):
            self._rows = rows
            self._sink = sink

        def cursor(self, *a, **k):
            return TestCheckToolOutageExcludesSandboxDenials._Cur(self._rows, self._sink)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_the_sql_excludes_sandbox_denied_rows(self) -> None:
        sink: list = []
        conn = self._Conn([], sink)
        with patch("robothor.db.connection.get_connection", return_value=conn):
            detectors.check_tool_outage()

        sql, params = sink[0]
        assert "error_type" in sql and ("<>" in sql or "!=" in sql or "NOT IN" in sql.upper()), (
            "the SQL must filter agent_tool_events on error_type, or a tool "
            "correctly refused by the benchmark sandbox pages as a dead dependency"
        )
        assert "sandbox_denied" in params.values(), (
            "the excluded error_type must be sandbox_denied, bound as a named parameter"
        )

    def test_a_tool_whose_only_calls_are_sandbox_denials_is_not_an_outage(self) -> None:
        """The row the (fixed) SQL returns once sandbox_denied rows are
        excluded entirely: zero real calls in the window, so it never even
        reaches the min_calls floor."""
        sink: list = []
        rows = [
            {
                "tool_name": "create_task",
                "total": 0,
                "failures": 0,
                "error_type": None,
                "last_success_at": None,
                "outage_days": None,
            }
        ]
        conn = self._Conn(rows, sink)
        with patch("robothor.db.connection.get_connection", return_value=conn):
            out = detectors.check_tool_outage()

        assert out == [], "a tool with zero real calls must never read as an outage"
