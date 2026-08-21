"""The alert digest must have a READER, not just a writer.

``robothor/engine/alerts.py`` pages Telegram only for ``level='critical'``.
Every ``warning``/``info`` alert is written as an ``alert_digest`` row in
``crm_agent_notifications`` addressed to ``main`` — and until this module's
feature landed, **nothing read that table**. ``feature_flags.py`` says it
outright: "there is no read/list tool, and nothing in warmup or the heartbeat
reads ``crm_agent_notifications``. A row alone reaches nobody."

So every warning-level alert the platform ever raised — unresolvable manifest
tools, model-breaker trips, workflow failures, failed pages that fell back to
``alert_fallback`` — landed on a write-only surface.

These tests pin the reader, and pin the acknowledgement discipline that keeps
it honest: a row is acknowledged only once its text has *verifiably survived*
into the delivered preamble. That mirrors ``alerts.py``'s ``delivered =
bool(sent)`` — the arity bug hid behind exactly the assumption that a send
that was attempted was a send that happened.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from robothor.engine.models import AgentConfig
from robothor.engine.warmup import (
    _CONTEXT_HOOKS,
    ALERT_SECTION_HEADER,
    MAX_ALERT_ROWS,
    MAX_ALERT_SECTION_CHARS,
    OPERATOR_INBOX_AGENT_ID,
    build_interactive_preamble,
    build_warmth_preamble,
)

if TYPE_CHECKING:
    from pathlib import Path

BLOCK_PATCH = "robothor.memory.blocks.read_block"
TRACKING_PATCH = "robothor.engine.tracking.get_schedule"
INBOX_PATCH = "robothor.crm.dal.get_agent_inbox"
ACK_PATCH = "robothor.crm.dal.acknowledge_notification"


def _digest_row(
    subject: str,
    *,
    body: str = "details",
    age_minutes: int = 30,
    notification_type: str = "alert_digest",
    row_id: str | None = None,
) -> dict[str, Any]:
    """One ``get_agent_inbox`` row in ``notification_to_dict`` (camelCase) shape."""
    created = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return {
        "id": row_id or str(uuid.uuid4()),
        "tenantId": "default",
        "fromAgent": "engine",
        "toAgent": OPERATOR_INBOX_AGENT_ID,
        "notificationType": notification_type,
        "subject": subject,
        "body": body,
        "metadata": {},
        "taskId": None,
        "readAt": None,
        "acknowledgedAt": None,
        "createdAt": created.isoformat(),
    }


def _inbox(rows: list[dict[str, Any]]):
    """A ``get_agent_inbox`` stub that answers per ``type_filter``.

    The reader queries once per alert notification type, so a blanket
    ``return_value`` would double every row.
    """

    def _side_effect(**kwargs: Any) -> list[dict[str, Any]]:
        wanted = kwargs.get("type_filter")
        return [r for r in rows if r["notificationType"] == wanted]

    return _side_effect


@pytest.fixture
def no_context_hooks():
    """Context hooks reach out to date/travel/weather providers — mute them."""
    saved = _CONTEXT_HOOKS.copy()
    _CONTEXT_HOOKS.clear()
    try:
        yield
    finally:
        _CONTEXT_HOOKS[:] = saved


def _interactive(agent_id: str = OPERATOR_INBOX_AGENT_ID, **kwargs: Any) -> str:
    """Build an interactive preamble with every non-alert section muted."""
    with (
        patch(BLOCK_PATCH, return_value=None),
        patch("robothor.crm.dal.list_tasks", return_value=[]),
        patch("robothor.engine.warmup._recent_fleet_surfaces", return_value=""),
    ):
        return build_interactive_preamble(
            agent_id,
            user_message="hello",
            include_blocks=False,
            sender_name="",
            **kwargs,
        )


class TestUnreadAlertsSection:
    """(a) renders and names the count, (b) absent when empty, (c) capped."""

    def test_section_renders_and_names_the_count(self, no_context_hooks) -> None:
        rows = [
            _digest_row("[warning] Agent 'auto-agent' declares unavailable tools"),
            _digest_row("[warning] Model breaker opened for openrouter/x"),
            _digest_row("[info] Backup pruned 148GB of WAL"),
        ]
        with (
            patch(INBOX_PATCH, side_effect=_inbox(rows)) as inbox,
            patch(ACK_PATCH, return_value=True),
        ):
            result = _interactive()

        assert inbox.called, "warmup never read crm_agent_notifications at all"
        assert ALERT_SECTION_HEADER in result, (
            "the alert digest still has no reader — warning-level alerts are "
            "written to crm_agent_notifications and surfaced to nobody"
        )
        assert "3" in result.split("\n", 1)[0] or "(3)" in result
        assert "declares unavailable tools" in result
        assert "Model breaker opened" in result

    def test_no_rows_means_no_section(self, no_context_hooks) -> None:
        """An empty inbox must add no empty-noise header to the preamble."""
        with (
            patch(INBOX_PATCH, side_effect=_inbox([])),
            patch(ACK_PATCH, return_value=True) as ack,
        ):
            result = _interactive()

        assert ALERT_SECTION_HEADER not in result
        assert not ack.called

    def test_section_respects_row_and_char_caps(self, no_context_hooks) -> None:
        rows = [_digest_row(f"[warning] noisy alert {i} " + "x" * 300) for i in range(40)]
        with (
            patch(INBOX_PATCH, side_effect=_inbox(rows)),
            patch(ACK_PATCH, return_value=True),
        ):
            result = _interactive()

        assert ALERT_SECTION_HEADER in result
        start = result.index(ALERT_SECTION_HEADER)
        section = result[start:].split("\n\n", 1)[0]
        assert len(section) <= MAX_ALERT_SECTION_CHARS, (
            f"alert section is {len(section)} chars, cap is {MAX_ALERT_SECTION_CHARS} — "
            "an uncapped section eats the whole warmup budget"
        )
        rendered = [ln for ln in section.split("\n") if ln.startswith("•")]
        assert 0 < len(rendered) <= MAX_ALERT_ROWS

    def test_section_is_operator_scoped(self, no_context_hooks) -> None:
        """Worker agents don't get the operator's alert inbox."""
        with (
            patch(INBOX_PATCH, side_effect=_inbox([_digest_row("[warning] boom")])) as inbox,
            patch(ACK_PATCH, return_value=True),
        ):
            result = _interactive(agent_id="email-classifier")

        assert ALERT_SECTION_HEADER not in result
        assert not inbox.called


class TestAcknowledgeOnlyWhatWasDelivered:
    """A row is acked when — and only when — its text really reached the run."""

    def test_surfaced_rows_are_acknowledged(self, no_context_hooks) -> None:
        rows = [_digest_row("[warning] first"), _digest_row("[warning] second")]
        with (
            patch(INBOX_PATCH, side_effect=_inbox(rows)),
            patch(ACK_PATCH, return_value=True) as ack,
        ):
            result = _interactive()

        assert ALERT_SECTION_HEADER in result
        acked = {
            c.args[0] if c.args else c.kwargs.get("notification_id") for c in ack.call_args_list
        }
        assert acked == {r["id"] for r in rows}, (
            "surfaced digest rows were never acknowledged — they will repeat forever"
        )

    def test_ack_rule_ignores_rows_absent_from_the_delivered_text(self) -> None:
        """The rule itself: an id that is not in the delivered text is not acked."""
        from robothor.engine.warmup import _ack_surfaced_alerts

        shown, cut = str(uuid.uuid4()), str(uuid.uuid4())
        with patch(ACK_PATCH, return_value=True) as ack:
            acked = _ack_surfaced_alerts([shown, cut], f"…id={shown}", "default")

        assert acked == 1
        assert [c.args[0] for c in ack.call_args_list] == [shown]

    def test_rows_truncated_out_of_the_preamble_are_not_acknowledged(
        self, no_context_hooks, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PROBE, don't trust silence.

        ``build_interactive_preamble`` hard-truncates at ``MAX_WARMTH_CHARS``
        *after* assembling sections. A row whose line was cut off reached
        nobody, so acking it would be the same "assume it sent" bug that hid
        the Telegram arity failure. Shrinking the budget puts the cut inside
        the alert section itself, which is where it can actually bite.
        """
        monkeypatch.setattr("robothor.engine.warmup.MAX_WARMTH_CHARS", 200)
        rows = [_digest_row(f"[warning] alert number {i}") for i in range(MAX_ALERT_ROWS)]
        with (
            patch(INBOX_PATCH, side_effect=_inbox(rows)) as inbox,
            patch(ACK_PATCH, return_value=True) as ack,
        ):
            result = _interactive()

        # Not a vacuous pass: the section really was built, then partly cut.
        assert inbox.called
        assert "[warmup truncated]" in result
        surfaced = {r["id"] for r in rows if r["id"] in result}
        cut = {r["id"] for r in rows} - surfaced
        assert cut, "test did not actually truncate any alert line"

        acked = {c.args[0] for c in ack.call_args_list}
        assert acked == surfaced, (
            "a digest row that was truncated out of the delivered preamble was "
            "marked acknowledged anyway — that is 'assume it sent' all over again"
        )

    def test_ack_failure_never_breaks_warmup(self, no_context_hooks) -> None:
        with (
            patch(INBOX_PATCH, side_effect=_inbox([_digest_row("[warning] boom")])),
            patch(ACK_PATCH, side_effect=RuntimeError("db down")),
        ):
            result = _interactive()

        assert ALERT_SECTION_HEADER in result


