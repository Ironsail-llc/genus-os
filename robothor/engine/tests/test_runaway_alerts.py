"""Tests for tunable runaway-token thresholds and soft-alert batching.

Context (2026-08-19): after a 20h outage, post-recovery catch-up runs
legitimately burned 500-620k tokens each — 6 pages to the operator's
Telegram in ~90 minutes for ~$0.35 of contained, working-as-designed spend.
The guard itself (robothor/engine/runner.py) was correct; the *alerting*
was not. Fix: env-tunable thresholds + batch soft alerts to at most one
page per quiet-period boundary, with a summary for anything accrued while
the window was open. The hard cap always pages immediately — unchanged.
"""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import patch

import pytest


@pytest.fixture
def runner_module(monkeypatch):
    """Reload runner with a clean module-level batching registry per test.

    The soft-alert registry lives at module scope (single-loop, no locks —
    see robothor/engine/runner.py). Reset it directly rather than reloading
    the whole module, which would re-run expensive import-time side effects
    (litellm pricing registration, etc.).
    """
    from robothor.engine import runner as runner_module

    monkeypatch.setattr(runner_module, "_soft_runaway_window_started_at", None)
    monkeypatch.setattr(runner_module, "_soft_runaway_pending", [])
    return runner_module


class TestEnvTunableThresholds:
    def test_default_alert_threshold_is_500k(self, monkeypatch) -> None:
        monkeypatch.delenv("ROBOTHOR_RUNAWAY_ALERT_TOKENS", raising=False)
        from robothor.engine import runner

        importlib.reload(runner)
        assert runner.RUNAWAY_TOKEN_ALERT == 500_000

    def test_default_hard_cap_is_5m(self, monkeypatch) -> None:
        monkeypatch.delenv("ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS", raising=False)
        from robothor.engine import runner

        importlib.reload(runner)
        assert runner.RUNAWAY_TOKEN_HARD_CAP == 5_000_000

    def test_env_override_respected_for_alert(self, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_RUNAWAY_ALERT_TOKENS", "750000")
        from robothor.engine import runner

        try:
            importlib.reload(runner)
            assert runner.RUNAWAY_TOKEN_ALERT == 750_000
        finally:
            monkeypatch.delenv("ROBOTHOR_RUNAWAY_ALERT_TOKENS", raising=False)
            importlib.reload(runner)

    def test_env_override_respected_for_hard_cap(self, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS", "8000000")
        from robothor.engine import runner

        try:
            importlib.reload(runner)
            assert runner.RUNAWAY_TOKEN_HARD_CAP == 8_000_000
        finally:
            monkeypatch.delenv("ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS", raising=False)
            importlib.reload(runner)

    def test_garbage_env_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_RUNAWAY_ALERT_TOKENS", "not-a-number")
        from robothor.engine import runner

        try:
            importlib.reload(runner)
            assert runner.RUNAWAY_TOKEN_ALERT == 500_000
        finally:
            monkeypatch.delenv("ROBOTHOR_RUNAWAY_ALERT_TOKENS", raising=False)
            importlib.reload(runner)


class TestSoftAlertBatching:
    """Drives runner._send_soft_runaway_alert directly with a fake clock."""

    def test_first_soft_event_pages_immediately(self, runner_module) -> None:
        fake_now = [1000.0]
        with (
            patch.object(runner_module, "_runaway_alert_clock", lambda: fake_now[0]),
            patch("robothor.engine.task_registry.get_task_registry") as mock_registry,
        ):
            runner_module._send_soft_runaway_alert(
                "crm-dedup", "run-1", 621_000, "openrouter/xiaomi/mimo-v2.5", 0.12
            )

        assert mock_registry.return_value.spawn.call_count == 1
        _, kwargs = mock_registry.return_value.spawn.call_args
        assert kwargs["name"] == "runaway-alert:crm-dedup"
        mock_registry.return_value.spawn.call_args[0][0].close()

    def test_second_event_within_window_does_not_page(self, runner_module) -> None:
        fake_now = [1000.0]
        with (
            patch.object(runner_module, "_runaway_alert_clock", lambda: fake_now[0]),
            patch("robothor.engine.task_registry.get_task_registry") as mock_registry,
        ):
            runner_module._send_soft_runaway_alert("crm-dedup", "run-1", 510_000, "model-a", 0.05)
            assert mock_registry.return_value.spawn.call_count == 1

            fake_now[0] += 600  # 10 minutes later, well within the 1h window
            runner_module._send_soft_runaway_alert("crm-sync", "run-2", 540_000, "model-a", 0.06)
            # Still just the one page from the first event — the second
            # accumulated instead of paging again.
            assert mock_registry.return_value.spawn.call_count == 1
            mock_registry.return_value.spawn.call_args[0][0].close()

        assert len(runner_module._soft_runaway_pending) == 1
        assert runner_module._soft_runaway_pending[0]["agent"] == "crm-sync"

    def test_post_window_event_delivers_batched_summary(self, runner_module) -> None:
        fake_now = [1000.0]
        with (
            patch.object(runner_module, "_runaway_alert_clock", lambda: fake_now[0]),
            patch("robothor.engine.task_registry.get_task_registry") as mock_registry,
        ):
            # First event: immediate page, opens the window.
            runner_module._send_soft_runaway_alert("crm-dedup", "run-1", 510_000, "model-a", 0.05)
            # Second event, within window: accumulates.
            fake_now[0] += 600
            runner_module._send_soft_runaway_alert("crm-sync", "run-2", 540_000, "model-a", 0.06)
            # Third event, after the window has expired: flushes the
            # accrued batch as a single summary alert.
            fake_now[0] += 3600
            runner_module._send_soft_runaway_alert(
                "crm-followup", "run-3", 560_000, "model-a", 0.07
            )

            assert mock_registry.return_value.spawn.call_count == 2
            _, kwargs = mock_registry.return_value.spawn.call_args
            assert kwargs["name"] == "runaway-alert-summary:crm-followup"

            # Close every coroutine handed to the mocked spawn() so none of
            # them warn "was never awaited" during garbage collection.
            for call in mock_registry.return_value.spawn.call_args_list:
                call.args[0].close()

        # Pending batch was cleared and a new window opened by the flush.
        assert runner_module._soft_runaway_pending == []
        assert runner_module._soft_runaway_window_started_at == fake_now[0]

    async def test_summary_alert_body_includes_batched_count(self, runner_module) -> None:
        fake_now = [1000.0]
        captured = {}

        async def fake_alert(level, title, body, **kwargs):
            captured["level"] = level
            captured["title"] = title
            captured["body"] = body
            return True

        with (
            patch.object(runner_module, "_runaway_alert_clock", lambda: fake_now[0]),
            patch("robothor.engine.alerts.alert", fake_alert),
        ):
            runner_module._send_soft_runaway_alert("crm-dedup", "run-1", 510_000, "model-a", 0.05)
            fake_now[0] += 600
            runner_module._send_soft_runaway_alert("crm-sync", "run-2", 540_000, "model-a", 0.06)
            fake_now[0] += 3600
            runner_module._send_soft_runaway_alert(
                "crm-followup", "run-3", 560_000, "model-a", 0.07
            )
            await asyncio.sleep(0)  # let the spawned alert task run

        assert "1" in captured["body"]
        assert "crm-sync" in captured["body"]
        assert "last hour" in captured["body"]

    async def test_immediate_alert_body_notes_containment_and_cost(self, runner_module) -> None:
        fake_now = [1000.0]
        captured = {}

        async def fake_alert(level, title, body, **kwargs):
            captured["level"] = level
            captured["title"] = title
            captured["body"] = body
            return True

        with (
            patch.object(runner_module, "_runaway_alert_clock", lambda: fake_now[0]),
            patch("robothor.engine.alerts.alert", fake_alert),
        ):
            runner_module._send_soft_runaway_alert(
                "crm-dedup", "run-1", 621_000, "openrouter/xiaomi/mimo-v2.5", 0.12
            )
            await asyncio.sleep(0)  # let the spawned alert task run

        assert captured["level"] == "warning"
        body_lower = captured["body"].lower()
        assert "contained" in body_lower
        assert "0.12" in captured["body"]
        assert "batched" in body_lower


class TestHardCapAlwaysPages:
    """Hard-cap alerts bypass the soft-alert batching entirely — always page."""

    @pytest.mark.asyncio
    async def test_hard_cap_paths_do_not_use_soft_batching_state(self, runner_module) -> None:
        # The hard-cap alert path in _run_loop calls alerts.alert() directly
        # (not _send_soft_runaway_alert), so repeated hard-cap hits never
        # touch — and are never suppressed by — the soft-alert registry.
        fake_now = [1000.0]
        with (
            patch.object(runner_module, "_runaway_alert_clock", lambda: fake_now[0]),
            patch("robothor.engine.task_registry.get_task_registry") as mock_registry,
        ):
            runner_module._send_soft_runaway_alert("crm-dedup", "run-1", 510_000, "model-a", 0.05)
            assert mock_registry.return_value.spawn.call_count == 1
            mock_registry.return_value.spawn.call_args[0][0].close()

        # Soft-alert state is untouched by anything outside
        # _send_soft_runaway_alert — a hard-cap alert() call elsewhere in
        # the loop never reads or mutates _soft_runaway_window_started_at.
        import inspect

        source = inspect.getsource(runner_module.AgentRunner._run_loop)
        start = source.index("RUNAWAY_TOKEN_HARD_CAP")
        end = source.index("RUNAWAY_TOKEN_ALERT", start)
        hard_cap_block = source[start:end]
        assert "_send_soft_runaway_alert" not in hard_cap_block
        assert "_soft_runaway" not in hard_cap_block
