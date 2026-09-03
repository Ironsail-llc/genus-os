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

#: A real call always carries one. The guard refuses without it — the opt-out
#: list is per-tenant, and a guard may not guess whose list it is reading.
TENANT = "tenant-a"

#: Only this address is flagged. Everyone else is either a known-but-willing
#: contact or absent from the CRM entirely, and both must be allowed through.
FLAGGED = {"bob@example.com"}


def _fake_lookup(emails, tenant_id="default"):
    return {e.strip().lower() for e in emails if e and e.strip().lower() in FLAGGED}


def _send_raising(exc: BaseException, **kwargs: Any):
    """Run gws_gmail_send with the CRM lookup raising `exc`."""
    kwargs.setdefault("run_id", RUN_ID)
    kwargs.setdefault("tenant_id", TENANT)
    with (
        patch("robothor.crm.dal.do_not_contact_emails", side_effect=exc),
        patch.object(gws, "_run_gws", return_value={"id": "m1"}) as run,
        patch.object(gws, "_record_sent_email"),
        patch("robothor.engine.tracking.log_guardrail_event") as audit,
    ):
        result = gws._handle_gws_tool(
            "gws_gmail_send",
            {"to": "alice@example.com", "subject": "s", "body": "b"},
            **kwargs,
        )
    return result, run, audit


def _send(args: dict[str, Any], **kwargs: Any):
    """Run gws_gmail_send with the CRM lookup faked and the CLI stubbed."""
    kwargs.setdefault("tenant_id", TENANT)
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
            patch("robothor.engine.tracking.log_guardrail_event"),
        ):
            result = gws._handle_gws_tool(
                "gws_gmail_send",
                {"to": "alice@example.com", "subject": "s", "body": "b"},
                run_id=RUN_ID,
                tenant_id=TENANT,
            )

        assert "error" in result
        run.assert_not_called()

    def test_an_unreadable_list_files_no_guardrail_row(self):
        """The evidence write goes to the same database the lookup just failed
        on, through its own connection. Trying it there buys nothing — it
        raises too — and the only thing it can produce is a second, more
        confusing traceback stacked on the real one. When the failure WAS the
        lookup, log it and stop; the tool error is what the agent sees, and
        the ERROR line is what the operator greps for.
        """
        from psycopg2.errors import UndefinedColumn

        for exc in (
            RuntimeError("connection refused"),
            UndefinedColumn("column crm_people.deleted_at does not exist"),
        ):
            result, run, audit = _send_raising(exc)
            assert "error" in result
            run.assert_not_called()
            audit.assert_not_called()

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
                tenant_id=TENANT,
            )

        assert "error" not in result
        run.assert_called_once()
        audit.assert_not_called()

    def test_an_unrelated_missing_column_is_not_the_carve_out(self):
        """The carve-out is scoped to the column the migration adds, not to
        `UndefinedColumn` as a class.

        The lookup SQL also names `crm_people.deleted_at`, `tenant_id`,
        `additional_emails` and the whole `contact_identifiers` table. A
        carve-out that keyed off the exception TYPE would turn any one of
        those going missing — a botched migration, a partial restore, a
        renamed column — into a silent allow, which is the failure this
        guard exists to prevent. Only "the flag's own column is not there
        yet" is a pending migration; everything else is an unreadable list.
        """
        from psycopg2.errors import UndefinedColumn

        result, run, _ = _send_raising(
            UndefinedColumn("column crm_people.deleted_at does not exist")
        )

        assert "error" in result
        run.assert_not_called()

    def test_a_missing_identifier_table_is_not_the_carve_out(self):
        from psycopg2.errors import UndefinedTable

        result, run, _ = _send_raising(
            UndefinedTable('relation "contact_identifiers" does not exist')
        )

        assert "error" in result
        run.assert_not_called()

    def test_an_unknown_tenant_refuses_rather_than_checking_default(self):
        """`tenant_id or DEFAULT_TENANT` is a silent fallback inside a guard.

        The opt-out list is per-tenant. If the caller could not say which
        tenant it is acting for, checking `default`'s list answers a question
        nobody asked: it can clear a recipient who is flagged in the tenant
        the send actually belongs to, and report itself as having checked.
        A guard may not guess whose list it is reading — an unknown tenant is
        an unknown answer, and an unknown answer refuses, the same as an
        unreadable list.
        """
        for absent in (None, "", "   "):
            result, run, _ = _send(
                {"to": "alice@example.com", "subject": "s", "body": "b"},
                run_id=RUN_ID,
                tenant_id=absent,
            )

            assert "error" in result, f"tenant_id={absent!r} should refuse"
            assert result["guard"] == "do_not_contact"
            run.assert_not_called()

    def test_the_default_tenant_is_only_used_when_it_was_actually_named(self):
        """Nothing here forbids DEFAULT_TENANT — a single-tenant instance names
        it explicitly and is checked against it. What is forbidden is reaching
        for it because the caller said nothing."""
        from robothor.constants import DEFAULT_TENANT

        seen: list[str] = []

        def _record(emails, tenant_id="unset"):
            seen.append(tenant_id)
            return set()

        with (
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=_record),
            patch.object(gws, "_run_gws", return_value={"id": "m1"}) as run,
            patch.object(gws, "_record_sent_email"),
        ):
            result = gws._handle_gws_tool(
                "gws_gmail_send",
                {"to": "alice@example.com", "subject": "s", "body": "b"},
                run_id=RUN_ID,
                tenant_id=DEFAULT_TENANT,
            )

        assert "error" not in result
        run.assert_called_once()
        assert seen == [DEFAULT_TENANT]

    def test_no_run_id_still_refuses_but_writes_no_orphan_row(self):
        """agent_guardrail_events.run_id is NOT NULL and references agent_runs,
        so a call made outside a run must not attempt the write. The refusal
        itself is not conditional on there being somewhere to record it."""
        result, run, audit = _send({"to": "bob@example.com", "subject": "s", "body": "b"})

        assert "error" in result
        run.assert_not_called()
        audit.assert_not_called()


