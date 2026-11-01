"""Engine fallbacks still say when they were taken.

Every site here used to be `except Exception: pass`, or logged at DEBUG on a
path whose job is to raise an alarm. Each test makes the inner call raise and
checks (a) the fallback is unchanged and (b) a log record from the module names
what failed, at the level the site now promises.
"""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _records(
    caplog: pytest.LogCaptureFixture, logger: str, *needles: str, level: int = logging.DEBUG
) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == logger and r.levelno >= level and all(n in r.getMessage() for n in needles)
    ]


# ── warmup.py context hooks ─────────────────────────────────────────────────

_WARMUP = "robothor.engine.warmup"


def test_travel_status_logs_a_block_read_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.engine import warmup

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("blocks table missing")

    monkeypatch.setattr("robothor.memory.blocks.read_block", _boom)
    with caplog.at_level(logging.DEBUG, logger=_WARMUP):
        assert warmup._travel_status() is None
    assert _records(caplog, _WARMUP, "travel", "blocks table missing")


def test_weather_context_logs_an_unreadable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.engine import warmup

    # A directory where the file should be: exists() is true, read_text() raises.
    (tmp_path / "brain" / "memory" / "weather-status.md").mkdir(parents=True)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    with caplog.at_level(logging.DEBUG, logger=_WARMUP):
        assert warmup._weather_context() is None
    assert _records(caplog, _WARMUP, "weather")


def test_git_status_context_logs_each_failed_git_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.engine import warmup

    def _timeout(cmd: list[str], **k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd, 0.08)

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", _timeout)
    config = SimpleNamespace(tools_allowed=["git_status"])
    with caplog.at_level(logging.DEBUG, logger=_WARMUP):
        assert warmup._git_status_context(config) is None  # type: ignore[arg-type]
    assert _records(caplog, _WARMUP, "git status")
    assert _records(caplog, _WARMUP, "git log")


# ── self_model.py ───────────────────────────────────────────────────────────

_SELF_MODEL = "robothor.engine.self_model"


@pytest.mark.asyncio
async def test_self_model_logs_anomaly_and_learnings_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from robothor.engine import self_model

    with (
        patch.object(
            self_model,
            "get_fleet_health",
            return_value={"agents": [{"agent_id": "example-agent", "total_runs": 3}]},
        ),
        patch.object(self_model, "get_agent_stats", return_value={}),
        patch.object(self_model, "detect_anomalies", side_effect=RuntimeError("anomaly query")),
        patch.object(self_model, "get_failure_patterns", return_value={}),
        patch.object(self_model, "get_memory_stats", return_value={}),
        patch.object(self_model, "read_block", side_effect=RuntimeError("block read")),
        patch.object(self_model.llm_client, "generate", AsyncMock(return_value="## Strengths")),
        caplog.at_level(logging.DEBUG, logger=_SELF_MODEL),
    ):
        model = await self_model.build_self_model(days=1)

    assert model["anomalies"] == []
    assert _records(caplog, _SELF_MODEL, "example-agent", "anomaly query")
    assert _records(caplog, _SELF_MODEL, "autoagent_learnings", "block read")


# ── autodream.py ────────────────────────────────────────────────────────────


def test_autodream_logs_a_redis_read_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.engine import autodream

    def _boom() -> Any:
        raise ConnectionError("redis refused")

    monkeypatch.setattr("robothor.events.bus._get_redis", _boom)
    monkeypatch.setattr(autodream, "_FALLBACK_PATH", str(tmp_path / "missing"))
    with caplog.at_level(logging.DEBUG, logger="robothor.engine.autodream"):
        assert autodream._get_last_run_ts_with_source() == (None, "none")
    assert _records(caplog, "robothor.engine.autodream", "Redis", "redis refused")


# ── dashboards ──────────────────────────────────────────────────────────────