class TestHeartbeatWarmupSurfacesAlerts:
    """The heartbeat (cron run of the operator agent) is the other reader."""

    def test_cron_warmup_surfaces_alerts_for_operator_agent(
        self, no_context_hooks, tmp_path: Path
    ) -> None:
        config = AgentConfig(id=OPERATOR_INBOX_AGENT_ID, name="Main")
        rows = [_digest_row("[warning] heartbeat should see this")]
        with (
            patch(TRACKING_PATCH, return_value=None),
            patch(INBOX_PATCH, side_effect=_inbox(rows)),
            patch(ACK_PATCH, return_value=True) as ack,
        ):
            result, timings = build_warmth_preamble(config, tmp_path)

        assert ALERT_SECTION_HEADER in result
        assert "heartbeat should see this" in result
        assert "unread_alerts" in timings, (
            "every warmup section is timed and exception-guarded — this one must be too"
        )
        assert ack.called

    def test_cron_warmup_skips_alerts_for_worker_agents(
        self, no_context_hooks, tmp_path: Path
    ) -> None:
        config = AgentConfig(id="crm-hygiene", name="CRM Hygiene")
        with (
            patch(TRACKING_PATCH, return_value=None),
            patch(INBOX_PATCH, side_effect=_inbox([_digest_row("[warning] boom")])) as inbox,
        ):
            result, _ = build_warmth_preamble(config, tmp_path)

        assert ALERT_SECTION_HEADER not in result
        assert not inbox.called


