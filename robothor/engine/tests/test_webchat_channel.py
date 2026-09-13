"""The webchat channel: a member's own Helm session as a delivery surface.

Two rows are written for one send — the chat turn the member reads in the Helm
and the notification that puts it in their inbox — and the receipt counts what
came back from each, never "the call did not raise". A send that wrote one of
the two is a ``partial:``, because a message sitting in a session nobody has
open is not a message somebody received.

``ask`` is the other half. There is no inbound webchat socket, so the question
goes out over the run's own SSE stream and the answer comes back through the
row the bridge's ``/api/approvals/question/{id}`` settles: the engine learns by
polling the row it already owns rather than by a second endpoint nobody else
would use.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from robothor.engine import chat, run_status
from robothor.engine.channels.webchat import WebchatChannel
from robothor.engine.models import AgentConfig, AgentRun, RunStatus, TriggerType

MEMBER = "11111111-1111-4111-8111-111111111111"
OWNER = "22222222-2222-4222-8222-222222222222"


def _config(agent_id: str = "assistant") -> AgentConfig:
    return AgentConfig(id=agent_id, name="Assistant")


def _run() -> AgentRun:
    return AgentRun(id="run-1", status=RunStatus.COMPLETED, trigger_type=TriggerType.CRON)


def _identity(role: str, user_id: str):
    return SimpleNamespace(
        channel="webchat",
        identifier=user_id,
        verified=True,
        role=role,
        user_account_id=user_id,
        display_name="",
        tenant_id="test-tenant",
    )


class _Writes:
    """The two persistence calls the channel makes, recorded rather than mocked
    away: what each returned is the only evidence in the receipt."""

    def __init__(self, message_id: int | None = 7, notification_id: str | None = "notif-1") -> None:
        self.message_id = message_id
        self.notification_id = notification_id
        self.surfaces: list[dict] = []
        self.notifications: list[dict] = []

    async def save_channel_surface_async(self, session_key, content, author_agent_id, **kw):
        self.surfaces.append(
            {"session_key": session_key, "content": content, "agent": author_agent_id, **kw}
        )
        return self.message_id

    def send_notification(self, **kw):
        self.notifications.append(kw)
        return self.notification_id


@pytest.fixture
def writes(monkeypatch):
    from robothor.engine.channels import webchat as module

    w = _Writes()
    monkeypatch.setattr(
        module.chat_store, "save_channel_surface_async", w.save_channel_surface_async
    )
    monkeypatch.setattr(module, "_send_notification", w.send_notification)
    monkeypatch.setattr(
        module, "_resolve_identity", lambda channel, identifier, tenant: _identity("member", MEMBER)
    )
    return w


@pytest.fixture(autouse=True)
def _clean_sessions():
    chat._sessions.clear()
    yield
    chat._sessions.clear()


@pytest.fixture(autouse=True)
def _enforce_default(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_PER_USER_SESSIONS", raising=False)


# ─── send ────────────────────────────────────────────────────────────


class TestSend:
    @pytest.mark.asyncio
    async def test_send_writes_a_chat_turn_and_a_notification(self, writes):
        receipt = await WebchatChannel().send(MEMBER, "the briefing", config=_config(), run=_run())

        assert len(writes.surfaces) == 1
        assert len(writes.notifications) == 1
        assert writes.surfaces[0]["session_key"] == chat.derive_user_session_key(
            "assistant", MEMBER
        )
        assert writes.notifications[0]["to_agent"] == MEMBER
        assert writes.notifications[0]["notification_type"] == "info"
        assert writes.notifications[0]["metadata"]["kind"] == "webchat_delivery"
        # The notification is the readable-by-operators half. It points at the
        # chat row rather than naming the session that row belongs to.
        assert "session_key" not in writes.notifications[0]["metadata"]
        assert writes.notifications[0]["metadata"]["chat_message_id"] == "7"
        assert receipt.expected == 2
        assert receipt.acknowledged == 2
        assert receipt.complete is True
        assert receipt.platform_ids == ["7", "notif-1"]

    @pytest.mark.asyncio
    async def test_the_turn_is_readable_from_the_in_memory_session(self, writes):
        """``GET /chat/history`` reads RAM, and the DB only at boot. A delivery
        that skipped the in-memory session would be invisible in the Helm until
        the next engine restart."""
        await WebchatChannel().send(MEMBER, "the briefing", config=_config(), run=_run())

        session = chat.get_shared_session(chat.derive_user_session_key("assistant", MEMBER))
        assert session.history[-1]["role"] == "assistant"
        assert "the briefing" in session.history[-1]["content"]

    @pytest.mark.asyncio
    async def test_no_chat_row_means_no_turn_in_ram_either(self, writes):
        """The in-memory session must not outrun the table behind it.

        ``/chat/history`` reads RAM, and ``_restore_sessions`` refills RAM from
        the DB at boot. A turn appended without a row would show in the Helm
        until the next restart and then vanish — a message the member saw, then
        did not, with nothing anywhere saying it was never stored.
        """
        writes.message_id = None
        await WebchatChannel().send(MEMBER, "the briefing", config=_config(), run=_run())

        key = chat.derive_user_session_key("assistant", MEMBER)
        assert key not in chat._sessions or chat._sessions[key].history == []

    @pytest.mark.asyncio
    async def test_send_to_a_member_uses_the_derived_key(self, writes):
        await WebchatChannel().send(MEMBER, "hi", config=_config("main"), run=_run())
        assert writes.surfaces[0]["session_key"] == "agent:main:user:" + MEMBER

    @pytest.mark.asyncio
    async def test_send_to_the_owner_uses_the_shared_session_key(self, writes, monkeypatch):
        """The operator's webchat↔Telegram continuity, from the delivery side."""
        from robothor.engine.channels import webchat as module

        monkeypatch.setattr(
            module,
            "_resolve_identity",
            lambda channel, identifier, tenant: _identity("owner", OWNER),
        )
        await WebchatChannel().send(OWNER, "hi", config=_config("main"), run=_run())
        assert writes.surfaces[0]["session_key"] == chat.get_main_session_key()

    @pytest.mark.asyncio
    async def test_a_missing_notification_is_not_delivered(self, writes):
        writes.notification_id = None
        receipt = await WebchatChannel().send(MEMBER, "hi", config=_config(), run=_run())

        assert receipt.acknowledged == 1
        assert receipt.complete is False
        assert receipt.status == "failed:webchat_no_notification"

    @pytest.mark.asyncio
    async def test_a_missing_chat_row_is_not_delivered(self, writes):
        writes.message_id = None
        receipt = await WebchatChannel().send(MEMBER, "hi", config=_config(), run=_run())

        assert receipt.acknowledged == 1
        assert receipt.complete is False
        assert receipt.status == "failed:webchat_no_session_write"

    @pytest.mark.asyncio
    async def test_neither_row_written_is_a_failed_send(self, writes):
        writes.message_id = None
        writes.notification_id = None
        receipt = await WebchatChannel().send(MEMBER, "hi", config=_config(), run=_run())

        assert receipt.acknowledged == 0
        assert receipt.status == "failed:webchat_send"

    @pytest.mark.asyncio
    async def test_an_incomplete_send_leaves_the_run_undelivered(self, writes):
        from robothor.engine.delivery import apply_receipt

        writes.notification_id = None
        run = _run()
        receipt = await WebchatChannel().send(MEMBER, "hi", config=_config(), run=run)
        apply_receipt(run, "webchat", receipt)

        assert run.delivered_at is None
        assert run.delivery_status == "failed:webchat_no_notification"

    @pytest.mark.asyncio
    async def test_empty_and_unexpanded_targets_are_refused_before_any_write(self, writes):
        channel = WebchatChannel()
        empty = await channel.send("", "hi", config=_config(), run=_run())
        unexpanded = await channel.send("${HELM_USER}", "hi", config=_config(), run=_run())

        assert empty.status == "failed:webchat_no_target"
        assert unexpanded.status == "failed:webchat_unexpanded_target"
        assert writes.surfaces == [] and writes.notifications == []

    @pytest.mark.asyncio
    async def test_unknown_user_is_failed_webchat_unknown_user(self, writes, monkeypatch):
        from robothor.engine.channels import webchat as module

        monkeypatch.setattr(module, "_resolve_identity", lambda channel, identifier, tenant: None)
        receipt = await WebchatChannel().send(MEMBER, "hi", config=_config(), run=_run())

        assert receipt.status == "failed:webchat_unknown_user"
        assert writes.surfaces == [] and writes.notifications == []


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_reports_the_sessions_this_process_holds(self, monkeypatch):
        from robothor.engine.channels import webchat as module

        monkeypatch.setattr(module, "_probe_database", lambda: True)
        chat.get_shared_session("agent:main:user:someone")
        report = await WebchatChannel().health()

        assert report["ok"] is True
        assert report["sessions"] == 1

    @pytest.mark.asyncio
    async def test_health_never_raises(self, monkeypatch):
        from robothor.engine.channels import webchat as module

        def _boom():
            raise RuntimeError("the database is gone")

        monkeypatch.setattr(module, "_probe_database", _boom)
        report = await WebchatChannel().health()

        assert report["channel"] == "webchat"
        assert report["configured"] is True
        assert report["ok"] is False


