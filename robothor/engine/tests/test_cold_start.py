"""Cold start: the engine reaches READY=1 quickly and warm.

``daemon.main`` used to start every subsystem strictly in sequence, parse the
manifest directory again for each consumer, create the DB pool on the first
query that happened to need it, leave the system-prompt cache empty until each
agent's first run, and restore webchat sessions synchronously on the event
loop. The first turn after a deploy paid for all of it.

These tests drive ``main()`` to READY=1 with fake subsystems: three of them
take 300 ms each, standing in for the I/O-bound steps that do not depend on
one another. Nothing here touches Postgres, Redis or a network.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.engine import daemon

if TYPE_CHECKING:
    from pathlib import Path

SLOW = 0.3  # seconds; each slow fake step

ALICE_KEY = "agent:main:alice"
ALICE_HISTORY = [{"role": "user", "content": "hi"}]


class _ReachedReady(Exception):  # noqa: N818 - a control-flow sentinel, not an error
    """Raised by the fake ``_sd_notify`` so ``main`` stops at READY=1."""


MAIN_YAML = """id: main
name: Main
instruction_file: brain/MAIN.md
"""

BOB_YAML = """id: bob
name: Bob
instruction_file: brain/BOB.md
hooks:
  - stream: email
    event_type: email.new
    message: "New email"
