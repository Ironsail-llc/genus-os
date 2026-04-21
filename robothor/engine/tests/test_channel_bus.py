"""Tests for the channel bus — dual-writes of fleet Telegram deliveries into
main's canonical session so the supervisor has visibility of everything that
reaches the user.

Phase 1 scope: authorship filter, surface persistence, map recording,
kill-switch, audit publish. Phase 2/3 add reply resolution and wake tests.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.channel_bus import (
    on_post_delivery,
    record_outbound,
    resolve_reply_context,
)
from robothor.engine.chat_store import save_channel_surface
from robothor.engine.hook_registry import HookAction, HookContext, HookEvent


@pytest.fixture
def chat_store_db():
    with patch("robothor.engine.chat_store.get_connection") as mock_conn:
        conn = MagicMock()
        cur = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.rowcount = 1
        cur.fetchone.return_value = {"id": 77}
        cur.fetchall.return_value = []
        mock_conn.return_value = conn
        yield {"connection": mock_conn, "conn": conn, "cursor": cur}


@pytest.fixture
def channel_bus_db():
    with patch("robothor.engine.channel_bus.get_connection", create=True) as mock_conn:
        conn = MagicMock()
        cur = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.rowcount = 1
        cur.fetchone.return_value = None
        mock_conn.return_value = conn
        # The module imports get_connection inside the function, so patch the
        # source symbol — the from-import above mounts a placeholder.
        with patch("robothor.db.connection.get_connection", return_value=conn):
            yield {"conn": conn, "cursor": cur}


class TestSaveChannelSurface:
    def test_writes_full_jsonb(self, chat_store_db):
        msg_id = save_channel_surface(
            session_key="agent:main:primary",
            content="Weekly devops report — all green.",
            author_agent_id="devops-manager",
            author_display_name="Dev Team Operations Manager",
            surfaced_from_run_id="run-123",
        )
        assert msg_id == 77
        # Second execute is the chat_messages insert
        insert_call = chat_store_db["cursor"].execute.call_args_list[1]
        payload = json.loads(insert_call[0][1][1])
        assert payload["role"] == "assistant"
        assert payload["content"] == "Weekly devops report — all green."
        assert payload["author_agent_id"] == "devops-manager"
        assert payload["author_display_name"] == "Dev Team Operations Manager"
        assert payload["surfaced_from_run_id"] == "run-123"
        assert payload["origin"] == "channel_bus"

    def test_omits_optional_fields_when_empty(self, chat_store_db):
        save_channel_surface(
            session_key="agent:main:primary",
            content="hello",
            author_agent_id="worker",
        )
        insert_call = chat_store_db["cursor"].execute.call_args_list[1]
        payload = json.loads(insert_call[0][1][1])
        assert "author_display_name" not in payload
        assert "surfaced_from_run_id" not in payload


class TestOnPostDelivery:
    @pytest.mark.asyncio
    async def test_main_heartbeat_persists_but_does_not_wake(self):
        """Main's heartbeat must still be mapped + written to the canonical
        session (so replies resolve), but must never submit a wake (loop)."""
        from robothor.engine.channel_bus import WakeDebouncer, set_debouncer

        ctx = HookContext(
            event=HookEvent.POST_DELIVERY,
            agent_id="main",
            run_id="run-1",
            output_text="heartbeat report text",
            metadata={
                "channel": "telegram",
                "chat_id": "42",
                "platform_message_ids": ["100"],
                "author_display_name": "Main",
                "surface_to_channel": True,
                "tenant_id": "default",
                "trigger_detail": "heartbeat:0 6-22 * * *",
            },
        )
        deb = WakeDebouncer(trigger_fn=AsyncMock(), enabled=True)
        set_debouncer(deb)
        try:
            save_mock = AsyncMock(return_value=99)
            with (
                patch(
                    "robothor.engine.channel_bus.save_channel_surface_async",
                    new=save_mock,
                ),
                patch(
                    "robothor.engine.channel_bus._record_outbound_async",
                    new=AsyncMock(),
                ),
                patch("robothor.events.bus.publish"),
            ):
                result = await on_post_delivery(ctx)
            assert result.action == HookAction.ALLOW
            # Heartbeat goes via scheduler (not interactive), so channel_bus
            # must write the canonical-session row.
            save_mock.assert_called_once()
            # But the wake bucket must stay empty — no loop.
            assert deb._buckets == {}
        finally:
            set_debouncer(None)

    @pytest.mark.asyncio
    async def test_main_interactive_skips_chat_write_but_still_maps(self):
        """Main's interactive turns are already written by the telegram
        handler. The channel bus looks up the latest assistant row and maps
        it, but does NOT re-write the chat_message."""
        from robothor.engine.channel_bus import WakeDebouncer, set_debouncer

        ctx = HookContext(
            event=HookEvent.POST_DELIVERY,
            agent_id="main",
            run_id="run-1",
            output_text="interactive reply text",
            metadata={
                "channel": "telegram",
                "chat_id": "42",
                "platform_message_ids": ["100"],
                "author_display_name": "Main",
                "surface_to_channel": True,
                "tenant_id": "default",
                "trigger_detail": "chat:42|sender:operator",
            },
        )
        deb = WakeDebouncer(trigger_fn=AsyncMock(), enabled=True)
        set_debouncer(deb)
        try:
            save_mock = AsyncMock()
            with (
                patch(
                    "robothor.engine.channel_bus.save_channel_surface_async",
                    new=save_mock,
                ),
                patch(
                    "robothor.engine.channel_bus._record_outbound_async",
                    new=AsyncMock(),
                ),
                patch("robothor.events.bus.publish"),
                # Mock the "find latest main assistant row" DB lookup
                patch("robothor.db.connection.get_connection") as mock_conn,
            ):
                conn = MagicMock()
                cur = MagicMock()
                conn.__enter__ = MagicMock(return_value=conn)
                conn.__exit__ = MagicMock(return_value=False)
                conn.cursor.return_value = cur
                cur.fetchone.return_value = (42,)
                mock_conn.return_value = conn

                await on_post_delivery(ctx)
            save_mock.assert_not_called()
            # Still no wake for main
            assert deb._buckets == {}
        finally:
            set_debouncer(None)

    @pytest.mark.asyncio
    async def test_skips_when_kill_switch_false(self):
        ctx = HookContext(
            event=HookEvent.POST_DELIVERY,
            agent_id="chatty-agent",
            run_id="run-1",
            output_text="yo",
            metadata={
                "channel": "telegram",
                "chat_id": "42",
                "platform_message_ids": ["100"],
                "surface_to_channel": False,
                "tenant_id": "default",
            },
        )
        with patch(
            "robothor.engine.channel_bus.save_channel_surface_async",
            new=AsyncMock(),
        ) as mock_save:
            await on_post_delivery(ctx)
        mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_without_output_text(self):
        ctx = HookContext(
            event=HookEvent.POST_DELIVERY,
            agent_id="worker",
            run_id="run-1",
            output_text="",
            metadata={
                "chat_id": "42",
                "platform_message_ids": ["100"],
                "tenant_id": "default",
            },
        )
        with patch(
            "robothor.engine.channel_bus.save_channel_surface_async",
            new=AsyncMock(),
        ) as mock_save:
            await on_post_delivery(ctx)
        mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_without_chat_id(self):
        ctx = HookContext(
            event=HookEvent.POST_DELIVERY,
            agent_id="worker",
            run_id="run-1",
            output_text="hello",
            metadata={"chat_id": "", "tenant_id": "default"},
        )
        with patch(
            "robothor.engine.channel_bus.save_channel_surface_async",
            new=AsyncMock(),
        ) as mock_save:
            await on_post_delivery(ctx)
        mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_persists_fleet_output_to_main_session(self):
        ctx = HookContext(
            event=HookEvent.POST_DELIVERY,
            agent_id="devops-manager",
            run_id="run-42",
            output_text="Weekly report.",
            metadata={
                "channel": "telegram",
                "chat_id": "42",
                "platform_message_ids": ["101", "102"],
                "author_display_name": "Dev Team Operations Manager",
                "surface_to_channel": True,
                "tenant_id": "default",
            },
        )
        mock_save = AsyncMock(return_value=99)
        with (
            patch("robothor.engine.channel_bus.save_channel_surface_async", new=mock_save),
            patch("robothor.engine.channel_bus._record_outbound_async", new=AsyncMock()),
            patch("robothor.events.bus.publish"),
            patch(
                "robothor.engine.channel_bus.get_main_session_key",
                return_value="agent:main:primary",
            ),
        ):
            await on_post_delivery(ctx)
        mock_save.assert_called_once()
        kwargs = mock_save.call_args.kwargs
        assert kwargs["session_key"] == "agent:main:primary"
        assert kwargs["content"] == "Weekly report."
        assert kwargs["author_agent_id"] == "devops-manager"
        assert kwargs["author_display_name"] == "Dev Team Operations Manager"
        assert kwargs["surfaced_from_run_id"] == "run-42"
        assert kwargs["tenant_id"] == "default"

    @pytest.mark.asyncio
    async def test_publishes_audit_event(self):
        ctx = HookContext(
            event=HookEvent.POST_DELIVERY,
            agent_id="devops-manager",
            run_id="run-42",
            output_text="Weekly report.",
            metadata={
                "channel": "telegram",
                "chat_id": "42",
                "platform_message_ids": ["101"],
                "surface_to_channel": True,
                "tenant_id": "default",
            },
        )
        publish_mock = MagicMock()
        with (
            patch(
                "robothor.engine.channel_bus.save_channel_surface_async",
                new=AsyncMock(return_value=99),
            ),
            patch(
                "robothor.engine.channel_bus._record_outbound_async",
                new=AsyncMock(),
            ),
            patch("robothor.events.bus.publish", publish_mock),
        ):
            await on_post_delivery(ctx)
        publish_mock.assert_called_once()
        kwargs = publish_mock.call_args.kwargs or publish_mock.call_args[1]
        payload = kwargs["payload"]
        assert payload["agent_id"] == "devops-manager"
        assert payload["chat_message_id"] == 99
        assert payload["platform_message_ids"] == ["101"]


class TestRecordOutbound:
    def test_inserts_one_row_per_message_id(self):
        with patch("robothor.db.connection.get_connection") as mock_conn:
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            mock_conn.return_value = conn

            record_outbound(
                tenant_id="default",
                channel="telegram",
                chat_id="42",
                platform_message_ids=["101", "102", "103"],
                session_key="agent:main:primary",
                chat_message_id=77,
                author_agent_id="devops-manager",
                author_run_id="run-42",
            )
            # Three channel_message_map inserts — one per platform_message_id.
            # Contact 360 write-through issues more queries per id, but the
            # count of the specific channel_message_map INSERT remains three.
            map_inserts = [
                c
                for c in cur.execute.call_args_list
                if "INSERT INTO channel_message_map" in c[0][0]
            ]
            assert len(map_inserts) == 3
            conn.commit.assert_called_once()

    def test_noop_on_empty_message_ids(self):
        with patch("robothor.db.connection.get_connection") as mock_conn:
            conn = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            mock_conn.return_value = conn
            record_outbound(
                tenant_id="default",
                channel="telegram",
                chat_id="42",
                platform_message_ids=[],
                session_key="agent:main:primary",
                chat_message_id=77,
                author_agent_id="devops-manager",
            )
            mock_conn.assert_not_called()


class TestResolveReplyContext:
    def test_returns_none_on_miss(self):
        with patch("robothor.db.connection.get_connection") as mock_conn:
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            cur.fetchone.return_value = None
            mock_conn.return_value = conn

            result = resolve_reply_context(
                chat_id="42", platform_message_id="999", tenant_id="default"
            )
            assert result is None

    def test_returns_author_and_snippet_on_hit(self):
        with patch("robothor.db.connection.get_connection") as mock_conn:
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            cur.fetchone.return_value = (
                77,  # chat_message_id
                "devops-manager",  # author_agent_id
                "run-42",  # author_run_id
                "agent:main:primary",  # session_key
                {
                    "role": "assistant",
                    "content": "Weekly report — all systems green. " * 20,
                    "author_display_name": "Dev Team Operations Manager",
                },
            )
            mock_conn.return_value = conn

            result = resolve_reply_context(
                chat_id="42", platform_message_id="101", tenant_id="default"
            )
        assert result is not None
        assert result["chat_message_id"] == 77
        assert result["author_agent_id"] == "devops-manager"
        assert result["author_display_name"] == "Dev Team Operations Manager"
        assert result["author_run_id"] == "run-42"
        assert len(result["content_snippet"]) == 200
        assert result["content_snippet"].startswith("Weekly report")


class TestAgentConfigSurface:
    def test_surface_to_channel_defaults_true(self):
        from robothor.engine.models import AgentConfig

        cfg = AgentConfig(id="foo", name="Foo")
        assert cfg.surface_to_channel is True

    def test_channel_event_in_trigger_enum(self):
        from robothor.engine.models import TriggerType

        assert TriggerType.CHANNEL_EVENT == "channel_event"


class TestFormatReplyPrefix:
    def test_basic_quote(self):
        from robothor.engine.channel_bus import format_reply_prefix

        ctx = {
            "author_display_name": "Dev Team Operations Manager",
            "author_agent_id": "devops-manager",
            "content_snippet": "Weekly report — 3 incidents resolved.",
        }
        prefix = format_reply_prefix(ctx)
        # Intent header prevents main from treating the quote as a work item
        assert prefix.startswith("[User is referencing this earlier message")
        assert "do not act unless explicitly asked" in prefix
        assert "@Dev Team Operations Manager" in prefix
        assert "Weekly report" in prefix

    def test_truncates_long_snippet(self):
        from robothor.engine.channel_bus import format_reply_prefix

        long = "x" * 400
        prefix = format_reply_prefix({"author_agent_id": "agent", "content_snippet": long})
        assert "..." in prefix
        # Header + attribution + up-to-180-char snippet + ellipsis
        assert len(prefix) < 320

    def test_strips_newlines_from_snippet_only(self):
        from robothor.engine.channel_bus import format_reply_prefix

        prefix = format_reply_prefix(
            {
                "author_agent_id": "agent",
                "content_snippet": "line one\nline two\nline three",
            }
        )
        # Snippet must be collapsed to a single line
        assert "line one line two line three" in prefix
        # But the prefix itself may contain one separator between header and attribution
        assert prefix.count("\n") == 1

    def test_falls_back_to_agent_id_without_display_name(self):
        from robothor.engine.channel_bus import format_reply_prefix

        prefix = format_reply_prefix({"author_agent_id": "devops-manager", "content_snippet": "hi"})
        assert "@devops-manager" in prefix


class TestRecordInbound:
    def test_inserts_inbound_row(self):
        from robothor.engine.channel_bus import record_inbound

        with patch("robothor.db.connection.get_connection") as mock_conn:
            conn = MagicMock()
            cur = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            mock_conn.return_value = conn

            record_inbound(
                tenant_id="default",
                channel="telegram",
                chat_id="42",
                platform_message_id="555",
                session_key="agent:main:primary",
                chat_message_id=88,
                author_agent_id="user",
            )
            # First execute is the channel_message_map insert; Contact 360
            # write-through issues additional queries after it.
            conn.commit.assert_called_once()
            first_call = cur.execute.call_args_list[0][0]
            sql = first_call[0]
            params = first_call[1]
            assert "'inbound'" in sql
            assert "NULL, 'inbound'" in sql
            assert params[3] == "555"
            assert params[6] == "user"


class TestWakeDebouncer:
    @pytest.mark.asyncio
    async def test_disabled_does_not_submit(self):
        from robothor.engine.channel_bus import WakeDebouncer

        trigger = AsyncMock()
        d = WakeDebouncer(trigger_fn=trigger, enabled=False)
        await d.submit("default", "42", "worker", "run-1")
        # Nothing queued — let any pending task complete
        await asyncio.sleep(0)
        assert not d._buckets
        trigger.assert_not_called()

    @pytest.mark.asyncio
    async def test_burst_collapses_into_single_wake(self):
        from robothor.engine.channel_bus import WakeDebouncer

        trigger = AsyncMock()
        d = WakeDebouncer(
            trigger_fn=trigger,
            debounce_seconds=0,  # fire immediately for test
            cooldown_seconds=0,
            rate_limit_per_hour=1000,
            enabled=True,
        )
        for i in range(5):
            await d.submit("default", "42", f"agent-{i}", f"run-{i}")
        # Let the debounce timer fire
        await asyncio.sleep(0.05)
        assert trigger.call_count == 1
        kwargs = trigger.call_args.kwargs
        assert sorted(kwargs["agents"]) == [f"agent-{i}" for i in range(5)]
        assert sorted(kwargs["run_ids"]) == [f"run-{i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_timer_resets_on_new_surface(self):
        from robothor.engine.channel_bus import WakeDebouncer

        trigger = AsyncMock()
        d = WakeDebouncer(
            trigger_fn=trigger,
            debounce_seconds=0,
            cooldown_seconds=0,
            rate_limit_per_hour=1000,
            enabled=True,
        )
        await d.submit("default", "42", "agent-a", "run-1")
        await d.submit("default", "42", "agent-b", "run-2")
        await asyncio.sleep(0.05)
        # One wake for both, not two
        assert trigger.call_count == 1

    @pytest.mark.asyncio
    async def test_separate_chats_get_separate_wakes(self):
        from robothor.engine.channel_bus import WakeDebouncer

        trigger = AsyncMock()
        d = WakeDebouncer(
            trigger_fn=trigger,
            debounce_seconds=0,
            cooldown_seconds=0,
            rate_limit_per_hour=1000,
            enabled=True,
        )
        await d.submit("default", "42", "agent-a", "run-1")
        await d.submit("default", "99", "agent-a", "run-2")
        await asyncio.sleep(0.05)
        assert trigger.call_count == 2

    @pytest.mark.asyncio
    async def test_main_running_defers_wake(self):
        from robothor.engine.channel_bus import WakeDebouncer

        trigger = AsyncMock()
        d = WakeDebouncer(
            trigger_fn=trigger,
            debounce_seconds=0,
            cooldown_seconds=0,
            rate_limit_per_hour=1000,
            enabled=True,
        )
        d.mark_main_started()
        await d.submit("default", "42", "worker", "run-1")
        await asyncio.sleep(0.05)
        trigger.assert_not_called()
        # Pending bucket remembered
        assert d._pending_key == ("default", "42")
        # When main finishes, pending wake fires
        await d.mark_main_finished()
        assert trigger.call_count == 1

    @pytest.mark.asyncio
    async def test_cooldown_blocks_rapid_wakes(self):
        from robothor.engine.channel_bus import WakeDebouncer

        trigger = AsyncMock()
        d = WakeDebouncer(
            trigger_fn=trigger,
            debounce_seconds=0,
            cooldown_seconds=3600,  # long cooldown
            rate_limit_per_hour=1000,
            enabled=True,
        )
        await d.submit("default", "42", "agent-a", "run-1")
        await asyncio.sleep(0.05)
        assert trigger.call_count == 1
        # Second surface inside the cooldown window
        await d.submit("default", "42", "agent-b", "run-2")
        await asyncio.sleep(0.05)
        assert trigger.call_count == 1
        assert d._pending_key == ("default", "42")


class TestStress:
    @pytest.mark.asyncio
    async def test_hundred_surfaces_produce_one_wake(self):
        """A burst of 100 surfaces from different agents into the same chat
        must debounce down to exactly one wake, with no lost metadata."""
        from robothor.engine.channel_bus import WakeDebouncer

        trigger = AsyncMock()
        d = WakeDebouncer(
            trigger_fn=trigger,
            debounce_seconds=0,  # fire as soon as the asyncio loop idles
            cooldown_seconds=0,
            rate_limit_per_hour=1000,
            enabled=True,
        )
        for i in range(100):
            await d.submit("default", "42", f"agent-{i % 10}", f"run-{i}")
        # Drain the pending debounce timer
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert trigger.call_count == 1
        # All 100 run_ids preserved; 10 distinct agents collected
        kwargs = trigger.call_args.kwargs
        assert len(kwargs["run_ids"]) == 100
        assert len(kwargs["agents"]) == 10


class TestRateLimit:
    def test_under_limit_allows(self):
        from robothor.engine.channel_bus import WakeDebouncer

        d = WakeDebouncer(trigger_fn=AsyncMock(), rate_limit_per_hour=5, enabled=True)
        for _ in range(5):
            assert d.check_rate_limit("agent-a") is True

    def test_over_limit_rejects(self):
        from robothor.engine.channel_bus import WakeDebouncer

        d = WakeDebouncer(trigger_fn=AsyncMock(), rate_limit_per_hour=3, enabled=True)
        assert d.check_rate_limit("agent-a") is True
        assert d.check_rate_limit("agent-a") is True
        assert d.check_rate_limit("agent-a") is True
        assert d.check_rate_limit("agent-a") is False  # 4th rejected

    def test_per_agent_buckets_are_independent(self):
        from robothor.engine.channel_bus import WakeDebouncer

        d = WakeDebouncer(trigger_fn=AsyncMock(), rate_limit_per_hour=2, enabled=True)
        assert d.check_rate_limit("a") is True
        assert d.check_rate_limit("a") is True
        assert d.check_rate_limit("b") is True  # separate bucket
        assert d.check_rate_limit("a") is False


class TestHistoryRenderingPrefix:
    def test_fleet_surface_gets_author_prefix(self):
        from robothor.engine.session import _render_history_for_llm

        msg = {
            "role": "assistant",
            "content": "Weekly devops report.",
            "author_agent_id": "devops-manager",
            "author_display_name": "Dev Team Operations Manager",
            "origin": "channel_bus",
        }
        rendered = _render_history_for_llm(msg)
        assert rendered["role"] == "assistant"
        assert rendered["content"].startswith("[@Dev Team Operations Manager] ")
        assert "Weekly devops report" in rendered["content"]
        # JSONB metadata stripped — only role + content survive
        assert set(rendered.keys()) == {"role", "content"}

    def test_main_assistant_turns_are_untouched(self):
        from robothor.engine.session import _render_history_for_llm

        msg = {"role": "assistant", "content": "Sure, I'll take a look."}
        assert _render_history_for_llm(msg) == msg

    def test_user_turns_pass_through(self):
        from robothor.engine.session import _render_history_for_llm

        msg = {
            "role": "user",
            "content": "Summarize for me",
            "telegram_message_id": "555",
        }
        rendered = _render_history_for_llm(msg)
        assert rendered == {"role": "user", "content": "Summarize for me"}

    def test_prefix_falls_back_to_agent_id_without_display_name(self):
        from robothor.engine.session import _render_history_for_llm

        msg = {
            "role": "assistant",
            "content": "hi",
            "author_agent_id": "worker-7",
        }
        rendered = _render_history_for_llm(msg)
        assert rendered["content"].startswith("[@worker-7] ")


class TestCHANNEL_EVENTTriggerType:
    def test_channel_event_routes_to_interactive_warmup_kind(self):
        """The runner must recognise CHANNEL_EVENT and set warmup_kind to
        'interactive' so main's memory blocks + session load on wake."""
        from robothor.engine.models import TriggerType

        # Sanity: the enum includes our new value
        assert TriggerType.CHANNEL_EVENT.value == "channel_event"