class TestNotificationToolRegistrationParity:
    """(d) built-but-unregistered is a repeat offender in this repo.

    ``skill_view`` shipped as a handler with no schema and was invisible to
    every agent. The digest reader depends on the agent being able to LIST and
    ACKNOWLEDGE notifications, so pin those to the registry the runner actually
    advertises from — not to any single schema module.
    """

    REQUIRED = ("get_inbox", "ack_notification", "send_notification")

    def test_notification_tools_are_registered_in_the_real_registry(self) -> None:
        from robothor.engine.tools.registry import ToolRegistry

        schemas = ToolRegistry()._schemas
        missing = [n for n in self.REQUIRED if n not in schemas]
        assert not missing, (
            f"notification tools have handlers but no registered schema: {missing} — "
            "no agent is ever offered them, so the digest can be read but never acked"
        )

    def test_notification_handlers_and_schemas_agree(self) -> None:
        from robothor.engine.tools.dispatch import _collect_handlers
        from robothor.engine.tools.registry import ToolRegistry

        handlers = _collect_handlers()
        schemas = ToolRegistry()._schemas
        for name in self.REQUIRED:
            assert name in handlers, f"{name} has a schema but no handler — it would error"
            assert name in schemas, f"{name} has a handler but no schema — it is unreachable"

    def test_ack_tool_is_named_in_the_surfaced_section(self, no_context_hooks) -> None:
        """The section must tell the agent how to clear a row it acts on."""
        with (
            patch(INBOX_PATCH, side_effect=_inbox([_digest_row("[warning] boom")])),
            patch(ACK_PATCH, return_value=True),
        ):
            result = _interactive()

        assert "ack_notification" in result


