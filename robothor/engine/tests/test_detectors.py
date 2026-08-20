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
