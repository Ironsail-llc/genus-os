"""
Test fixtures for the Agent Engine.

Follows brain/memory_system/conftest.py patterns:
- Isolated test data via unique prefixes
- Mock dependencies (DB, Redis, LLM)
- Reusable fixtures for common test scenarios
"""

from __future__ import annotations

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

    def _seeded_check(user_role: str, tenant_id: str, tool_name: str) -> str | None:
        del tenant_id
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
