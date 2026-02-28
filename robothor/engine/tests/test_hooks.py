"""
Tests for event hooks — Redis Stream consumers that trigger agent runs.

Verifies:
- Stream prefix matches event_bus publisher prefix
- Event type field name matches envelope format
- Calendar/vision event types match what publishers emit
- Downstream agents are triggered on success
- Dedup prevents concurrent hook runs
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import yaml

from robothor.engine.hooks import EVENT_TRIGGERS, STREAM_PREFIX, EventHooks, _LEGACY_EVENT_TRIGGERS, build_event_triggers
from robothor.engine.models import AgentConfig, AgentHook, AgentRun, DeliveryMode, RunStatus, TriggerType


def _make_run(status: str = "completed") -> MagicMock:
    """Create a mock AgentRun with a status that has .value."""
    run = MagicMock()
    status_mock = MagicMock()
    status_mock.value = status
    run.status = status_mock
    return run


class TestStreamPrefix:
    """Verify stream prefix matches event_bus publishers."""

    def test_stream_prefix_matches_event_bus(self):
        """STREAM_PREFIX must equal event_bus.STREAM_PREFIX so consumers
        subscribe to the same keys publishers write to."""
        assert STREAM_PREFIX == "robothor:events:"

    def test_stream_prefix_in_triggers(self):
        """All EVENT_TRIGGERS keys are bare stream names (no prefix).
        The prefix is applied at subscription time."""
        for key in EVENT_TRIGGERS:
            assert not key.startswith("robothor:"), (
                f"EVENT_TRIGGERS key '{key}' should be bare, not prefixed"
            )


class TestEventTypesMatchPublishers:
    """Verify event types match what publisher scripts actually emit."""

    def test_email_trigger_uses_email_new(self):
        """email_sync.py publishes 'email.new'."""
        email_types = {t["event_type"] for t in EVENT_TRIGGERS["email"]}
        assert "email.new" in email_types

    def test_calendar_triggers_match_calendar_sync(self):
        """calendar_sync.py publishes calendar.new, calendar.rescheduled,
        calendar.modified — not calendar.updated."""
        cal_types = {t["event_type"] for t in EVENT_TRIGGERS["calendar"]}
        assert "calendar.new" in cal_types
        assert "calendar.rescheduled" in cal_types
        assert "calendar.modified" in cal_types
        assert "calendar.updated" not in cal_types, (
            "calendar.updated was the old bug — calendar_sync.py never publishes this"
        )

    def test_vision_trigger_uses_person_unknown(self):
        """vision_service.py publishes 'vision.person_unknown',
        not 'vision.unknown_person'."""
        vision_types = {t["event_type"] for t in EVENT_TRIGGERS["vision"]}
        assert "vision.person_unknown" in vision_types
        assert "vision.unknown_person" not in vision_types, (
            "vision.unknown_person was the old bug — vision_service.py publishes vision.person_unknown"
        )

    def test_all_triggers_have_required_fields(self):
        """Every trigger must have event_type, agent_id, and message."""
        for stream, triggers in EVENT_TRIGGERS.items():
            for trigger in triggers:
                assert "event_type" in trigger, f"Missing event_type in {stream}"
                assert "agent_id" in trigger, f"Missing agent_id in {stream}"
                assert "message" in trigger, f"Missing message in {stream}"


class TestHandleEvent:
    """Test _handle_event reads the correct envelope field and strips prefix."""

    @pytest.fixture
    def hooks(self, engine_config):
        runner = MagicMock()
        hooks = EventHooks(engine_config, runner)
        # Initialize the prefix mapping and triggers (normally done in start())
        hooks._event_triggers = dict(EVENT_TRIGGERS)
        hooks._prefixed_to_bare = {
            f"{STREAM_PREFIX}{s}": s for s in EVENT_TRIGGERS
        }
        return hooks

    @pytest.fixture
    def mock_redis(self):
        redis = AsyncMock()
        redis.xack = AsyncMock()
        redis.xadd = AsyncMock()
        return redis

    @pytest.mark.asyncio
    async def test_reads_type_field_not_event_type(self, hooks, mock_redis):
        """event_bus._make_envelope() stores event type under 'type',
        not 'event_type'. Verify _handle_event reads the correct field."""
        data = {
            b"type": b"email.new",
            b"source": b"email_sync",
            b"payload": b'{"subject": "test"}',
        }

        hooks.runner.execute = AsyncMock(return_value=_make_run("completed"))

        with patch("robothor.engine.hooks.try_acquire", return_value=True), \
             patch("robothor.engine.hooks.release"), \
             patch("robothor.engine.hooks.deliver", new_callable=AsyncMock), \
             patch("robothor.engine.config.load_agent_config") as mock_load:
            mock_load.return_value = AgentConfig(
                id="email-classifier", name="Email Classifier",
                delivery_mode=DeliveryMode.ANNOUNCE,
            )

            await hooks._handle_event(
                f"{STREAM_PREFIX}email", b"1-0", data, mock_redis, "engine"
            )

            # Runner should have been called — meaning the event type matched
            hooks.runner.execute.assert_called_once()
            call_kwargs = hooks.runner.execute.call_args
            assert call_kwargs.kwargs["agent_id"] == "email-classifier"

    @pytest.mark.asyncio
    async def test_strips_prefix_for_trigger_lookup(self, hooks, mock_redis):
        """When stream name is 'robothor:events:email', _handle_event
        should strip prefix and look up 'email' in EVENT_TRIGGERS."""
        data = {b"type": b"email.new", b"source": b"email_sync"}

        hooks.runner.execute = AsyncMock(return_value=_make_run("completed"))

        with patch("robothor.engine.hooks.try_acquire", return_value=True), \
             patch("robothor.engine.hooks.release"), \
             patch("robothor.engine.hooks.deliver", new_callable=AsyncMock), \
             patch("robothor.engine.config.load_agent_config") as mock_load:
            mock_load.return_value = AgentConfig(
                id="email-classifier", name="Email Classifier",
                delivery_mode=DeliveryMode.ANNOUNCE,
            )

            # Pass prefixed stream name — should still match
            await hooks._handle_event(
                f"{STREAM_PREFIX}email", b"1-0", data, mock_redis, "engine"
            )
            hooks.runner.execute.assert_called_once()

            # Pass bare name (no match in _prefixed_to_bare, falls through)
            hooks.runner.execute.reset_mock()
            await hooks._handle_event(
                "email", b"2-0", data, mock_redis, "engine"
            )
            hooks.runner.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_match_for_wrong_event_type(self, hooks, mock_redis):
        """Events with non-matching types should not trigger any agent."""
        data = {b"type": b"email.archived", b"source": b"email_sync"}

        hooks.runner.execute = AsyncMock()

        with patch("robothor.engine.hooks.try_acquire", return_value=True), \
             patch("robothor.engine.hooks.release"):
            await hooks._handle_event(
                f"{STREAM_PREFIX}email", b"1-0", data, mock_redis, "engine"
            )
            hooks.runner.execute.assert_not_called()


class TestDownstreamTriggers:
    """Verify downstream agents are triggered on successful runs."""

    @pytest.fixture
    def hooks(self, engine_config):
        runner = MagicMock()
        hooks = EventHooks(engine_config, runner)
        hooks._event_triggers = dict(EVENT_TRIGGERS)
        hooks._prefixed_to_bare = {
            f"{STREAM_PREFIX}{s}": s for s in EVENT_TRIGGERS
        }
        return hooks

    @pytest.fixture
    def mock_redis(self):
        redis = AsyncMock()
        redis.xack = AsyncMock()
        redis.xadd = AsyncMock()
        return redis

    @pytest.mark.asyncio
    async def test_downstream_triggered_on_success(self, hooks, mock_redis):
        """When a hook run completes successfully and the agent has
        downstream_agents, those agents should be triggered."""
        data = {b"type": b"email.new", b"source": b"email_sync"}

        hooks.runner.execute = AsyncMock(return_value=_make_run("completed"))

        classifier_config = AgentConfig(
            id="email-classifier",
            name="Email Classifier",
            delivery_mode=DeliveryMode.ANNOUNCE,
            downstream_agents=["email-analyst", "email-responder"],
        )

        with patch("robothor.engine.hooks.try_acquire", return_value=True), \
             patch("robothor.engine.hooks.release"), \
             patch("robothor.engine.hooks.deliver", new_callable=AsyncMock), \
             patch("robothor.engine.config.load_agent_config") as mock_load:
            mock_load.return_value = classifier_config

            # Patch _trigger_downstream to track calls without actually running
            hooks._trigger_downstream = AsyncMock()

            await hooks._handle_event(
                f"{STREAM_PREFIX}email", b"1-0", data, mock_redis, "engine"
            )

            # Runner should have been called for the classifier
            hooks.runner.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_downstream_skipped_on_failure(self, hooks, mock_redis):
        """When a hook run fails, downstream agents should NOT be triggered."""
        data = {b"type": b"email.new", b"source": b"email_sync"}

        hooks.runner.execute = AsyncMock(return_value=_make_run("failed"))

        classifier_config = AgentConfig(
            id="email-classifier",
            name="Email Classifier",
            delivery_mode=DeliveryMode.ANNOUNCE,
            downstream_agents=["email-analyst", "email-responder"],
        )

        with patch("robothor.engine.hooks.try_acquire", return_value=True), \
             patch("robothor.engine.hooks.release"), \
             patch("robothor.engine.hooks.deliver", new_callable=AsyncMock), \
             patch("robothor.engine.config.load_agent_config") as mock_load:
            mock_load.return_value = classifier_config
            hooks._trigger_downstream = AsyncMock()

            await hooks._handle_event(
                f"{STREAM_PREFIX}email", b"1-0", data, mock_redis, "engine"
            )

            # _trigger_downstream should not have been called
            hooks._trigger_downstream.assert_not_called()

    @pytest.mark.asyncio
    async def test_downstream_skipped_when_no_downstream_agents(self, hooks, mock_redis):
        """When agent has no downstream_agents, nothing extra should happen."""
        data = {b"type": b"vision.person_unknown", b"source": b"vision_service"}

        hooks.runner.execute = AsyncMock(return_value=_make_run("completed"))

        vision_config = AgentConfig(
            id="vision-monitor",
            name="Vision Monitor",
            delivery_mode=DeliveryMode.NONE,
            downstream_agents=[],
        )

        with patch("robothor.engine.hooks.try_acquire", return_value=True), \
             patch("robothor.engine.hooks.release"), \
             patch("robothor.engine.hooks.deliver", new_callable=AsyncMock), \
             patch("robothor.engine.config.load_agent_config") as mock_load:
            mock_load.return_value = vision_config
            hooks._trigger_downstream = AsyncMock()

            await hooks._handle_event(
                f"{STREAM_PREFIX}vision", b"1-0", data, mock_redis, "engine"
            )

            hooks._trigger_downstream.assert_not_called()


class TestDedup:
    """Verify dedup prevents concurrent hook runs."""

    @pytest.fixture
    def hooks(self, engine_config):
        runner = MagicMock()
        hooks = EventHooks(engine_config, runner)
        hooks._event_triggers = dict(EVENT_TRIGGERS)
        hooks._prefixed_to_bare = {
            f"{STREAM_PREFIX}{s}": s for s in EVENT_TRIGGERS
        }
        return hooks

    @pytest.fixture
    def mock_redis(self):
        redis = AsyncMock()
        redis.xack = AsyncMock()
        redis.xadd = AsyncMock()
        return redis

    @pytest.mark.asyncio
    async def test_dedup_prevents_concurrent_hook(self, hooks, mock_redis):
        """When try_acquire returns False (agent already running),
        the hook should skip execution."""
        data = {b"type": b"email.new", b"source": b"email_sync"}

        hooks.runner.execute = AsyncMock()

        with patch("robothor.engine.hooks.try_acquire", return_value=False):
            await hooks._handle_event(
                f"{STREAM_PREFIX}email", b"1-0", data, mock_redis, "engine"
            )

            # Runner should NOT have been called
            hooks.runner.execute.assert_not_called()


class TestTriggerDownstream:
    """Test the _trigger_downstream method directly."""

    @pytest.fixture
    def hooks(self, engine_config):
        runner = MagicMock()
        hooks = EventHooks(engine_config, runner)
        return hooks

    @pytest.mark.asyncio
    async def test_trigger_downstream_executes_agent(self, hooks):
        """_trigger_downstream should load config, execute, and deliver."""
        hooks.runner.execute = AsyncMock(return_value=_make_run("completed"))

        downstream_config = AgentConfig(
            id="email-analyst",
            name="Email Analyst",
            delivery_mode=DeliveryMode.ANNOUNCE,
        )

        with patch("robothor.engine.hooks.try_acquire", return_value=True), \
             patch("robothor.engine.hooks.release") as mock_release, \
             patch("robothor.engine.hooks.deliver", new_callable=AsyncMock) as mock_deliver, \
             patch("robothor.engine.config.load_agent_config", return_value=downstream_config):

            await hooks._trigger_downstream("email-analyst", "email", "email.new")

            hooks.runner.execute.assert_called_once()
            call_kwargs = hooks.runner.execute.call_args.kwargs
            assert call_kwargs["agent_id"] == "email-analyst"
            assert call_kwargs["trigger_type"] == TriggerType.HOOK
            assert "downstream:" in call_kwargs["trigger_detail"]

            mock_deliver.assert_called_once()
            mock_release.assert_called_once_with("email-analyst")

    @pytest.mark.asyncio
    async def test_trigger_downstream_skipped_when_running(self, hooks):
        """_trigger_downstream should skip if agent is already running."""
        hooks.runner.execute = AsyncMock()

        with patch("robothor.engine.hooks.try_acquire", return_value=False):
            await hooks._trigger_downstream("email-analyst", "email", "email.new")
            hooks.runner.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_trigger_downstream_releases_on_error(self, hooks):
        """_trigger_downstream should always release the lock, even on error."""
        hooks.runner.execute = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("robothor.engine.hooks.try_acquire", return_value=True), \
             patch("robothor.engine.hooks.release") as mock_release, \
             patch("robothor.engine.config.load_agent_config") as mock_load:
            mock_load.return_value = AgentConfig(
                id="email-analyst", name="Email Analyst",
                delivery_mode=DeliveryMode.ANNOUNCE,
            )

            await hooks._trigger_downstream("email-analyst", "email", "email.new")
            mock_release.assert_called_once_with("email-analyst")


class TestBuildEventTriggers:
    """Test build_event_triggers() — manifest-driven hook aggregation."""

    def test_from_manifests(self, tmp_path):
        """Hooks defined in manifests produce correct trigger map."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        # Agent with one hook
        (agents_dir / "my-agent.yaml").write_text(yaml.dump({
            "id": "my-agent",
            "name": "My Agent",
            "description": "Test",
            "version": "2026-02-28",
            "department": "custom",
            "hooks": [
                {"stream": "email", "event_type": "email.new", "message": "Process emails"},
            ],
        }))

        triggers = build_event_triggers(agents_dir)
        assert "email" in triggers
        assert len(triggers["email"]) == 1
        assert triggers["email"][0]["agent_id"] == "my-agent"
        assert triggers["email"][0]["event_type"] == "email.new"
        assert triggers["email"][0]["message"] == "Process emails"

    def test_legacy_fallback(self, tmp_path):
        """When no manifests define hooks, falls back to legacy triggers."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        # Agent with NO hooks
        (agents_dir / "bare-agent.yaml").write_text(yaml.dump({
            "id": "bare-agent",
            "name": "Bare Agent",
            "description": "No hooks",
            "version": "2026-02-28",
            "department": "custom",
        }))

        triggers = build_event_triggers(agents_dir)
        # Should fall back to legacy triggers
        assert triggers == _LEGACY_EVENT_TRIGGERS

    def test_legacy_fallback_nonexistent_dir(self, tmp_path):
        """Non-existent manifest dir falls back to legacy triggers."""
        triggers = build_event_triggers(tmp_path / "nonexistent")
        assert triggers == _LEGACY_EVENT_TRIGGERS

    def test_multi_hook_agent(self, tmp_path):
        """Agent with multiple hooks across different streams."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        (agents_dir / "multi.yaml").write_text(yaml.dump({
            "id": "multi-hook",
            "name": "Multi Hook",
            "description": "Multiple hooks",
            "version": "2026-02-28",
            "department": "custom",
            "hooks": [
                {"stream": "calendar", "event_type": "calendar.new", "message": "New event"},
                {"stream": "calendar", "event_type": "calendar.modified", "message": "Modified event"},
                {"stream": "email", "event_type": "email.new", "message": "New email"},
            ],
        }))

        triggers = build_event_triggers(agents_dir)
        assert len(triggers["calendar"]) == 2
        assert len(triggers["email"]) == 1

    def test_multi_agent_same_stream(self, tmp_path):
        """Multiple agents hooking the same stream/event_type."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        (agents_dir / "agent-a.yaml").write_text(yaml.dump({
            "id": "agent-a",
            "name": "Agent A",
            "description": "First",
            "version": "2026-02-28",
            "department": "custom",
            "hooks": [{"stream": "email", "event_type": "email.new", "message": "A handles email"}],
        }))
        (agents_dir / "agent-b.yaml").write_text(yaml.dump({
            "id": "agent-b",
            "name": "Agent B",
            "description": "Second",
            "version": "2026-02-28",
            "department": "custom",
            "hooks": [{"stream": "email", "event_type": "email.new", "message": "B handles email"}],
        }))

        triggers = build_event_triggers(agents_dir)
        assert len(triggers["email"]) == 2
        agent_ids = {t["agent_id"] for t in triggers["email"]}
        assert agent_ids == {"agent-a", "agent-b"}

    def test_invalid_hooks_skipped(self, tmp_path):
        """Invalid hook entries (missing stream/event_type) are silently skipped."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        (agents_dir / "bad-hooks.yaml").write_text(yaml.dump({
            "id": "bad-hooks",
            "name": "Bad Hooks",
            "description": "Invalid hooks",
            "version": "2026-02-28",
            "department": "custom",
            "hooks": [
                {"stream": "email"},  # Missing event_type
                {"event_type": "email.new"},  # Missing stream
                "not-a-dict",  # Not a dict at all
                {"stream": "email", "event_type": "email.new", "message": "Valid"},  # Valid
            ],
        }))

        triggers = build_event_triggers(agents_dir)
        # Only the valid hook should produce a trigger
        assert "email" in triggers
        assert len(triggers["email"]) == 1
        assert triggers["email"][0]["message"] == "Valid"

    def test_empty_hooks_list_fallback(self, tmp_path):
        """Agent with empty hooks list still falls back to legacy."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        (agents_dir / "empty-hooks.yaml").write_text(yaml.dump({
            "id": "empty-hooks",
            "name": "Empty Hooks",
            "description": "Empty hooks list",
            "version": "2026-02-28",
            "department": "custom",
            "hooks": [],
        }))

        triggers = build_event_triggers(agents_dir)
        assert triggers == _LEGACY_EVENT_TRIGGERS