class TestTransientDatabaseError:
    """Fail-closed is right for a compliance flag, but it must not be brittle.

    A dropped connection or a restarting Postgres is the common case, and it
    is over in under a second. Refusing the send on the first blip converts
    routine database churn into a mail outage — and an operator who watches
    that happen learns to distrust the guard, which is how a control ends up
    switched off. One bounded retry absorbs the blip; a second failure is a
    real outage and still refuses. One retry, not a loop: a retry loop inside
    a send path is how a transient fault becomes a hung run.
    """

    def _send_with_lookup(self, side_effect):
        calls: list[float] = []
        with (
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=side_effect) as lookup,
            patch.object(gws.time, "sleep", side_effect=calls.append),
            patch.object(gws, "_run_gws", return_value={"id": "m1"}) as run,
            patch.object(gws, "_record_sent_email"),
        ):
            result = gws._handle_gws_tool(
                "gws_gmail_send",
                {"to": "alice@example.com", "subject": "s", "body": "b"},
                run_id=RUN_ID,
                tenant_id=TENANT,
            )
        return result, run, lookup, calls

    def test_a_blip_is_retried_once_and_the_send_proceeds(self):
        from psycopg2 import OperationalError

        result, run, lookup, slept = self._send_with_lookup(
            [OperationalError("server closed the connection unexpectedly"), set()]
        )

        assert "error" not in result
        run.assert_called_once()
        assert lookup.call_count == 2
        assert slept == [gws._DNC_RETRY_DELAY_SECONDS]

    def test_a_second_failure_still_refuses(self):
        from psycopg2 import OperationalError

        result, run, lookup, _ = self._send_with_lookup(
            [
                OperationalError("could not connect to server"),
                OperationalError("could not connect to server"),
            ]
        )

        assert "error" in result
        run.assert_not_called()
        assert lookup.call_count == 2

    def test_the_retry_is_one_attempt_not_a_loop(self):
        """A retry loop in a send path turns a transient fault into a hung run."""
        from psycopg2 import OperationalError

        _, _, lookup, slept = self._send_with_lookup(
            OperationalError("could not connect to server")
        )

        assert lookup.call_count == 2
        assert len(slept) == 1

    def test_a_query_error_is_not_retried(self):
        """OperationalError is the connection-level class. A ProgrammingError
        means the query is wrong, and asking again gets the same answer."""
        from psycopg2 import ProgrammingError

        result, run, lookup, slept = self._send_with_lookup(
            ProgrammingError("syntax error at or near")
        )

        assert "error" in result
        run.assert_not_called()
        assert lookup.call_count == 1
        assert slept == []