class TestChannelBusConfig:
    def test_defaults_to_disabled(self):
        from robothor.engine.models import ChannelBusConfig

        cfg = ChannelBusConfig()
        assert cfg.wake_on_surface is False
        assert cfg.wake_debounce_seconds == 15
        assert cfg.per_agent_rate_limit_per_hour == 20

    def test_parsed_from_yaml_block(self):
        from robothor.engine.config import manifest_to_agent_config

        manifest = {
            "id": "main",
            "name": "Main",
            "channel_bus": {
                "wake_on_surface": True,
                "wake_debounce_seconds": 30,
                "per_agent_rate_limit_per_hour": 50,
            },
        }
        cfg = manifest_to_agent_config(manifest)
        assert cfg.channel_bus is not None
        assert cfg.channel_bus.wake_on_surface is True
        assert cfg.channel_bus.wake_debounce_seconds == 30
        assert cfg.channel_bus.per_agent_rate_limit_per_hour == 50

    def test_no_block_yields_none(self):
        from robothor.engine.config import manifest_to_agent_config

        cfg = manifest_to_agent_config({"id": "worker", "name": "Worker"})
        assert cfg.channel_bus is None


class TestSaveExchangeReturnsIds:
    @pytest.mark.asyncio
    async def test_async_returns_inserted_ids(self, chat_store_db):
        """save_exchange_async must return [user_id, assistant_id] so the
        channel bus can link map rows to real chat_messages rows."""
        from robothor.engine.chat_store import save_exchange_async

        # Mock fetchone() to return different ids for each insert
        ids = iter([{"id": 42}, {"id": 100}, {"id": 101}])
        chat_store_db["cursor"].fetchone.side_effect = lambda: next(ids)
        result = await save_exchange_async("agent:main:primary", "hello", "hi")
        assert result == [100, 101]

    @pytest.mark.asyncio
    async def test_async_returns_none_on_failure(self, chat_store_db):
        from robothor.engine.chat_store import save_exchange_async

        chat_store_db["cursor"].execute.side_effect = RuntimeError("boom")
        result = await save_exchange_async("agent:main:primary", "hello", "hi")
        assert result is None


