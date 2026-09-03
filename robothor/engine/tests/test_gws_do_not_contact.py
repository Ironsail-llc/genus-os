"""Outbound email must refuse a recipient flagged ``do_not_contact``.

Migration 113 and the DAL give the flag a home and an editor. This is the
half that makes it a control: the send path reads it, and a blocked send
leaves a row in ``agent_guardrail_events`` so the operator can see the
guardrail acting rather than take its word for it.

Both outbound senders are covered. ``gws_gmail_send`` names its recipients
in the call; ``gws_gmail_reply`` derives them from the thread, which is the
more dangerous of the two — reply-all can address someone the agent never
typed, and that is precisely the person most likely to have asked to be left
alone.

The failure mode this file also pins is the lookup itself failing. "We could
not read the opt-out list" is not "nobody opted out", so an unreadable list
refuses the send. A fail-open guard here would be worse than no guard: it
would report itself as enforcing while mailing the people who opted out.

``_run_gws`` is mocked throughout — no CLI, no network, no database.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.tools.handlers import gws

RUN_ID = "11111111-2222-3333-4444-555555555555"

#: Only this address is flagged. Everyone else is either a known-but-willing
#: contact or absent from the CRM entirely, and both must be allowed through.
FLAGGED = {"bob@example.com"}


def _fake_lookup(emails, tenant_id="default"):
    return {e.strip().lower() for e in emails if e and e.strip().lower() in FLAGGED}


def _send(args: dict[str, Any], **kwargs: Any):
    """Run gws_gmail_send with the CRM lookup faked and the CLI stubbed."""
    with (
        patch("robothor.crm.dal.do_not_contact_emails", side_effect=_fake_lookup),
        patch.object(gws, "_run_gws", return_value={"id": "m1", "threadId": "t1"}) as run,
        patch.object(gws, "_record_sent_email"),
        patch("robothor.engine.tracking.log_guardrail_event") as audit,
    ):
        result = gws._handle_gws_tool("gws_gmail_send", args, **kwargs)
    return result, run, audit


class TestGmailSend:
    def test_flagged_recipient_is_refused(self):
        result, run, audit = _send(
            {"to": "bob@example.com", "subject": "Offer", "body": "hi"}, run_id=RUN_ID
        )

        assert "error" in result
        assert "do_not_contact" in result["guard"]
        assert "bob@example.com" in result["error"]
        run.assert_not_called()

    def test_refusal_is_recorded_as_a_guardrail_event(self):
        _, _, audit = _send(
            {"to": "bob@example.com", "subject": "Offer", "body": "hi"}, run_id=RUN_ID
        )

        audit.assert_called_once()
        kwargs = audit.call_args.kwargs
        assert audit.call_args.args[0] == RUN_ID
        assert audit.call_args.args[1] == "do_not_contact"
        assert audit.call_args.args[2] == "blocked"
        assert kwargs["tool_name"] == "gws_gmail_send"
        assert "bob@example.com" in kwargs["reason"]

    def test_case_and_display_name_forms_still_match(self):
        """Agents paste `Name <addr>` and mixed case; the flag is on the address."""
        result, run, _ = _send(
            {"to": "Bob Example <BOB@Example.com>", "subject": "s", "body": "b"},
            run_id=RUN_ID,
        )

        assert "error" in result
        run.assert_not_called()

    def test_a_flagged_cc_blocks_the_whole_send(self):
        result, run, _ = _send(
            {
                "to": "alice@example.com",
                "cc": "bob@example.com",
                "subject": "s",
                "body": "b",
            },
            run_id=RUN_ID,
        )

        assert "error" in result
        run.assert_not_called()

    def test_willing_recipient_proceeds(self):
        result, run, audit = _send(
            {"to": "alice@example.com", "subject": "s", "body": "b"}, run_id=RUN_ID
        )

        assert "error" not in result
        run.assert_called_once()
        audit.assert_not_called()

    def test_recipient_absent_from_the_crm_proceeds(self):
        """The flag is an opt-out list, not an allow-list. A stranger is not
        a violation — treating "unknown" as blocked would take out cold
        outreach entirely."""
        result, run, audit = _send(
            {"to": "nobody@example.org", "subject": "s", "body": "b"}, run_id=RUN_ID
        )

        assert "error" not in result
        run.assert_called_once()
        audit.assert_not_called()

    def test_an_unreadable_opt_out_list_refuses_the_send(self):
        with (
            patch(
                "robothor.crm.dal.do_not_contact_emails",
                side_effect=RuntimeError("connection refused"),
            ),
            patch.object(gws, "_run_gws") as run,
            patch("robothor.engine.tracking.log_guardrail_event") as audit,
        ):
            result = gws._handle_gws_tool(
                "gws_gmail_send",
                {"to": "alice@example.com", "subject": "s", "body": "b"},
                run_id=RUN_ID,
            )

        assert "error" in result
        run.assert_not_called()
        assert audit.call_args.args[2] == "blocked"
        assert "could not be checked" in audit.call_args.kwargs["reason"]

    def test_a_missing_column_is_a_pending_migration_not_a_refusal(self):
        """The one carve-out in the fail-closed rule.

        Code and schema do not land in the same instant. If the deploy beats
        `robothor migrate`, `crm_people.do_not_contact` does not exist yet, and
        failing closed there would take every outbound email on the instance
        down until someone noticed — an outage caused by a guard whose whole
        job is to block a handful of addresses. Pre-migration the flag cannot
        have been set by anyone, so nobody has opted out and the pre-113
        behaviour is the correct one. It is logged at ERROR because the state
        is temporary and someone has to see it if it is not.
        """
        from psycopg2.errors import UndefinedColumn

        with (
            patch(
                "robothor.crm.dal.do_not_contact_emails",
                side_effect=UndefinedColumn("column p.do_not_contact does not exist"),
            ),
            patch.object(gws, "_run_gws", return_value={"id": "m1"}) as run,
            patch.object(gws, "_record_sent_email"),
            patch("robothor.engine.tracking.log_guardrail_event") as audit,
        ):
            result = gws._handle_gws_tool(
                "gws_gmail_send",
                {"to": "bob@example.com", "subject": "s", "body": "b"},
                run_id=RUN_ID,
            )

        assert "error" not in result
        run.assert_called_once()
        audit.assert_not_called()

    def test_no_run_id_still_refuses_but_writes_no_orphan_row(self):
        """agent_guardrail_events.run_id is NOT NULL and references agent_runs,
        so a call made outside a run must not attempt the write. The refusal
        itself is not conditional on there being somewhere to record it."""
        result, run, audit = _send({"to": "bob@example.com", "subject": "s", "body": "b"})

        assert "error" in result
        run.assert_not_called()
        audit.assert_not_called()


_THREAD = {
    "messages": [
        {
            "payload": {
                "headers": [
                    {"name": "From", "value": "bob@example.com"},
                    {"name": "To", "value": "alice@example.com"},
                    {"name": "Subject", "value": "Question"},
                    {"name": "Message-ID", "value": "<abc@example.com>"},
                ]
            }
        }
    ]
}


class TestGmailReply:
    def _reply(self, lookup):
        calls: list[list[str]] = []

        def _fake_run_gws(argv, timeout=None):
            calls.append(argv)
            return _THREAD

        with (
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=lookup),
            patch.object(gws, "_run_gws", side_effect=_fake_run_gws),
            patch.object(gws, "_record_sent_email"),
            patch.object(gws, "ROBOTHOR_EMAIL", "bot@example.com"),
            patch("robothor.engine.tracking.log_guardrail_event") as audit,
        ):
            result = gws._handle_gws_tool(
                "gws_gmail_reply",
                {"thread_id": "t1", "body": "here you go"},
                run_id=RUN_ID,
            )
        sends = [c for c in calls if "send" in c]
        return result, sends, audit

    def test_flagged_thread_participant_is_refused(self):
        result, sends, audit = self._reply(_fake_lookup)

        assert "error" in result
        assert sends == []
        assert audit.call_args.args[1] == "do_not_contact"
        assert audit.call_args.kwargs["tool_name"] == "gws_gmail_reply"

    def test_willing_thread_proceeds(self):
        result, sends, audit = self._reply(lambda emails, tenant_id="default": set())

        assert "error" not in result
        assert len(sends) == 1
        audit.assert_not_called()


class TestWiring:
    """A guard the dispatcher never reaches is the inert shape this repo keeps
    finding. The registered handler must hand the run and tenant down."""

    @pytest.mark.asyncio
    async def test_registered_handler_passes_run_and_tenant_through(self):
        from robothor.engine.tools.dispatch import ToolContext

        ctx = ToolContext(agent_id="a", run_id=RUN_ID, tenant_id="t1")
        with patch.object(gws, "_handle_gws_tool", return_value={"ok": True}) as inner:
            await gws.HANDLERS["gws_gmail_send"]({"to": "alice@example.com"}, ctx)

        assert inner.call_args.kwargs["run_id"] == RUN_ID
        assert inner.call_args.kwargs["tenant_id"] == "t1"