def test_cron_status_logs_a_crontab_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.engine.dashboards import data

    def _missing(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError("crontab")

    monkeypatch.setattr(subprocess, "run", _missing)
    with caplog.at_level(logging.DEBUG, logger="robothor.engine.dashboards.data"):
        assert data.get_cron_status() == {"systemCrons": []}
    assert _records(caplog, "robothor.engine.dashboards.data", "crontab")


@pytest.mark.asyncio
async def test_homepage_logs_engine_and_vision_fetch_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.engine.dashboards import router

    class _Down:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        async def __aenter__(self) -> _Down:
            return self

        async def __aexit__(self, *exc: object) -> None: ...

        async def get(self, url: str, **k: Any) -> Any:
            raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    rendered: dict[str, Any] = {}

    def _render(name: str, **ctx: Any) -> str:
        rendered.update(ctx)
        return "<html></html>"

    monkeypatch.setattr(httpx, "AsyncClient", _Down)
    monkeypatch.setattr(subprocess, "run", MagicMock(side_effect=FileNotFoundError("systemctl")))
    monkeypatch.setattr("robothor.engine.dashboards.data.get_stats", lambda: {})
    monkeypatch.setattr("robothor.engine.dashboards.services.get_uptime", lambda: "1h")
    monkeypatch.setattr("robothor.engine.dashboards.theme.brand_css", lambda: "")
    monkeypatch.setattr(router, "_render_template", _render)
    request = SimpleNamespace(headers={})

    with caplog.at_level(logging.DEBUG, logger="robothor.engine.dashboards.router"):
        await router.homepage(request)  # type: ignore[arg-type]

    assert rendered["agents"] == {}
    assert rendered["vision_mode"] == "unknown"
    logger = "robothor.engine.dashboards.router"
    assert _records(caplog, logger, "engine", "ConnectError", level=logging.WARNING)
    assert _records(caplog, logger, "vision", "ConnectError")


# ── telegram ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_status_edit_is_logged_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deep-mode progress ticker and the tool-status line share this edit."""
    from robothor.engine.telegram_plan_mode import edit_status_text

    bot = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=RuntimeError("message is not modified"))
    )
    with caplog.at_level(logging.DEBUG, logger="robothor.engine.telegram_plan_mode"):
        ok = await edit_status_text(bot, "42", 7, "working...")
    assert ok is False
    assert _records(caplog, "robothor.engine.telegram_plan_mode", "message is not modified")

    bot.edit_message_text = AsyncMock()
    assert await edit_status_text(bot, "42", 7, "working...") is True
    assert await edit_status_text(bot, "42", None, "working...") is False


class _Handlers:
    """Just enough of TelegramBot for the plan and model callbacks."""

    def __init__(self) -> None:
        self._model_override: dict[str, str] = {}
        self._active_tasks: dict[str, Any] = {}
        self.sent: list[str] = []

    def _session_key(self, chat_id: str) -> str:
        return f"telegram:{chat_id}"

    def _get_tenant_id(self, chat_id: str) -> str:
        return "test-tenant"

    async def send_message(self, chat_id: str, text: str, **k: Any) -> list[Any]:
        self.sent.append(text)
        return []

    def _build_model_keyboard(self, model_id: str) -> None:
        return None


def _callback(data: str, *, method: str) -> Any:
    message = SimpleNamespace(chat=SimpleNamespace(id=42))
    setattr(message, method, AsyncMock(side_effect=RuntimeError("message to edit not found")))
    return SimpleNamespace(data=data, message=message, answer=AsyncMock())


@pytest.mark.asyncio
async def test_rejecting_a_plan_logs_a_failed_keyboard_removal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from robothor.engine import telegram_handlers as th

    bot = _Handlers()
    session = SimpleNamespace(active_plan=SimpleNamespace(plan_id="p1", status="pending"))
    callback = _callback("plan:reject:p1", method="edit_reply_markup")
    with (
        patch.object(th, "get_shared_session", return_value=session),
        patch.object(th, "_plan_is_expired", return_value=False),
        patch.object(th, "get_task_registry", return_value=MagicMock()),
        patch.object(th, "clear_plan_state_async", MagicMock()),
        caplog.at_level(logging.DEBUG, logger="robothor.engine.telegram_handlers"),
    ):
        await th.TelegramHandlersMixin.on_plan_decision(bot, callback)  # type: ignore[arg-type]

    callback.answer.assert_awaited_with("Plan rejected")
    assert session.active_plan is None
    assert _records(caplog, "robothor.engine.telegram_handlers", "message to edit not found")