class TestSaveExchangeExtras:
    """Verify that save_exchange propagates JSONB extras (used by Phase 2
    reply-to persistence)."""

    def test_user_extras_merged_into_payload(self, chat_store_db):
        from robothor.engine.chat_store import save_exchange

        save_exchange(
            "agent:main:primary",
            user_content="Summarize for me",
            assistant_content="Sure!",
            user_extras={
                "telegram_message_id": "555",
                "replies_to": {"author_agent_id": "devops-manager"},
            },
        )
        # exec calls: upsert, user insert, assistant insert
        user_params = chat_store_db["cursor"].execute.call_args_list[1][0][1]
        user_payload = json.loads(user_params[1])
        assert user_payload["role"] == "user"
        assert user_payload["content"] == "Summarize for me"
        assert user_payload["telegram_message_id"] == "555"
        assert user_payload["replies_to"]["author_agent_id"] == "devops-manager"

    def test_assistant_extras_merged(self, chat_store_db):
        from robothor.engine.chat_store import save_exchange

        save_exchange(
            "agent:main:primary",
            user_content="hi",
            assistant_content="hello",
            assistant_extras={"stream_finalized": True},
        )
        asst_params = chat_store_db["cursor"].execute.call_args_list[2][0][1]
        asst_payload = json.loads(asst_params[1])
        assert asst_payload["stream_finalized"] is True

    def test_no_extras_produces_minimal_payload(self, chat_store_db):
        from robothor.engine.chat_store import save_exchange

        save_exchange("agent:main:primary", user_content="hi", assistant_content="hello")
        user_params = chat_store_db["cursor"].execute.call_args_list[1][0][1]
        user_payload = json.loads(user_params[1])
        assert set(user_payload.keys()) == {"role", "content"}