class TestEndToEndDigestRoundTrip:
    """Fire a REAL warning alert and prove the operator's warmup catches it.

    Every assertion above stubs the inbox. This one drives the actual
    ``alerts.alert()`` writer into an in-memory stand-in for
    ``crm_agent_notifications`` and then reads it back through warmup — the
    write-only surface's full round trip, including the "and then it stops
    repeating" half that the acknowledgement exists for.
    """

    @staticmethod
    def _fake_store(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        import robothor.crm.dal as dal

        store: list[dict[str, Any]] = []

        def _send(**kwargs: Any) -> str:
            row = _digest_row(
                kwargs["subject"],
                body=kwargs.get("body") or "",
                age_minutes=0,
                notification_type=kwargs["notification_type"],
            )
            store.append(row)
            return str(row["id"])

        def _inbox_read(**kwargs: Any) -> list[dict[str, Any]]:
            return [
                r
                for r in store
                if r["notificationType"] == kwargs.get("type_filter") and r["readAt"] is None
            ]

        def _ack(notification_id: str, tenant_id: str = "default") -> bool:
            for r in store:
                if r["id"] == notification_id and r["acknowledgedAt"] is None:
                    r["readAt"] = r["acknowledgedAt"] = datetime.now(UTC).isoformat()
                    return True
            return False

        monkeypatch.setattr(dal, "send_notification", _send)
        monkeypatch.setattr(dal, "get_agent_inbox", _inbox_read)
        monkeypatch.setattr(dal, "acknowledge_notification", _ack)
        return store

    async def test_warning_alert_reaches_the_operator_and_then_clears(
        self, no_context_hooks, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from robothor.engine.alerts import alert

        store = self._fake_store(monkeypatch)

        assert await alert("warning", "Disk 91% full", "/ has 8GB free") is True
        assert store and store[0]["notificationType"] == "alert_digest"

        first = _interactive()
        assert ALERT_SECTION_HEADER in first
        assert "Disk 91% full" in first, (
            "a warning alert was written to crm_agent_notifications and the "
            "operator's warmup still never showed it"
        )

        # Acked on surface — the next turn must not repeat it.
        assert store[0]["acknowledgedAt"] is not None
        second = _interactive()
        assert ALERT_SECTION_HEADER not in second
        assert "Disk 91% full" not in second

    async def test_failed_critical_page_still_surfaces_as_fallback(
        self, no_context_hooks, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A critical page that did NOT deliver must not vanish silently."""
        from robothor.engine.alerts import alert

        store = self._fake_store(monkeypatch)
        # No Telegram sender wired up => the page cannot deliver.
        monkeypatch.setattr(
            "robothor.engine.delivery.get_telegram_sender", lambda: None, raising=False
        )

        delivered = await alert("critical", "PostgreSQL down", "3 consecutive ping failures")
        assert delivered is False
        assert store[0]["notificationType"] == "alert_fallback"

        result = _interactive()
        assert ALERT_SECTION_HEADER in result
        assert "PostgreSQL down" in result, (
            "a critical page that failed to deliver left an alert_fallback row "
            "that the operator's warmup never surfaced"
        )