@pytest.mark.asyncio
async def test_approving_a_plan_logs_a_failed_keyboard_removal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from robothor.engine import telegram_handlers as th

    bot = _Handlers()
    bot._execute_approved_plan = AsyncMock()  # type: ignore[attr-defined]
    session = SimpleNamespace(active_plan=SimpleNamespace(plan_id="p1", status="pending"))
    callback = _callback("plan:approve:p1", method="edit_reply_markup")
    with (
        patch.object(th, "get_shared_session", return_value=session),
        patch.object(th, "_plan_is_expired", return_value=False),
        patch("robothor.engine.telegram_plan_admission.approval_error", return_value=None),
        caplog.at_level(logging.DEBUG, logger="robothor.engine.telegram_handlers"),
    ):
        await th.TelegramHandlersMixin.on_plan_decision(bot, callback)  # type: ignore[arg-type]
        await bot._active_tasks["42"]

    assert session.active_plan.status == "approved"
    assert _records(caplog, "robothor.engine.telegram_handlers", "message to edit not found")


@pytest.mark.asyncio
async def test_switching_model_logs_a_failed_message_edit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from robothor.engine import telegram_handlers as th

    model_id = next(iter(th.MODEL_DISPLAY_NAMES))
    bot = _Handlers()
    callback = _callback(f"model:{model_id}", method="edit_text")
    with (
        patch.object(th, "get_shared_session", return_value=SimpleNamespace(model_override=None)),
        patch.object(th, "get_task_registry", return_value=MagicMock()),
        patch.object(th, "update_model_override_async", MagicMock()),
        caplog.at_level(logging.DEBUG, logger="robothor.engine.telegram_handlers"),
    ):
        await th.TelegramHandlersMixin.on_model_select(bot, callback)  # type: ignore[arg-type]

    assert bot._model_override["42"] == model_id
    callback.answer.assert_awaited()
    assert _records(caplog, "robothor.engine.telegram_handlers", "message to edit not found")


# ── tool handlers ───────────────────────────────────────────────────────────


def test_gws_email_resolution_logs_an_owner_config_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.engine.tools.handlers import gws

    def _boom() -> Any:
        raise ValueError("owner.yaml malformed")

    monkeypatch.delenv("ROBOTHOR_AI_EMAIL", raising=False)
    monkeypatch.setattr("robothor.owner_config.load_owner_config", _boom)
    with caplog.at_level(logging.DEBUG, logger="robothor.engine.tools.handlers.gws"):
        assert gws._resolve_robothor_email() == ""
    assert _records(caplog, "robothor.engine.tools.handlers.gws", "owner.yaml malformed")