"""


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    """A two-agent workspace: main and bob, each with an instruction file."""
    agents = tmp_path / "docs" / "agents"
    agents.mkdir(parents=True)
    (tmp_path / "docs" / "workflows").mkdir()
    (tmp_path / "docs" / "hooks").mkdir()
    (tmp_path / "brain").mkdir()
    (tmp_path / "brain" / "MAIN.md").write_text("You are main.")
    (tmp_path / "brain" / "BOB.md").write_text("You are Bob.")
    (agents / "main.yaml").write_text(MAIN_YAML)
    (agents / "bob.yaml").write_text(BOB_YAML)
    return tmp_path


class _Harness:
    """Records what ``main`` did on its way to READY=1."""

    def __init__(self) -> None:
        self.started = 0.0
        self.ready_at: float | None = None
        self.scans = 0
        self.pool_created_at: float | None = None
        self.pool_at_ready: bool = False
        self.prompt_cache_at_ready: set[str] = set()
        self.scheduler_start_args: tuple[Any, ...] = ()
        self.scheduler_start_kwargs: dict[str, Any] = {}
        self.hooks_start_kwargs: dict[str, Any] = {}
        self.scan_results: list[Any] = []
        self.history_when_channels_start: list[Any] | None = None
        self.serve_health_kwargs: dict[str, Any] = {}
        self.session_load_seconds = 0.0

    @property
    def elapsed(self) -> float:
        assert self.ready_at is not None, "main never sent READY=1"
        return self.ready_at - self.started


async def _forever(*_args: Any, **_kwargs: Any) -> None:
    await asyncio.Event().wait()


async def _slow(*_args: Any, **_kwargs: Any) -> int:
    await asyncio.sleep(SLOW)
    return 0


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, fleet: Path) -> _Harness:
    """Patch every subsystem ``main`` starts with a fake.

    Three fakes are slow (300 ms each) and independent of one another:
    resuming interrupted runs, resuming coding jobs, and the manifest scan
    (a blocking read, as the real one is). Everything else returns at once.
    """
    # Import the modules main imports lazily, so first-import cost does not
    # land inside the timed window.
    import robothor.engine.buddy_hooks  # noqa: F401
    import robothor.engine.channel_bus  # noqa: F401
    import robothor.engine.hook_registry  # noqa: F401
    import robothor.engine.leader  # noqa: F401
    import robothor.engine.messaging  # noqa: F401
    import robothor.engine.repair_recovery  # noqa: F401
    import robothor.engine.teams  # noqa: F401
    import robothor.engine.tools  # noqa: F401
    from robothor.engine import config as config_mod
    from robothor.engine.config import EngineConfig

    h = _Harness()
    engine_config = EngineConfig(
        tenant_id="test-tenant",
        workspace=fleet,
        manifest_dir=fleet / "docs" / "agents",
        workflow_dir=fleet / "docs" / "workflows",
        bot_token="",
    )

    real_scan = config_mod.load_manifest_dir

    def counting_scan(manifest_dir: Path) -> Any:
        h.scans += 1
        time.sleep(SLOW)  # blocking, like reading YAML off a volume
        result = real_scan(manifest_dir)
        h.scan_results.append(result)
        return result

    monkeypatch.setattr(config_mod, "load_manifest_dir", counting_scan)

    import robothor.db.connection as db_connection

    def fake_get_pool(*_args: Any, **_kwargs: Any) -> Any:
        if h.pool_created_at is None:
            h.pool_created_at = time.perf_counter()
        return MagicMock(name="pool")

    monkeypatch.setattr(db_connection, "get_pool", fake_get_pool)

    config_mod._prompt_cache.clear()

    from robothor.engine import chat

    def fake_load_sessions(**_kwargs: Any) -> dict[str, Any]:
        time.sleep(h.session_load_seconds)  # blocking, like the real query
        return {ALICE_KEY: {"history": list(ALICE_HISTORY)}}

    monkeypatch.setattr(chat, "load_all_sessions", fake_load_sessions)
    chat._cache.sessions.pop(ALICE_KEY, None)

    def fake_notify(state: str) -> None:
        if state == "READY=1":
            h.ready_at = time.perf_counter()
            h.pool_at_ready = h.pool_created_at is not None
            h.prompt_cache_at_ready = set(config_mod._prompt_cache)
            raise _ReachedReady

    class FakeScheduler:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.trigger_channel_event = _noop

        def start(self, *args: Any, **kwargs: Any) -> Any:
            # Recorded at the call: the task's body may not run before READY.
            h.scheduler_start_args = args
            h.scheduler_start_kwargs = kwargs
            return _forever()

    class FakeEventHooks:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def start(self, *args: Any, **kwargs: Any) -> Any:
            h.hooks_start_kwargs = {"args": args, **kwargs}
            return _forever()

    class FakeWorkflowEngine:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def load_workflows(self, *_args: Any) -> int:
            return 0

    class FakeElector:
        run = staticmethod(_forever)
        stop = staticmethod(_noop)

    async def fake_runner(_config: Any) -> tuple[Any, Any]:
        return MagicMock(name="runner"), None

    m = monkeypatch.setattr
    m(daemon, "_install_shutdown_signals", asyncio.Event)
    m(daemon, "validate_engine_auth_configuration", lambda: None)
    m(daemon, "_configure_structured_logging", lambda: None)
    m(daemon, "_harden_and_state_posture", lambda: None)
    m(daemon, "_set_daemon_start_ts", lambda: None)
    m(daemon, "_bootstrap_owner_links", _noop)
    m(daemon, "load_provider_secrets_at_startup", _noop)
    m(daemon.EngineConfig, "from_env", classmethod(lambda _cls: engine_config))
    m(daemon, "_create_runner_with_assets", fake_runner)
    m(daemon, "resume_interrupted_runs", _slow)
    m(daemon, "_cleanup_stale_runs", lambda _tenant: 0)
    m("robothor.engine.repair_recovery.recover", _noop)
    m(daemon, "_start_coding_jobs", _slow)
    m(daemon, "_init_fleet_capacity", lambda _c: None)
    m("robothor.engine.messaging.init_messenger", lambda: None)
    m("robothor.engine.teams.init_team_manager", lambda: None)
    m("robothor.engine.leader.LeaderElector", FakeElector)
    m("robothor.engine.hook_registry.register_plugin_hooks", lambda _r: None)
    m("robothor.engine.buddy_hooks.register_buddy_hooks", lambda _r: None)
    m("robothor.engine.tools.set_runner", lambda *_a: None)
    m(daemon, "WorkflowEngine", FakeWorkflowEngine)
    m(daemon, "set_workflow_engine", lambda _e: None)
    m(daemon, "CronScheduler", FakeScheduler)
    m(daemon, "_attach_sales_runtime", lambda *_a: None)
    m(daemon, "EventHooks", FakeEventHooks)
    m(daemon, "_start_federation", _noop)
    m(daemon, "_background_workers", lambda _t: [])

    def fake_serve_health(*_args: Any, **kwargs: Any) -> Any:
        h.serve_health_kwargs = kwargs
        return _forever()

    m(daemon, "serve_health", fake_serve_health)
    for loop_name in (
        "_watchdog",
        "_autodream_loop",
        "_federation_heartbeat",
        "_curiosity_density_loop",
        "_curator_loop",
        "_extension_watcher_loop",
        "_capacity_governor_loop",
    ):
        m(daemon, loop_name, _forever)

    async def fake_channels(*_args: Any) -> None:
        # Telegram, Slack and inbound channels share the chat session cache;
        # what they see when they start is what their first turn runs with.
        h.history_when_channels_start = chat.get_shared_session(ALICE_KEY).history

    m(daemon, "_start_channels", fake_channels)
    m(daemon, "_sd_notify", fake_notify)
    return h


async def _cancel_leftovers() -> None:
    """Cancel the fake subsystem tasks ``main`` left running at READY=1."""
    current = asyncio.current_task()
    leftovers = [t for t in asyncio.all_tasks() if t is not current]
    for task in leftovers:
        task.cancel()
    await asyncio.gather(*leftovers, return_exceptions=True)


async def _run_to_ready(h: _Harness) -> None:
    h.started = time.perf_counter()
    try:
        with pytest.raises(_ReachedReady):
            await daemon.main()
    finally:
        await _cancel_leftovers()


class TestManifestsAreParsedOnce:
    @pytest.mark.asyncio
    async def test_main_parses_the_manifest_directory_once(self, harness):
        await _run_to_ready(harness)

        assert harness.scans == 1

    @pytest.mark.asyncio
    async def test_the_scheduler_is_handed_the_same_scan(self, harness):
        await _run_to_ready(harness)

        handed = harness.scheduler_start_kwargs.get("scan")
        assert handed is not None
        assert handed is harness.scan_results[0]

    @pytest.mark.asyncio
    async def test_the_event_hooks_are_handed_the_same_manifests(self, harness):
        await _run_to_ready(harness)

        handed = harness.hooks_start_kwargs.get("manifests")
        assert handed is not None
        assert [m["id"] for m in handed] == ["bob", "main"]


class TestIndependentStepsRunConcurrently:
    @pytest.mark.asyncio
    async def test_three_300ms_steps_take_about_300ms_not_900ms(self, harness):
        await _run_to_ready(harness)

        # Sequential today: resume (300) + coding jobs (300) + the hook loop's
        # manifest scan (300) all precede READY, about 900 ms.
        assert harness.elapsed < 0.6, f"startup took {harness.elapsed * 1000:.0f} ms"

    @pytest.mark.asyncio
    async def test_a_failing_step_still_fails_startup(self, harness, monkeypatch):
        async def boom() -> None:
            await asyncio.sleep(0.05)
            raise RuntimeError("coding jobs exploded")

        monkeypatch.setattr(daemon, "_start_coding_jobs", boom)
        harness.started = time.perf_counter()
        try:
            with pytest.raises(RuntimeError, match="coding jobs exploded"):
                await daemon.main()
        finally:
            await _cancel_leftovers()

        assert harness.ready_at is None, "READY=1 must not be sent after a failed step"


class TestWarmBeforeReady:
    @pytest.mark.asyncio
    async def test_the_db_pool_exists_before_ready(self, harness):
        await _run_to_ready(harness)

        assert harness.pool_at_ready

    @pytest.mark.asyncio
    async def test_every_installed_agent_has_a_cached_system_prompt(self, harness):
        await _run_to_ready(harness)

        assert harness.prompt_cache_at_ready >= {"main", "bob"}


class TestStartupTiming:
    @pytest.mark.asyncio
    async def test_one_startup_line_names_the_total_and_each_step(self, harness, caplog):
        with caplog.at_level(logging.INFO, logger="robothor.engine"):
            await _run_to_ready(harness)

        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("STARTUP ")]
        assert len(lines) == 1
        assert re.fullmatch(r"STARTUP \d+ms( [a-z_]+=\d+ms)+", lines[0]), lines[0]

    @pytest.mark.asyncio
    async def test_health_reports_startup_ms_after_startup(self, harness):
        await _run_to_ready(harness)

        body = _health_body()
        assert isinstance(body.get("startup_ms"), int)
        assert body["startup_ms"] >= 0

    def test_health_reports_null_startup_ms_before_startup(self, monkeypatch):
        from robothor.engine import startup

        monkeypatch.setattr(startup, "_total_ms", None)

        body = _health_body()
        assert "startup_ms" in body
        assert body["startup_ms"] is None


def _health_body() -> dict[str, Any]:
    mock_config = MagicMock()
    mock_config.tenant_id = "test-tenant"
    mock_config.bot_token = ""
    mock_config.port = 18800

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
    ):
        app = create_health_app(mock_config, runner=None, workflow_engine=None)
    client = TestClient(app, raise_server_exceptions=False)
    with patch("robothor.engine.tracking.list_schedules", return_value=[]):
        return client.get("/health").json()


class TestSessionRestoreIsOffTheLoop:
    """Webchat sessions are read from Postgres at startup.

    The read ran inside ``create_health_app``, on the event loop. It must
    leave the loop free, yet finish before any channel (Telegram, Slack,
    inbound) can take a turn on the shared session cache, so a message right
    after a restart never runs with an empty history.
    """

    @pytest.mark.asyncio
    async def test_channels_start_with_the_restored_history(self, harness):
        await _run_to_ready(harness)

        assert harness.history_when_channels_start == ALICE_HISTORY

    @pytest.mark.asyncio
    async def test_the_health_server_does_not_restore_a_second_time(self, harness):
        await _run_to_ready(harness)

        assert harness.serve_health_kwargs.get("restore_sessions") is False

    @pytest.mark.asyncio
    async def test_the_session_read_does_not_stall_the_loop(self, harness):
        harness.session_load_seconds = 0.8
        gaps: list[float] = []
        done = asyncio.Event()

        async def ticker() -> None:
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0.02)
        harness.started = time.perf_counter()
        try:
            with pytest.raises(_ReachedReady):
                await daemon.main()
        finally:
            done.set()
            await tick
            await _cancel_leftovers()

        assert harness.history_when_channels_start == ALICE_HISTORY
        assert max(gaps) < 0.4, f"event loop stalled {max(gaps) * 1000:.0f} ms"

    def test_create_health_app_alone_still_restores(self, monkeypatch):
        """Callers outside the daemon keep the synchronous restore."""
        from robothor.engine import chat

        monkeypatch.setattr(
            chat, "load_all_sessions", lambda **_k: {ALICE_KEY: {"history": list(ALICE_HISTORY)}}
        )
        monkeypatch.setattr("robothor.engine.ide.init_ide", lambda *_a: None)
        chat._cache.sessions.pop(ALICE_KEY, None)
        config = MagicMock()
        config.tenant_id = "test-tenant"
        config.bot_token = ""

        from robothor.engine.health import create_health_app

        with (
            patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
            patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
            patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        ):
            create_health_app(config, runner=MagicMock(name="runner"))

        assert chat._cache.sessions[ALICE_KEY].history == ALICE_HISTORY
        chat._cache.sessions.pop(ALICE_KEY, None)


class TestConsumersUseTheSharedScan:
    """The scheduler and the event hooks each re-read the directory at start."""

    @pytest.mark.asyncio
    async def test_scheduler_start_registers_jobs_from_a_handed_scan(self, fleet, monkeypatch):
        from robothor.engine import scheduler as scheduler_mod
        from robothor.engine.config import EngineConfig, ManifestScan

        cron_manifest = {"id": "bob", "name": "Bob", "schedule": {"cron": "0 9 * * *"}}
        scan = ManifestScan(manifests=(cron_manifest,), scanned=1, dir_readable=True)

        def no_rescan(_dir: Any) -> Any:
            raise AssertionError("start() re-read the manifest directory")

        monkeypatch.setattr(scheduler_mod, "load_manifest_dir", no_rescan)
        cfg = EngineConfig(manifest_dir=fleet / "docs" / "agents", workspace=fleet)
        sched = scheduler_mod.CronScheduler(cfg, MagicMock())
        with (
            patch("robothor.engine.scheduler.upsert_schedule"),
            patch("robothor.engine.scheduler.update_schedule_state"),
            patch("robothor.engine.scheduler.delete_stale_schedules", return_value=[]),
            patch.object(sched.scheduler, "start"),
            patch("asyncio.sleep", side_effect=asyncio.CancelledError),
            pytest.raises(asyncio.CancelledError),
        ):
            await sched.start(scan=scan)

        assert "bob" in [j.id for j in sched.scheduler.get_jobs()]

    def test_event_triggers_build_from_handed_manifests(self, fleet, monkeypatch):
        from robothor.engine import config as config_mod
        from robothor.engine.hooks import build_event_triggers

        def no_rescan(_dir: Any) -> Any:
            raise AssertionError("build_event_triggers re-read the manifest directory")

        manifests = [
            {
                "id": "bob",
                "name": "Bob",
                "hooks": [{"stream": "email", "event_type": "email.new", "message": "m"}],
            }
        ]
        monkeypatch.setattr(config_mod, "load_manifest_dir", no_rescan)

        triggers = build_event_triggers(fleet / "docs" / "agents", manifests=manifests)

        assert [t["agent_id"] for t in triggers["email"]] == ["bob"]
