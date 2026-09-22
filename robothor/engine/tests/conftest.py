"""
Test fixtures for the Agent Engine.

Follows brain/memory_system/conftest.py patterns:
- Isolated test data via unique prefixes
- Mock dependencies (DB, Redis, LLM)
- Reusable fixtures for common test scenarios
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.config import EngineConfig
from robothor.engine.models import (
    AgentConfig,
    DeliveryMode,
)

if TYPE_CHECKING:
    from pathlib import Path

# test_prefix is inherited from the root conftest.py


@pytest.fixture(autouse=True)
def isolated_model_breaker(monkeypatch: pytest.MonkeyPatch):
    """Swap the global model breaker for a fresh, alert-less one per test.

    The production singleton is wired to ``_alert_operator``, which writes a
    real ``crm_agent_notifications`` row and posts to Telegram. Tests that
    drive ``_call_llm`` into repeated failures (e.g. with a 1s timeout) used to
    trip it and send real escalations — 92 phantom "openrouter/test/model"
    escalations landed in the operator's inbox this way. ``on_open=None`` makes
    the breaker inert, and a fresh instance per test stops open-circuit state
    leaking between tests. monkeypatch restores the real breaker afterwards.
    """
    from robothor.engine import model_breaker

    fresh = model_breaker.ModelBreaker(on_open=None)
    monkeypatch.setattr(model_breaker, "_BREAKER", fresh)
    yield fresh


@pytest.fixture(autouse=True)
def no_systemctl_in_engine_tests(monkeypatch: pytest.MonkeyPatch):
    """No engine test may shell out to the host's service manager.

    Measured with a logging shim first on ``PATH``: the suite spawned
    ``systemctl`` ten times — eight pre-existing ``ollama.service`` probes and
    two ``ActiveEnterTimestamp`` reads from the warmup host-state section. That
    count *understated* it, because ``host_state``'s module-global 60s cache
    suppressed every later test that warmed up ``main`` inside the window — and
    that same global meant a section rendered in one test file could be served
    to another.

    So: stub the probe, refuse any ``systemctl`` spawn, and clear the cache on
    both sides of every test. ``test_no_systemctl_in_tests.py`` asserts all
    three, so weakening this turns the suite red instead of quietly restoring
    the spawns.

    **The guarantee is that the spawn does not happen** — not that it is always
    loud. ``host_profile``'s ``ollama.service`` probe wraps its call in
    ``except Exception``, so it swallows this AssertionError and returns None,
    exactly as it does on a host with no systemd. That is correct degradation
    for that probe; it just means "loud" holds only where the caller does not
    catch, and the test file pins the property that holds everywhere.

    ``Popen`` is the guarded primitive rather than ``run``: ``run``,
    ``check_output`` and ``check_call`` are all built on it, so guarding it
    alone also covers a module that did ``from subprocess import run`` — which
    patching the module attribute did not, and which reached the host.
    ``asyncio``'s spawn path does not go through ``Popen``, so it is guarded
    separately.

    Scoped to argv[0]'s basename deliberately: an exact-string match is one
    ``/usr/bin/`` away from inert, and a blanket subprocess ban would be
    reverted the first time a test needed ``git``.
    """
    import asyncio as _asyncio
    import subprocess as _subprocess
    from pathlib import PurePath

    from robothor.engine import host_state

    host_state.reset_host_state_cache()

    def _stubbed_probe() -> None:
        return None

    _stubbed_probe._is_test_stub = True  # type: ignore[attr-defined]
    monkeypatch.setattr(host_state, "_systemctl_active_enter", _stubbed_probe)

    def _refuse_if_systemctl(args) -> None:  # type: ignore[no-untyped-def]
        argv0 = args[0] if isinstance(args, (list, tuple)) and args else args
        if isinstance(argv0, (str, PurePath)) and PurePath(str(argv0)).name == "systemctl":
            raise AssertionError(
                "A test tried to spawn systemctl. Tests must not depend on the "
                "host's service manager — patch the probe instead "
                "(see robothor/engine/tests/test_no_systemctl_in_tests.py)."
            )

    real_popen = _subprocess.Popen

    class _GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *rest, **kwargs):  # type: ignore[no-untyped-def]
            _refuse_if_systemctl(args)
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(_subprocess, "Popen", _GuardedPopen)

    real_exec = _asyncio.create_subprocess_exec

    async def _guarded_exec(program, *args, **kwargs):  # type: ignore[no-untyped-def]
        _refuse_if_systemctl([program])
        return await real_exec(program, *args, **kwargs)

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _guarded_exec)
    yield
    host_state.reset_host_state_cache()


@pytest.fixture(autouse=True)
def explicit_loopback_engine_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy unit apps mount Engine routers without the production middleware.

    Keep those tests in the one supported compatibility mode: explicit,
    non-production, loopback-only development.  Security tests delete this
    variable and exercise signed-token enforcement directly.
    """

    monkeypatch.setenv("GENUS_INSECURE_DEV_MODE", "true")
    monkeypatch.setenv("GENUS_ENVIRONMENT", "test")
    monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "127.0.0.1")