@pytest.mark.asyncio
async def test_git_commit_warns_when_the_protected_branch_check_cannot_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The check is fail-open; an operator must at least be able to see that it was skipped."""
    from robothor.engine.tools.handlers import git

    calls: list[list[str]] = []

    def _run(cmd: list[str], **k: Any) -> Any:
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-parse"]:
            raise subprocess.TimeoutExpired(cmd, 5)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="nothing to add")

    monkeypatch.setattr(git.subprocess, "run", _run)
    ctx = SimpleNamespace(workspace=str(tmp_path))
    with caplog.at_level(logging.DEBUG, logger="robothor.engine.tools.handlers.git"):
        result = await git._git_commit({"message": "m"}, ctx)  # type: ignore[arg-type]

    assert result == {"error": "git add failed: nothing to add"}
    assert _records(
        caplog, "robothor.engine.tools.handlers.git", "protected", level=logging.WARNING
    )


@pytest.mark.asyncio
async def test_snapshot_logs_a_failed_vision_fallback_screenshot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from robothor.engine.tools.handlers.browser import _action_snapshot

    page = AsyncMock()
    page.url = "https://example.com"
    page.title = AsyncMock(return_value="Example")
    locator = AsyncMock()
    locator.aria_snapshot = AsyncMock(return_value='- heading "Test" [level=1]')
    page.locator = MagicMock(return_value=locator)
    page.screenshot = AsyncMock(side_effect=RuntimeError("target closed"))
    page.evaluate = AsyncMock(return_value=10)
    session = MagicMock()
    session.page = page
    session.element_registry = {}
    ctx = MagicMock()
    ctx.agent_id = "test"

    with (
        patch(
            "robothor.engine.tools.handlers.browser._get_session",
            new_callable=AsyncMock,
            return_value=session,
        ),
        caplog.at_level(logging.DEBUG, logger="robothor.engine.tools.handlers.browser"),
    ):
        result = await _action_snapshot({}, ctx)

    assert "screenshot_base64" not in result
    assert _records(caplog, "robothor.engine.tools.handlers.browser", "target closed")


@pytest.mark.asyncio
async def test_judge_logs_a_failed_key_retirement(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.engine.tools.handlers import benchmark

    def _retire(*a: Any, **k: Any) -> None:
        raise RuntimeError("key pool locked")

    monkeypatch.setattr(benchmark, "JUDGE_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr("robothor.engine.key_pool.api_key_for_model", lambda model: "k-1")
    monkeypatch.setattr("robothor.engine.key_pool.retire_for_model", _retire)
    with (
        patch("litellm.acompletion", new=AsyncMock(side_effect=RuntimeError("401 unauthorized"))),
        caplog.at_level(logging.DEBUG, logger="robothor.engine.tools.handlers.benchmark"),
    ):
        outcome = await benchmark._judge_output("out", ["a"], "m")

    assert outcome.score is None
    assert _records(
        caplog,
        "robothor.engine.tools.handlers.benchmark",
        "key pool locked",
        level=logging.WARNING,
    )


# ── daemon.py: detectors are the alarm, so their failure is a WARNING ───────

_DAEMON = "robothor.engine.daemon"


@pytest.mark.asyncio
async def test_a_failing_outage_detector_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from robothor.engine import daemon

    monkeypatch.setattr(
        "robothor.engine.detectors.tool_outage_detector",
        AsyncMock(side_effect=RuntimeError("agent_runs unreadable")),
    )
    with caplog.at_level(logging.DEBUG, logger=_DAEMON):
        await daemon._run_outage_detectors("test-tenant")
    assert _records(caplog, _DAEMON, "agent_runs unreadable", level=logging.WARNING)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tick", "detector", "label"),
    [
        (4, "runaway_burn_detector", "runaway_burn"),
        (10, "repeat_error_detector", "pattern"),
        (20, "zombie_runner_detector", "zombie_runner"),
        (20, "stuck_workflow_detector", "workflow health"),
    ],
)
async def test_a_failing_tick_detector_is_a_warning(
    tick: int,
    detector: str,
    label: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from robothor.engine import daemon

    for name in (
        "runaway_burn_detector",
        "repeat_error_detector",
        "tool_degradation_detector",
        "zombie_runner_detector",
        "stuck_workflow_detector",
        "workflow_failure_streak_detector",
    ):
        monkeypatch.setattr(f"robothor.engine.detectors.{name}", AsyncMock(return_value=0))
    monkeypatch.setattr(
        f"robothor.engine.detectors.{detector}",
        AsyncMock(side_effect=RuntimeError("detector query failed")),
    )
    with caplog.at_level(logging.DEBUG, logger=_DAEMON):
        await daemon._run_tick_detectors(tick, "test-tenant")
    assert _records(caplog, _DAEMON, label, "detector query failed", level=logging.WARNING)


@pytest.mark.asyncio
async def test_a_failing_autodream_staleness_check_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The check that pages when memory consolidation stalls must not fail at DEBUG."""
    from robothor.engine import daemon

    def _boom() -> Any:
        raise RuntimeError("last-run lookup failed")

    monkeypatch.setattr(daemon, "_resolve_last_run", _boom)
    config = SimpleNamespace(default_chat_id="1", tenant_id="test-tenant")
    with caplog.at_level(logging.DEBUG, logger=_DAEMON):
        await daemon._check_autodream_staleness(config)  # type: ignore[arg-type]
    assert _records(caplog, _DAEMON, "autoDream", "last-run lookup failed", level=logging.WARNING)


# ── scheduler.py ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failing_user_cron_tick_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Its docstring promised "a DB hiccup is logged"; at DEBUG nobody saw it."""
    from robothor.engine.scheduler import CronScheduler

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("user_cronjobs missing")

    monkeypatch.setattr("robothor.engine.user_cron.list_due_cronjobs", _boom)
    scheduler = object.__new__(CronScheduler)
    scheduler.config = SimpleNamespace(tenant_id="test-tenant")  # type: ignore[assignment]
    with caplog.at_level(logging.DEBUG, logger="robothor.engine.scheduler"):
        await scheduler._tick_user_cronjobs()
    assert _records(
        caplog, "robothor.engine.scheduler", "user_cronjobs missing", level=logging.WARNING
    )
