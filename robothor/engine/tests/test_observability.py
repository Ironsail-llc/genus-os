"""Tests for observability quick wins — delivery_status, tool events, health check."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.delivery import deliver, set_telegram_sender
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus

# ─── Helpers ────────────────────────────────────────────────────────


class _FakeMessage:
    """Stand-in for an aiogram Message returned by a successful send."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


def _make_run(**kwargs: object) -> AgentRun:
    defaults: dict[str, object] = {
        "id": "run-1",
        "agent_id": "test",
        "status": RunStatus.COMPLETED,
        "output_text": "Hello",
    }
    defaults.update(kwargs)
    return AgentRun(**defaults)  # type: ignore[arg-type]


def _make_config(**kwargs: object) -> AgentConfig:
    defaults: dict[str, object] = {
        "id": "test",
        "name": "Test",
        "delivery_mode": DeliveryMode.ANNOUNCE,
        "delivery_to": "12345",
    }
    defaults.update(kwargs)
    return AgentConfig(**defaults)  # type: ignore[arg-type]


# ─── delivery_status Tests ──────────────────────────────────────────


class TestDeliveryStatus:
    @pytest.fixture(autouse=True)
    def _setup_sender(self):
        # A bare AsyncMock returns a truthy MagicMock that iterates empty —
        # i.e. "nothing was sent". Return a real one-message list so these
        # tests exercise a genuine single-chunk delivery.
        sender = AsyncMock(return_value=[_FakeMessage(1)])
        set_telegram_sender(sender)
        yield sender
        set_telegram_sender(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_sub_agent_suppressed(self):
        """Sub-agent runs get delivery_status='suppressed_sub_agent'."""
        config = _make_config()
        run = _make_run(parent_run_id="parent-123")
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "suppressed_sub_agent"

    @pytest.mark.asyncio
    async def test_no_output_status(self):
        """Runs with no output get delivery_status='no_output'."""
        config = _make_config()
        run = _make_run(output_text=None)
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "no_output"

    @pytest.mark.asyncio
    async def test_empty_output_status(self):
        """Runs with empty output get delivery_status='no_output'."""
        config = _make_config()
        run = _make_run(output_text="")
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "no_output"

    @pytest.mark.asyncio
    async def test_trivial_heartbeat_output_suppressed(self):
        """Short filler output from heartbeat runs is suppressed."""
        config = _make_config()
        run = _make_run(
            output_text="All clear — no open tasks, no emails. Board is clean.",
            trigger_detail="heartbeat:0 6-22 * * *",
        )
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "suppressed_trivial"

    @pytest.mark.asyncio
    async def test_substantial_heartbeat_output_delivered(self, _setup_sender):
        """Heartbeat output >300 chars is always delivered."""
        config = _make_config()
        long_report = "**Friday Apr 10 — 8 AM ET**\n\n" + "x" * 300
        run = _make_run(
            output_text=long_report,
            trigger_detail="heartbeat:0 6-22 * * *",
        )
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "delivered"

    @pytest.mark.asyncio
    async def test_trivial_output_from_non_heartbeat_still_delivered(self, _setup_sender):
        """'All clear' from a non-heartbeat run is NOT suppressed."""
        config = _make_config()
        run = _make_run(output_text="All clear — board is clean.")
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "delivered"

    @pytest.mark.asyncio
    async def test_none_mode_silent(self):
        """delivery_mode=none gets delivery_status='silent'."""
        config = _make_config(delivery_mode=DeliveryMode.NONE)
        run = _make_run()
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "silent"

    @pytest.mark.asyncio
    async def test_announce_delivered(self, _setup_sender):
        """Successful Telegram delivery sets delivery_status='delivered'."""
        config = _make_config()
        run = _make_run()
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "delivered"
        assert run.delivery_channel == "telegram"
        assert run.delivered_at is not None

    @pytest.mark.asyncio
    async def test_announce_failed(self, _setup_sender):
        """Failed Telegram delivery sets delivery_status starting with 'failed'."""
        _setup_sender.side_effect = RuntimeError("Network error")
        config = _make_config()
        run = _make_run()
        result = await deliver(config, run)
        assert result is False
        assert run.delivery_status is not None
        assert run.delivery_status.startswith("failed")


# ─── Tool Event Logging Tests ──────────────────────────────────────


class TestLogToolEvent:
    def test_logs_successful_event(self, mock_db):
        """log_tool_event inserts a row for successful tool calls."""
        from robothor.engine.tracking import log_tool_event

        log_tool_event(
            run_id="run-1",
            tool_name="list_tasks",
            duration_ms=150,
            success=True,
        )
        mock_db["cursor"].execute.assert_called_once()
        sql = mock_db["cursor"].execute.call_args[0][0]
        assert "agent_tool_events" in sql

    def test_logs_failed_event_with_error_type(self, mock_db):
        """log_tool_event records error_type for failed calls."""
        from robothor.engine.tracking import log_tool_event

        log_tool_event(
            run_id="run-1",
            tool_name="exec",
            duration_ms=5000,
            success=False,
            error_type="timeout",
        )
        args = mock_db["cursor"].execute.call_args[0][1]
        assert args[4] is False  # success
        assert args[5] == "timeout"  # error_type

    def test_db_failure_silently_caught(self):
        """log_tool_event doesn't raise on DB errors."""
        from robothor.engine.tracking import log_tool_event

        with patch("robothor.engine.tracking.get_connection", side_effect=Exception("DB down")):
            # Should not raise
            log_tool_event(
                run_id="run-1",
                tool_name="read_file",
                duration_ms=10,
                success=True,
            )


# ─── Buddy Reflection in Delivery Tests ──────────────────────────


class TestHeartbeatHasNoBuddyAppendix:
    """Buddy no longer appends anything to heartbeat delivery.

    The old reflection pipeline was deleted — main agent output reaches Telegram
    exactly as the agent wrote it. Buddy runs as its own scheduled agent and
    communicates via CRM tasks + agent_reviews, never by bolting text onto main.
    """

    @pytest.fixture(autouse=True)
    def _setup_sender(self):
        # One acknowledged message per chunk — a bare AsyncMock iterates
        # empty, which delivery correctly reads as "nothing was sent".
        sender = AsyncMock(return_value=[_FakeMessage(1)])
        set_telegram_sender(sender)
        yield sender
        set_telegram_sender(None)  # type: ignore[arg-type]

    def test_reflection_helpers_are_deleted(self):
        """Dead-code regression check: reflection helpers stay removed."""
        from robothor.engine import delivery

        for name in (
            "_maybe_append_buddy_reflection",
            "_generate_buddy_reflection",
            "_get_buddy_context",
        ):
            assert not hasattr(delivery, name), (
                f"delivery.{name} was resurrected — Buddy must not bolt text onto heartbeat"
            )

    @pytest.mark.asyncio
    async def test_heartbeat_carries_no_buddy_appendix(self, _setup_sender):
        """Main-agent heartbeat output reaches Telegram with no Buddy text bolted on."""
        original_body = "Morning report: 3 tasks done, 1 PR open."
        config = _make_config(id="main")
        run = _make_run(
            output_text=original_body,
            trigger_detail="heartbeat:0 6-22 * * *",
        )
        result = await deliver(config, run)
        assert result is True
        sent_text = _setup_sender.call_args[0][1] if _setup_sender.call_args else ""
        # Body stays as-is (Telegram sender may prefix with agent name header).
        assert original_body in sent_text
        # No Buddy-reflection separator or canned phrases appended.
        assert "\n---\n" not in sent_text
        for phrase in ("momentum is building", "Level up", "streak milestone", "FLEET PULSE"):
            assert phrase not in sent_text


# ─── Tool Stats Tests ──────────────────────────────────────────────


class TestGetToolStats:
    def test_returns_aggregated_stats(self, mock_db):
        """get_tool_stats returns per-tool aggregated data."""
        from robothor.engine.tracking import get_tool_stats

        mock_db["cursor"].fetchall.return_value = [
            {
                "tool_name": "exec",
                "total_calls": 50,
                "successes": 48,
                "failures": 2,
                "avg_duration_ms": 3000,
                "max_duration_ms": 15000,
                "p95_duration_ms": 10000,
            }
        ]
        results = get_tool_stats(hours=24)
        assert len(results) == 1
        assert results[0]["tool_name"] == "exec"
        assert results[0]["failures"] == 2


# ─── Cron Health Check Tests ───────────────────────────────────────

# Resolve brain/scripts path relative to repo root (works in CI and locally)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BRAIN_SCRIPTS = str(_REPO_ROOT / "brain" / "scripts")


def _import_cron_health_check():
    """Import cron_health_check from brain/scripts using importlib."""
    import importlib.util

    script_path = Path(_BRAIN_SCRIPTS) / "cron_health_check.py"
    if not script_path.exists():
        pytest.skip("brain/scripts/cron_health_check.py not deployed")

    spec = importlib.util.spec_from_file_location("cron_health_check", script_path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestCronHealthCheck:
    @pytest.fixture(autouse=True)
    def _load_module(self):
        self.chc = _import_cron_health_check()

    def test_classify_agent_healthy(self):
        """Agent with successes and low failure rate is healthy."""
        agent = {
            "total_runs": 10,
            "completed": 9,
            "failed": 1,
            "timeouts": 0,
            "last_success_at": "2026-03-04",
        }
        assert self.chc.classify_agent(agent) == "healthy"

    def test_classify_agent_error_high_fail_rate(self):
        """Agent with >50% failure rate is error."""
        agent = {
            "total_runs": 4,
            "completed": 1,
            "failed": 3,
            "timeouts": 0,
            "last_success_at": "2026-03-04",
        }
        assert self.chc.classify_agent(agent) == "error"

    def test_classify_agent_stale_no_runs(self):
        """Agent with 0 runs is stale."""
        agent = {
            "total_runs": 0,
            "completed": 0,
            "failed": 0,
            "timeouts": 0,
            "last_success_at": None,
        }
        assert self.chc.classify_agent(agent) == "stale"

    def test_classify_agent_no_success_but_no_failures(self):
        """Agent with runs but no successes AND no failures is healthy (still running)."""
        agent = {
            "total_runs": 2,
            "completed": 0,
            "failed": 0,
            "timeouts": 0,
            "last_success_at": None,
        }
        assert self.chc.classify_agent(agent) == "healthy"

    def test_format_duration(self):
        assert self.chc.format_duration(None) == "—"
        assert self.chc.format_duration(0) == "—"
        assert self.chc.format_duration(500) == "500ms"
        assert self.chc.format_duration(5000) == "5s"

    def test_format_cost(self):
        assert self.chc.format_cost(None) == "$0"
        assert self.chc.format_cost(0) == "$0"
        assert self.chc.format_cost(0.005) == "$0.0050"
        assert self.chc.format_cost(1.23) == "$1.23"

    def test_write_status_creates_file(self, tmp_path):
        """write_status creates a markdown file."""
        output = tmp_path / "status.md"
        agents = [
            {
                "agent_id": "test-agent",
                "total_runs": 10,
                "completed": 9,
                "failed": 1,
                "timeouts": 0,
                "avg_duration_ms": 150,
                "total_cost_usd": 0.05,
                "last_run_at": None,
                "last_success_at": "2026-03-04",
            }
        ]
        fleet = {
            "total_runs": 10,
            "completed": 9,
            "failed": 1,
            "timeouts": 0,
            "total_cost_usd": 0.05,
            "avg_duration_ms": 150,
        }
        tools: dict[str, list[str]] = {"slowest": [], "failing": []}
        self.chc.write_status(agents, fleet, tools, output_path=output)
        content = output.read_text()
        assert "# Cron Health Status" in content
        assert "test-agent" in content
        assert "Fleet Summary" in content


# ─── classify_run_failure Tests ─────────────────────────────────────


class TestClassifyRunFailureAgreesWithTheReaper:
    """The tool reported `daemon_restart_in_window` from its own comparison.

    `classify_reap_reason` was fixed to parse both timestamps to instants
    before comparing them; this line kept the lexicographic string compare, so
    the tool now answered "category: post_tool_crash" and
    "daemon_restart_in_window: true" about the same run — worse than the
    original bug, because the two halves of one answer disagree and the
    operator has no way to tell which half is lying.
    """

    @staticmethod
    async def _diagnose(started_iso: str, daemon_ts: str) -> dict:
        from robothor.engine.tools.handlers.observability import HANDLERS

        run = {
            "id": "run-1",
            "agent_id": "crm-hygiene",
            "status": "timeout",
            "started_at": started_iso,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        steps = [
            {"step_type": "llm_call", "tool_name": None},
            {"step_type": "tool_call", "tool_name": "list_tasks", "error_message": None},
        ]
        with (
            patch("robothor.engine.tracking.get_run", return_value=run),
            patch("robothor.engine.tracking.list_steps", return_value=steps),
            patch.dict("os.environ", {"ROBOTHOR_DAEMON_START_TS": daemon_ts}),
        ):
            return await HANDLERS["classify_run_failure"]({"run_id": "run-1"}, None)

    @pytest.mark.asyncio
    async def test_a_run_that_started_after_the_boot_is_not_in_the_window(self):
        """03:00:16-04:00 is 07:00:16Z — nine minutes AFTER a 06:51:20Z boot."""
        out = await self._diagnose(
            "2026-09-03T03:00:16.508797-04:00", "2026-09-03T06:51:20.843732+00:00"
        )
        assert out["daemon_restart_in_window"] is False
        assert out["category"] == "post_tool_crash"

    @pytest.mark.asyncio
    async def test_the_mirror_case_still_reports_the_window(self):
        """01:00:16-04:00 is 05:00:16Z — well before the same boot."""
        out = await self._diagnose(
            "2026-09-03T01:00:16.508797-04:00", "2026-09-03T06:51:20.843732+00:00"
        )
        assert out["daemon_restart_in_window"] is True
        assert out["category"] == "daemon_restart"