class TestObserveMode:
    """The lever that makes fail-closed operable.

    Fail-closed is the right default for a compliance flag, but a default with
    no override is a default nobody can respond to: when the guard is wrong at
    3am the only lever left is editing code, and what actually happens is that
    someone comments out the call. `ROBOTHOR_DNC_MODE=observe` keeps the
    control watching and writing evidence while the mail flows, which is a
    strictly better state than the guard being deleted. It is read from the
    environment at call time so flipping it does not need a code change, and
    anything other than `observe` enforces.
    """

    def _send_in_mode(self, mode, lookup=_fake_lookup):
        env = {} if mode is None else {"ROBOTHOR_DNC_MODE": mode}
        with (
            patch.dict(gws.os.environ, env, clear=False),
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=lookup),
            patch.object(gws, "_run_gws", return_value={"id": "m1"}) as run,
            patch.object(gws, "_record_sent_email"),
            patch("robothor.engine.tracking.log_guardrail_event") as audit,
        ):
            if mode is None:
                gws.os.environ.pop("ROBOTHOR_DNC_MODE", None)
            result = gws._handle_gws_tool(
                "gws_gmail_send",
                {"to": "bob@example.com", "subject": "s", "body": "b"},
                run_id=RUN_ID,
                tenant_id=TENANT,
            )
        return result, run, audit

    def test_observe_lets_the_send_through(self):
        result, run, _ = self._send_in_mode("observe")

        assert "error" not in result
        run.assert_called_once()

    def test_observe_still_files_the_evidence_as_observed(self):
        """The whole point of observe is that it is not off. The row still
        lands, naming the address, so the operator can see what enforcing
        would have cost before turning it back on."""
        _, _, audit = self._send_in_mode("observe")

        audit.assert_called_once()
        assert audit.call_args.args[1] == "do_not_contact"
        assert audit.call_args.args[2] == "observed"
        assert audit.call_args.kwargs["mode"] == "observe"
        assert "bob@example.com" in audit.call_args.kwargs["reason"]

    def test_the_default_is_enforce(self):
        result, run, audit = self._send_in_mode(None)

        assert "error" in result
        run.assert_not_called()
        assert audit.call_args.args[2] == "blocked"

    def test_an_unrecognised_value_enforces(self):
        """A typo in an env var must not silently disable a compliance control."""
        for value in ("off", "disabled", "OBSERVE_ALL", ""):
            result, run, _ = self._send_in_mode(value)

            assert "error" in result, f"ROBOTHOR_DNC_MODE={value!r} must enforce"
            run.assert_not_called()

    def test_observe_is_case_and_whitespace_tolerant(self):
        result, run, _ = self._send_in_mode("  Observe  ")

        assert "error" not in result
        run.assert_called_once()

    def test_observe_also_lets_an_unreadable_list_through(self):
        """Observe means the guard cannot stop mail, whatever its reason for
        wanting to. A failure that refuses in observe mode would be enforce
        wearing the wrong label."""
        with (
            patch.dict(gws.os.environ, {"ROBOTHOR_DNC_MODE": "observe"}, clear=False),
            patch(
                "robothor.crm.dal.do_not_contact_emails",
                side_effect=RuntimeError("connection refused"),
            ),
            patch.object(gws, "_run_gws", return_value={"id": "m1"}) as run,
            patch.object(gws, "_record_sent_email"),
        ):
            result = gws._handle_gws_tool(
                "gws_gmail_send",
                {"to": "alice@example.com", "subject": "s", "body": "b"},
                run_id=RUN_ID,
                tenant_id=TENANT,
            )

        assert "error" not in result
        run.assert_called_once()


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
                tenant_id=TENANT,
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