class TestTheRegistry:
    def test_webchat_is_a_builtin_and_cannot_be_replaced_by_a_plugin(self):
        from robothor.engine.channels import BUILTIN_CHANNELS, get_channel, register_channel

        assert "webchat" in BUILTIN_CHANNELS
        assert isinstance(get_channel("webchat"), WebchatChannel)
        with pytest.raises(ValueError):
            register_channel("webchat", WebchatChannel())


# ─── ask ─────────────────────────────────────────────────────────────


class _Rows:
    """``agent_questions`` as the channel sees it: one row, read repeatedly."""

    def __init__(self, statuses, answer="Globex") -> None:
        self.statuses = list(statuses)
        self.answer = answer
        self.reads = 0

    def get_question(self, question_id, *, tenant_id=""):
        self.reads += 1
        status = self.statuses[min(self.reads - 1, len(self.statuses) - 1)]
        return SimpleNamespace(
            id=question_id,
            status=status,
            answer=self.answer if status == "answered" else None,
            expires_at=datetime.now(UTC) + timedelta(seconds=300),
        )


@pytest.fixture
def rows(monkeypatch):
    def _install(statuses, answer="Globex"):
        from robothor.engine.channels import webchat as module

        store = _Rows(statuses, answer)
        monkeypatch.setattr(module.agent_questions, "get_question", store.get_question)
        monkeypatch.setattr(module, "POLL_INTERVAL_S", 0.01)
        return store

    return _install