@pytest.fixture(autouse=True)
def seeded_unit_test_permissions(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Keep Engine unit tests independent from a live RBAC database.

    Direct permission tests exercise the real database-backed evaluator with
    their own fake connections.  Other Engine unit tests run with the same
    effective defaults installed by ``seed_default_permissions``.  Missing
    roles remain denied, so the security invariant is still exercised.
    """

    if request.path.name in {"test_permissions.py", "test_rbac_service_role.py"}:
        return

    from robothor.engine import permissions

    def _seeded_check(
        user_role: str,
        tenant_id: str,
        tool_name: str,
        *,
        user_id: str | None = None,
    ) -> str | None:
        del tenant_id, user_id
        if not user_role:
            return "Missing execution role — access denied"
        if user_role in {"service", "user", "member", "admin", "owner"}:
            return None
        if user_role == "viewer" and (
            tool_name.startswith(("search_", "get_", "list_"))
            or tool_name in {"memory_block_read", "memory_block_list"}
        ):
            return None
        return f"No permission rules for role '{user_role}' — access denied"

    monkeypatch.setattr(permissions, "check_tool_permission", _seeded_check)


@pytest.fixture
def engine_config(tmp_path: Path) -> EngineConfig:
    """Engine config pointing to temp workspace."""
    manifest_dir = tmp_path / "docs" / "agents"
    manifest_dir.mkdir(parents=True)
    return EngineConfig(
        bot_token="test-token-123",
        default_chat_id="12345",
        port=18899,
        tenant_id="test-tenant",
        workspace=tmp_path,
        manifest_dir=manifest_dir,
        max_concurrent_agents=2,
        max_iterations=5,
    )


@pytest.fixture
def sample_agent_config() -> AgentConfig:
    """A minimal agent config for testing."""
    return AgentConfig(
        id="test-agent",
        name="Test Agent",
        description="A test agent",
        model_primary="openrouter/test/model",
        model_fallbacks=["openrouter/test/fallback"],
        cron_expr="0 * * * *",
        timezone="UTC",
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        tools_allowed=["list_tasks", "create_task"],
        tools_denied=[],
        instruction_file="",
        bootstrap_files=[],
        task_protocol=True,
    )


@pytest.fixture
def sample_manifest(tmp_path: Path) -> Path:
    """Create a sample YAML manifest file and return its directory."""
    manifest_dir = tmp_path / "docs" / "agents"
    manifest_dir.mkdir(parents=True)

    manifest_content = """id: test-agent
name: Test Agent
description: A test agent for testing

model:
  primary: openrouter/test/model
  fallbacks:
    - openrouter/test/fallback

schedule:
  cron: "0 * * * *"
  timezone: UTC
  timeout_seconds: 30
  session_target: isolated

delivery:
  mode: announce
  channel: telegram
  to: "12345"

tools_allowed:
  - list_tasks
  - create_task
  - resolve_task
tools_denied:
  - message

task_protocol: true
review_workflow: false
notification_inbox: false
shared_working_state: false
status_file: brain/memory/test-agent-status.md

instruction_file: ""
bootstrap_files: []

sla:
  urgent: 30m
  high: 2h
"""
    (manifest_dir / "test-agent.yaml").write_text(manifest_content)
    return manifest_dir


@pytest.fixture
def mock_litellm_response():
    """Create a mock litellm response."""

    def _make_response(content="Test response", tool_calls=None, model="test-model"):
        response = MagicMock()
        response.model = model

        choice = MagicMock()
        choice.message.content = content
        choice.message.tool_calls = tool_calls
        response.choices = [choice]

        usage = MagicMock()
        usage.prompt_tokens = 100
        usage.completion_tokens = 50
        response.usage = usage

        return response

    return _make_response


@pytest.fixture
def mock_db():
    """Mock the database connection for unit tests."""
    with patch("robothor.engine.tracking.get_connection") as mock_conn:
        conn = MagicMock()
        cur = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.rowcount = 1
        cur.fetchone.return_value = None
        cur.fetchall.return_value = []
        mock_conn.return_value = conn
        yield {"connection": mock_conn, "conn": conn, "cursor": cur}


@pytest.fixture
def _mock_run_persistence():
    """Isolate execution-loop unit tests from the tracking database.

    Runner persistence has three distinct phases: initial run creation,
    per-iteration step batches (lazily imported by ``AgentSession``), and the
    final background persistence task.  Mock each phase at the symbol its
    caller actually resolves so these unit tests remain deterministic when
    PostgreSQL is unavailable.
    """
    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runtime.classified_deadline._persist"),
        patch("robothor.engine.tracking.create_steps_batch", return_value=0),
        patch("robothor.engine.runner.AgentRunner._persist_run"),
    ):
        yield


@contextmanager
def voice_notes(*, enabled: bool):
    """Arm or disarm inbound voice transcription for the block.

    Through the SETTING, not the environment variable. `handle_voice` used to
    read `os.environ["ROBOTHOR_VOICE_NOTES_ENABLED"]` directly and the tests
    patched that; routing the handler through
    `get_settings().channels.voice_notes_enabled` (re-review R5) made the env
    patch a no-op, which would have left these tests passing for the wrong
    reason — the disabled ones by accident and the enabled ones not at all.
    """
    from robothor.engine import telegram_attachments

    with patch.object(telegram_attachments, "_voice_notes_enabled", return_value=enabled) as flag:
        yield flag


@pytest.fixture(autouse=True)
def isolated_runtime_control_store(monkeypatch):
    """Unit runs have no persisted run row. Durable-control integration tests replace this stub."""
    monkeypatch.setattr("robothor.engine.runtime.controls.stopped", lambda tenant, run_id: False)
    monkeypatch.setattr(
        "robothor.engine.runtime.controls.issue", lambda *args, **kwargs: {"status": "stopping"}
    )


@pytest.fixture(autouse=True)
def isolated_effect_store(monkeypatch):
    """A native unit test must opt into a private effect database, never a shared default."""
    import os

    if "host=/tmp/runtime-migrated-" in os.environ.get("ROBOTHOR_TEST_DB_DSN", ""):
        return  # Canonical integration harness supplies a disposable, fully migrated DB.

    def unavailable():
        raise AssertionError("Use an isolated effect_db fixture for durable effect tests")

    monkeypatch.setattr("robothor.engine.runtime.effects.get_connection", unavailable)


@pytest.fixture
def isolated_plan_claims(monkeypatch):
    """Endpoint unit tests use a fake runner and fake durable admission.

    Atomic database claims are exercised in test_chat_plan_claim and the
    fully migrated native integration suite.
    """
    from unittest.mock import AsyncMock

    monkeypatch.setattr("robothor.engine.chat_plan_claim.claim_plan", AsyncMock(return_value=True))
    monkeypatch.setattr("robothor.engine.chat_plan_claim.already_admitted", lambda *a: False)
    monkeypatch.setattr("robothor.engine.chat_plan_claim.clear_claim", lambda *a: None)
    monkeypatch.setattr(
        "robothor.engine.chat_plan_changes.replace_pending_async", AsyncMock(return_value=True)
    )