@pytest.fixture(autouse=True)
def _clean_status_sinks():
    run_status.reset_status_sinks()
    yield
    run_status.reset_status_sinks()


@pytest.fixture
def listening():
    """Register a status sink for ``run_id``, i.e. a browser reading the stream.

    Every test below that expects the channel to WAIT arms one: with nobody
    listening the channel refuses to wait at all (``NoListenerError``), because the
    question would be on nobody's screen.
    """
    events: list[dict] = []

    def _arm(run_id: str = "run-1") -> list[dict]:
        async def sink(event):
            events.append(event)

        run_status.register_status_sink(run_id, sink)
        return events

    return _arm


class TestAsk:
    @pytest.mark.asyncio
    async def test_ask_emits_over_the_runs_status_sink_with_the_row_id(self, rows, listening):
        rows(["answered"])
        seen = listening("run-1")
        answer = await WebchatChannel().ask(
            "Which vendor?",
            ["Acme", "Globex"],
            timeout=5,
            target=MEMBER,
            question_id="q-1",
            run_id="run-1",
        )

        assert answer == "Globex"
        assert seen[0]["event"] == "approval_required"
        assert seen[0]["id"] == "q-1"
        assert seen[0]["options"] == ["Acme", "Globex"]

    @pytest.mark.asyncio
    async def test_ask_returns_a_late_answer_written_through_the_row(self, rows, listening):
        """The browser answers through the bridge, which settles the row. The
        engine has no inbound webchat socket, so the row IS the return path."""
        store = rows(["pending", "answered"])
        listening("run-1")
        answer = await WebchatChannel().ask(
            "Which vendor?",
            ["Acme", "Globex"],
            timeout=5,
            target=MEMBER,
            question_id="q-1",
            run_id="run-1",
        )

        assert answer == "Globex"
        assert store.reads >= 2

    @pytest.mark.asyncio
    async def test_ask_returns_none_on_expiry_and_never_an_option(self, rows, listening):
        rows(["expired"])
        listening("run-1")
        answer = await WebchatChannel().ask(
            "Which vendor?",
            ["Acme", "Globex"],
            timeout=5,
            target=MEMBER,
            question_id="q-1",
            run_id="run-1",
        )
        assert answer is None

    @pytest.mark.asyncio
    async def test_ask_returns_none_when_the_clock_runs_out(self, rows, listening):
        rows(["pending"])
        listening("run-1")
        answer = await WebchatChannel().ask(
            "Which vendor?",
            ["Acme", "Globex"],
            timeout=0.05,
            target=MEMBER,
            question_id="q-1",
            run_id="run-1",
        )
        assert answer is None

    @pytest.mark.asyncio
    async def test_the_wait_does_not_overshoot_its_budget_by_a_poll_interval(
        self, monkeypatch, listening
    ):
        """``ask_user`` caps the wait strictly below the tool registry's own
        ``asyncio.timeout``. A poll loop that always slept a full interval before
        re-checking the deadline would spend that headroom and turn "nobody
        answered" into a tool failure."""
        from robothor.engine.channels import webchat as module

        monkeypatch.setattr(
            module.agent_questions,
            "get_question",
            lambda *a, **kw: SimpleNamespace(
                id="q-1", status="pending", answer=None, expires_at=None
            ),
        )
        monkeypatch.setattr(module, "POLL_INTERVAL_S", 10.0)
        listening("run-1")

        started = asyncio.get_running_loop().time()
        answer = await WebchatChannel().ask(
            "Which?", timeout=0.05, target=MEMBER, question_id="q-1", run_id="run-1"
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert answer is None
        assert elapsed < 1.0, f"waited {elapsed:.2f}s on a 0.05s budget"

    @pytest.mark.asyncio
    async def test_ask_refuses_to_wait_when_nobody_is_listening(self, rows):
        """``emit_status`` returns False when no sink took the event — i.e. the
        browser is not reading this run's stream, so the question is on nobody's
        screen.

        Waiting anyway spends the whole tool budget and then reports "asked and
        stayed silent", which is a claim about a prompt that was never displayed.
        ``NoListenerError`` is the documented "there is no way to ask here" outcome, so
        ``_ask_channel`` records ``delivered: false`` in the same tick.
        """
        from robothor.engine.channels.base import NoListenerError

        store = rows(["pending"])
        # No sink registered for this run — emit_status returns False.
        with pytest.raises(NoListenerError):
            await WebchatChannel().ask(
                "Which vendor?",
                ["Acme", "Globex"],
                timeout=30,
                target=MEMBER,
                question_id="q-1",
                run_id="run-nobody-home",
            )
        # One read to fetch the row, and then it gave up — it did not poll.
        assert store.reads == 1

    @pytest.mark.asyncio
    async def test_a_no_listener_refusal_is_a_kind_of_not_implemented_error(self):
        """Every existing caller catches ``NotImplementedError``
        (``ask_user._ask_channel``, ``PermissionEscalationManager``), so the more
        specific signal must not escape one that has not been taught about it."""
        from robothor.engine.channels.base import NoListenerError

        assert issubclass(NoListenerError, NotImplementedError)

    @pytest.mark.asyncio
    async def test_ask_without_a_question_id_raises_not_implemented(self):
        """No row is no return path, and ``None`` would mean "nobody replied"."""
        with pytest.raises(NotImplementedError):
            await WebchatChannel().ask("Which vendor?", target=MEMBER)

    @pytest.mark.asyncio
    async def test_ask_raises_when_the_row_cannot_be_read(self, monkeypatch):
        from robothor.engine.channels import webchat as module

        monkeypatch.setattr(
            module.agent_questions,
            "get_question",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db is gone")),
        )
        with pytest.raises(NotImplementedError):
            await WebchatChannel().ask("Which?", target=MEMBER, question_id="q-1")

    def test_the_channel_declares_it_wants_the_row_id(self):
        """The capability flag ``ask_user`` probes — Telegram and Slack do not
        set it, so their ``ask`` signatures are never handed a kwarg they
        cannot take."""
        assert WebchatChannel.ask_wants_question_id is True

    @pytest.mark.asyncio
    async def test_a_cancelled_wait_propagates_rather_than_answering(self, rows, listening):
        rows(["pending"])
        listening("run-1")
        channel = WebchatChannel()
        task = asyncio.create_task(
            channel.ask("Which?", timeout=30, target=MEMBER, question_id="q-1", run_id="run-1")
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestResolveIdentity:
    @pytest.mark.asyncio
    async def test_inbound_identity_resolution_is_not_this_channels_job(self):
        with pytest.raises(NotImplementedError):
            await WebchatChannel().resolve_identity(MEMBER)


class TestVerify:
    def test_there_is_no_verify(self):
        """``genus channel verify webchat`` exits 2 (nothing to verify) rather
        than running a probe that proves nothing about reach. Documented on the
        channel's doc page instead of faked here."""
        assert not hasattr(WebchatChannel, "verify")


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_and_stop_are_no_ops(self):
        channel = WebchatChannel()
        assert await channel.start() is None
        assert await channel.stop() is None


def test_patching_get_channel_is_not_how_the_registry_answers():
    """Guard against a test-only registration masking a missing built-in."""
    from robothor.engine.channels import registry

    with patch.object(registry, "_builtins_registered", False):
        registry.reset_channels()
        assert registry.get_channel("webchat") is not None
