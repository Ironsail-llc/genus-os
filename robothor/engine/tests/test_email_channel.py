"""``EmailChannel`` — email as a delivery target, with the opt-out honoured.

Email was reachable only as an agent *tool* (``gws_gmail_send``), so a manifest
saying ``delivery.channel: email`` resolved to nothing and the run recorded
``failed:no_channel:email``. This is the outbound half as a channel, and the one
property every test here turns on is the one that makes it safe to hand an
address to:

**A recipient flagged ``do_not_contact`` never reaches a transport.** Not "the
send returns an error afterwards" — the transport object is never built and the
fake transport's call counter reads zero. A guard that refuses after the mail
has left is not a guard, and a test that only asserts the status would pass for
one.

The second property is the one the SMTP half introduces: **no credential
reaches a log record.** An ``smtplib`` exception carries the server's own reply,
and a failed ``AUTH PLAIN <base64>`` line carries the password inside it. So
every error goes through ``_describe`` and the redactor, and the proof is a
``caplog`` at DEBUG rather than an assertion about intent.

Every address here is ``@example.com`` and every credential is visibly fake:
this is platform code.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.channels.email import EmailChannel
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus

TENANT = "tenant-a"

WILLING = "alice@example.com"
FLAGGED = "bob@example.com"
PERSON_ID = "11111111-2222-3333-4444-555555555555"
FLAGGED_PERSON_ID = "99999999-8888-7777-6666-555555555555"

SENDER = "genus@example.com"
SMTP_HOST = "smtp.example.com"
SMTP_USER = "genus@example.com"

#: Visibly not a credential, and distinctive enough that searching a log for it
#: is a real test rather than a coincidence.
FAKE_SMTP_PASSWORD = "not-a-real-smtp-password-9999"

GWS_MESSAGE_ID = "18f0000000000000"


class _FakeGws:
    """The gws CLI runner, counted. ``calls`` is the evidence a refusal refused."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result if result is not None else {"id": GWS_MESSAGE_ID}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        self.calls.append(args)
        return self.result


class _FakeSMTP:
    """Stands in for ``smtplib.SMTP``. Records every step, sends nothing."""

    def __init__(self, *, login_error: BaseException | None = None) -> None:
        self.started_tls = False
        self.logins: list[tuple[str, str]] = []
        self.messages: list[Any] = []
        self.quit_calls = 0
        self._login_error = login_error
        self.refused: dict[str, Any] = {}

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        if self._login_error is not None:
            raise self._login_error
        self.logins.append((user, password))

    def send_message(self, message: Any) -> dict[str, Any]:
        self.messages.append(message)
        return self.refused

    def quit(self) -> None:
        self.quit_calls += 1


class _SMTPFactory:
    """Counts INSTANTIATIONS: 'SMTP was never opened' is the assertion."""

    def __init__(self, client: _FakeSMTP | None = None) -> None:
        self.client = client if client is not None else _FakeSMTP()
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int, *, timeout: float = 30.0) -> _FakeSMTP:
        self.calls.append((host, port))
        return self.client


def _channel(
    *,
    gws: _FakeGws | None = None,
    gws_present: bool = True,
    smtp: _SMTPFactory | None = None,
) -> EmailChannel:
    channel = EmailChannel()
    channel.gws_send = gws if gws is not None else _FakeGws()
    channel.gws_probe = lambda: gws_present
    channel.smtp_factory = smtp if smtp is not None else _SMTPFactory()
    return channel


def _config(**kwargs: object) -> AgentConfig:
    defaults: dict[str, object] = {
        "id": "alice",
        "name": "Alice",
        "delivery_mode": DeliveryMode.ANNOUNCE,
        "delivery_to": WILLING,
        "delivery_channel": "email",
    }
    defaults.update(kwargs)
    return AgentConfig(**defaults)  # type: ignore[arg-type]


def _run() -> AgentRun:
    run = AgentRun(agent_id="alice", status=RunStatus.COMPLETED)
    run.tenant_id = TENANT
    return run


def _opt_out(*flagged: str):
    """A stand-in for ``crm.dal.do_not_contact_emails`` over ``flagged``."""
    listed = {a.lower() for a in flagged}

    def _lookup(emails, tenant_id: str = "default") -> set[str]:
        return {e.strip().lower() for e in emails if e and e.strip().lower() in listed}

    return _lookup


def _person(person_id: str = PERSON_ID, email: str = WILLING, *, flagged: bool = False):
    def _get(pid: str, tenant_id: str = "default", **_kw: Any) -> dict[str, Any] | None:
        if pid != person_id:
            return None
        return {
            "id": person_id,
            "emails": {"primaryEmail": email},
            "doNotContact": flagged,
        }

    return _get


@pytest.fixture(autouse=True)
def _configured(monkeypatch, tmp_path):
    """An instance with SMTP configured, and none of this box's own settings.

    The workspace and HOME are pinned to a scratch directory and every
    ``ROBOTHOR_``/``GENUS_`` name is dropped first: ``verify`` and ``health``
    read real settings, and any instance that actually configured email would
    otherwise decide the result of a test about an unconfigured one.
    """
    from robothor.settings import reset_settings
    from robothor.settings.aliases import DEPRECATED_ALIASES

    for name in list(os.environ):
        if name.startswith(("ROBOTHOR_", "GENUS_")) or name in DEPRECATED_ALIASES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ROBOTHOR_EMAIL_FROM", SENDER)
    monkeypatch.setenv("ROBOTHOR_EMAIL_SMTP_HOST", SMTP_HOST)
    monkeypatch.setenv("ROBOTHOR_EMAIL_SMTP_USER", SMTP_USER)
    monkeypatch.setenv("ROBOTHOR_EMAIL_SMTP_PASSWORD", FAKE_SMTP_PASSWORD)
    monkeypatch.delenv("ROBOTHOR_DNC_MODE", raising=False)
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def no_smtp(monkeypatch):
    """An instance with no SMTP configuration at all."""
    from robothor.settings import reset_settings

    for name in (
        "ROBOTHOR_EMAIL_FROM",
        "ROBOTHOR_EMAIL_SMTP_HOST",
        "ROBOTHOR_EMAIL_SMTP_USER",
        "ROBOTHOR_EMAIL_SMTP_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_settings()


class TestTheOptOutIsHonouredBeforeAnythingIsSent:
    @pytest.mark.asyncio
    async def test_a_person_flagged_do_not_contact_is_refused_by_id(self):
        gws = _FakeGws()
        smtp = _SMTPFactory()
        channel = _channel(gws=gws, smtp=smtp)
        with (
            patch("robothor.crm.dal.get_person", side_effect=_person(FLAGGED_PERSON_ID, FLAGGED)),
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out(FLAGGED)),
            patch("robothor.engine.tracking.log_guardrail_event"),
        ):
            receipt = await channel.send(
                FLAGGED_PERSON_ID, "the morning briefing", config=_config(), run=_run()
            )

        assert receipt.status == "failed:email_dnc"
        assert receipt.acknowledged == 0
        assert gws.calls == [], "a flagged recipient reached the gws transport"
        assert smtp.calls == [], "a flagged recipient opened an SMTP connection"

    @pytest.mark.asyncio
    async def test_a_person_flagged_do_not_contact_is_refused_by_address(self):
        gws = _FakeGws()
        smtp = _SMTPFactory()
        channel = _channel(gws=gws, smtp=smtp)
        with (
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out(FLAGGED)),
            patch("robothor.engine.tracking.log_guardrail_event"),
        ):
            receipt = await channel.send(FLAGGED, "hello", config=_config(), run=_run())

        assert receipt.status == "failed:email_dnc"
        assert (gws.calls, smtp.calls) == ([], [])

    @pytest.mark.asyncio
    async def test_the_refusal_is_recorded_as_a_guardrail_event(self):
        run = _run()
        with (
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out(FLAGGED)),
            patch("robothor.engine.tracking.log_guardrail_event") as audit,
        ):
            await _channel().send(FLAGGED, "hello", config=_config(), run=run)

        audit.assert_called_once()
        assert audit.call_args.args[1] == "do_not_contact"
        assert audit.call_args.args[2] == "blocked"
        assert audit.call_args.kwargs["tool_name"] == "channel:email"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("by_id", [False, True])
    async def test_the_dnc_check_is_case_insensitive(self, by_id: bool):
        """The CRM row is lower-cased; the manifest is whatever was typed."""
        gws = _FakeGws()
        channel = _channel(gws=gws)
        target = FLAGGED_PERSON_ID if by_id else "BOB@Example.com"
        with (
            patch(
                "robothor.crm.dal.get_person",
                side_effect=_person(FLAGGED_PERSON_ID, "BOB@Example.com"),
            ),
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out(FLAGGED)),
            patch("robothor.engine.tracking.log_guardrail_event"),
        ):
            receipt = await channel.send(target, "hello", config=_config(), run=_run())

        assert receipt.status == "failed:email_dnc"
        assert gws.calls == []

    @pytest.mark.asyncio
    async def test_an_unreadable_opt_out_list_refuses_without_claiming_an_opt_out(self):
        """`failed:email_dnc` is a claim about a person. This is not that."""
        gws = _FakeGws()
        channel = _channel(gws=gws)
        with patch(
            "robothor.crm.dal.do_not_contact_emails", side_effect=RuntimeError("no database")
        ):
            receipt = await channel.send(WILLING, "hello", config=_config(), run=_run())

        assert receipt.status == "failed:email_dnc_unreadable"
        assert gws.calls == []

    @pytest.mark.asyncio
    async def test_a_recipient_the_crm_never_heard_of_is_allowed(self):
        """An opt-out list, not an allow-list."""
        gws = _FakeGws()
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out(FLAGGED)):
            receipt = await _channel(gws=gws).send(
                "stranger@example.com", "hello", config=_config(), run=_run()
            )

        assert receipt.acknowledged == 1
        assert len(gws.calls) == 1

    @pytest.mark.asyncio
    async def test_observe_mode_notes_the_refusal_and_lets_the_mail_go(self, monkeypatch):
        """One control, one lever — the same one ``gws`` honours."""
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_DNC_MODE", "observe")
        reset_settings()
        gws = _FakeGws()
        with (
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out(FLAGGED)),
            patch("robothor.engine.tracking.log_guardrail_event") as audit,
        ):
            receipt = await _channel(gws=gws).send(FLAGGED, "hello", config=_config(), run=_run())

        assert receipt.acknowledged == 1
        assert len(gws.calls) == 1
        assert audit.call_args.args[2] == "observed"


class TestATargetIsResolvedBeforeItIsTrusted:
    @pytest.mark.asyncio
    async def test_a_malformed_address_never_reaches_the_transport(self):
        gws = _FakeGws()
        receipt = await _channel(gws=gws).send(
            "not-an-address", "hello", config=_config(), run=_run()
        )

        assert receipt.status == "failed:email_unresolved_target"
        assert gws.calls == []

    @pytest.mark.asyncio
    async def test_an_unexpanded_variable_is_its_own_refusal(self):
        receipt = await _channel().send("${EMAIL}", "hello", config=_config(), run=_run())

        assert receipt.status == "failed:email_unexpanded_target"

    @pytest.mark.asyncio
    async def test_an_empty_target_is_refused(self):
        receipt = await _channel().send("  ", "hello", config=_config(), run=_run())

        assert receipt.status == "failed:email_no_target"

    @pytest.mark.asyncio
    async def test_a_person_with_no_email_is_unresolved(self):
        gws = _FakeGws()
        with patch("robothor.crm.dal.get_person", side_effect=_person(PERSON_ID, "")):
            receipt = await _channel(gws=gws).send(PERSON_ID, "hello", config=_config(), run=_run())

        assert receipt.status == "failed:email_unresolved_target"
        assert gws.calls == []

    @pytest.mark.asyncio
    async def test_a_person_id_nothing_matches_is_unresolved(self):
        with patch("robothor.crm.dal.get_person", return_value=None):
            receipt = await _channel().send(PERSON_ID, "hello", config=_config(), run=_run())

        assert receipt.status == "failed:email_unresolved_target"

    @pytest.mark.asyncio
    async def test_a_send_with_no_run_has_no_tenant_and_refuses(self):
        """The opt-out list is per-tenant. A guard may not guess whose it reads."""
        gws = _FakeGws()
        receipt = await _channel(gws=gws).send(WILLING, "hello", config=_config(), run=None)

        assert receipt.status == "failed:email_no_run"
        assert gws.calls == []


class TestWhatTheChannelCanProve:
    @pytest.mark.asyncio
    async def test_gws_success_reports_the_provider_message_id(self):
        gws = _FakeGws({"id": GWS_MESSAGE_ID, "threadId": "t1"})
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws=gws).send(
                WILLING, "the morning briefing", config=_config(), run=_run()
            )

        assert (receipt.acknowledged, receipt.expected) == (1, 1)
        assert receipt.complete is True
        assert receipt.platform_ids == [GWS_MESSAGE_ID]
        assert receipt.target == WILLING

    @pytest.mark.asyncio
    async def test_gws_error_is_a_failed_receipt_not_an_exception(self):
        gws = _FakeGws({"error": "gws exited with code 1"})
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws=gws).send(WILLING, "hello", config=_config(), run=_run())

        assert receipt.status == "failed:email_send"
        assert receipt.acknowledged == 0

    @pytest.mark.asyncio
    async def test_gws_accepting_without_an_id_is_not_delivery(self):
        """ "The call did not raise" is the one thing that is never evidence."""
        gws = _FakeGws({"output": "ok"})
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws=gws).send(WILLING, "hello", config=_config(), run=_run())

        assert receipt.acknowledged == 0
        assert receipt.status == "failed:email_send"


class TestSMTPIsAFallbackForABSENCEOnly:
    @pytest.mark.asyncio
    async def test_smtp_is_not_opened_when_gws_is_available(self):
        gws = _FakeGws()
        smtp = _SMTPFactory()
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            await _channel(gws=gws, gws_present=True, smtp=smtp).send(
                WILLING, "hello", config=_config(), run=_run()
            )

        assert len(gws.calls) == 1
        assert smtp.calls == []

    @pytest.mark.asyncio
    async def test_smtp_sends_when_gws_is_absent(self):
        gws = _FakeGws()
        smtp = _SMTPFactory()
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws=gws, gws_present=False, smtp=smtp).send(
                WILLING, "hello", config=_config(), run=_run()
            )

        assert gws.calls == []
        assert smtp.calls == [(SMTP_HOST, 587)]
        assert smtp.client.started_tls is True
        assert smtp.client.logins == [(SMTP_USER, FAKE_SMTP_PASSWORD)]
        assert receipt.acknowledged == 1
        assert receipt.platform_ids and receipt.platform_ids[0].startswith("<")

    @pytest.mark.asyncio
    async def test_a_gws_error_does_not_re_send_the_same_mail_over_smtp(self):
        """The from-address and the audit trail differ between transports. A
        silent swap after an auth failure sends the same mail as someone else."""
        gws = _FakeGws({"error": "invalid_grant"})
        smtp = _SMTPFactory()
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws=gws, gws_present=True, smtp=smtp).send(
                WILLING, "hello", config=_config(), run=_run()
            )

        assert receipt.status == "failed:email_send"
        assert smtp.calls == []

    @pytest.mark.asyncio
    async def test_neither_transport_is_no_transport(self, no_smtp):
        gws = _FakeGws()
        smtp = _SMTPFactory()
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws=gws, gws_present=False, smtp=smtp).send(
                WILLING, "hello", config=_config(), run=_run()
            )

        assert receipt.status == "failed:email_no_transport"
        assert (gws.calls, smtp.calls) == ([], [])

    @pytest.mark.asyncio
    async def test_a_recipient_refused_by_the_server_is_not_delivered(self):
        client = _FakeSMTP()
        client.refused = {WILLING: (550, b"no such user")}
        smtp = _SMTPFactory(client)
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws_present=False, smtp=smtp).send(
                WILLING, "hello", config=_config(), run=_run()
            )

        assert receipt.acknowledged == 0
        assert receipt.status == "failed:email_send"


class TestNoCredentialReachesALogRecord:
    @pytest.mark.asyncio
    async def test_smtp_credentials_never_reach_a_log_record(self, caplog):
        """``smtplib`` puts the server's own reply in the exception, and a
        rejected ``AUTH PLAIN <base64>`` carries the password inside it."""
        import smtplib

        auth_line = f"AUTH PLAIN AGdlbnVzAG{FAKE_SMTP_PASSWORD}"
        error = smtplib.SMTPAuthenticationError(
            535, f"5.7.8 Username and Password not accepted: {auth_line}".encode()
        )
        smtp = _SMTPFactory(_FakeSMTP(login_error=error))
        with (
            caplog.at_level(logging.DEBUG),
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()),
        ):
            receipt = await _channel(gws_present=False, smtp=smtp).send(
                WILLING, "hello", config=_config(), run=_run()
            )

        assert receipt.status == "failed:email_send"
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert FAKE_SMTP_PASSWORD not in logged
        assert "AUTH PLAIN" not in logged
        assert "<redacted>" in logged

    @pytest.mark.asyncio
    async def test_health_reports_the_transport_without_the_password(self, no_smtp, monkeypatch):
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_EMAIL_FROM", SENDER)
        monkeypatch.setenv("ROBOTHOR_EMAIL_SMTP_HOST", SMTP_HOST)
        monkeypatch.setenv("ROBOTHOR_EMAIL_SMTP_PASSWORD", FAKE_SMTP_PASSWORD)
        reset_settings()

        report = await _channel(gws_present=False).health()

        assert report["channel"] == "email"
        assert report["configured"] is True
        assert report["transport"] == "smtp"
        assert FAKE_SMTP_PASSWORD not in str(report)


class TestVerify:
    @pytest.mark.asyncio
    async def test_verify_without_a_to_address_cannot_prove_a_send(self):
        """No fallback address. Verify is aimed by hand, every time."""
        steps = await _channel().verify(None)

        by_step = {step: (ok, detail) for step, ok, detail in steps}
        assert by_step["send"][0] is False
        assert "--to" in by_step["send"][1]

    @pytest.mark.asyncio
    async def test_verify_refuses_a_to_address_on_the_opt_out_list(self):
        gws = _FakeGws()
        channel = _channel(gws=gws)
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out(FLAGGED)):
            steps = await channel.verify(FLAGGED)

        by_step = {step: (ok, detail) for step, ok, detail in steps}
        assert by_step["send"][0] is False
        assert gws.calls == [], "verify mailed an address that opted out"

    @pytest.mark.asyncio
    async def test_verify_sends_to_an_address_nobody_flagged(self):
        gws = _FakeGws()
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out(FLAGGED)):
            steps = await _channel(gws=gws).verify(WILLING)

        by_step = {step: ok for step, ok, _detail in steps}
        assert by_step["transport"] is True
        assert by_step["send"] is True
        assert len(gws.calls) == 1

    @pytest.mark.asyncio
    async def test_an_unconfigured_instance_gets_one_configuration_step(self, no_smtp):
        from robothor.engine.channels.base import UNCONFIGURED_STEP

        steps = await _channel(gws_present=False).verify(WILLING)

        assert len(steps) == 1
        assert steps[0][0] == UNCONFIGURED_STEP
        assert steps[0][1] is False


class TestNothingStepsAroundTheClosedStatusSet:
    """The body and the subject are agent-controlled, and both can raise.

    A lone surrogate is ordinary LLM debris and makes `as_bytes()` raise
    `UnicodeEncodeError`; a newline in a manifest's `name:` makes the header
    setter raise `ValueError`. Either one escaping `send()` breaks the protocol
    contract `delivery.py` states in so many words — and `deliver()` then
    records `failed:email_exception: <raw exception text>`, a `delivery_status`
    outside the set `docs/channels/email.md` enumerates, so no dashboard query
    matches it. The set is only closed if nothing can step around it.
    """

    #: An unpaired UTF-16 high surrogate: encodable as a str, not as UTF-8.
    LONE_SURROGATE = "briefing \ud800 ready"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("gws_present", [True, False], ids=["gws", "smtp"])
    async def test_a_body_that_cannot_be_encoded_is_a_receipt(self, gws_present: bool):
        gws = _FakeGws()
        smtp = _SMTPFactory()
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws=gws, gws_present=gws_present, smtp=smtp).send(
                WILLING, self.LONE_SURROGATE, config=_config(), run=_run()
            )

        assert receipt.status == "failed:email_send"
        assert receipt.acknowledged == 0

    @pytest.mark.asyncio
    async def test_a_newline_in_the_subject_is_a_receipt(self):
        """Header injection's own defence raises; that raise must not escape."""
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel().send(
                WILLING,
                "hello",
                subject="Briefing\nBcc: bob@example.com",
                config=_config(),
                run=_run(),
            )

        assert receipt.status == "failed:email_send"

    @pytest.mark.asyncio
    async def test_a_newline_in_the_agent_name_is_a_receipt(self):
        """The subject falls back to `config.name`, which comes from a manifest."""
        gws = _FakeGws()
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws=gws).send(
                WILLING, "hello", config=_config(name="Alice\nBcc: bob@example.com"), run=_run()
            )

        assert receipt.status == "failed:email_send"
        assert gws.calls == []


class TestTheRowFlagIsItsOwnSignal:
    @pytest.mark.asyncio
    async def test_the_person_row_flag_refuses_even_when_the_list_is_empty(self):
        """The id form is sold on exactly this second signal: the flag follows
        the PERSON, so it holds for someone whose address has changed or who has
        no `contact_identifiers` row. With the DAL returning nothing, deleting
        `and not flagged_on_row` from the guard must fail this test."""
        gws = _FakeGws()
        smtp = _SMTPFactory()
        with (
            patch(
                "robothor.crm.dal.get_person",
                side_effect=_person(FLAGGED_PERSON_ID, FLAGGED, flagged=True),
            ),
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()),
            patch("robothor.engine.tracking.log_guardrail_event"),
        ):
            receipt = await _channel(gws=gws, smtp=smtp).send(
                FLAGGED_PERSON_ID, "hello", config=_config(), run=_run()
            )

        assert receipt.status == "failed:email_dnc"
        assert (gws.calls, smtp.calls) == ([], [])


class TestABenchmarkRunNeverMails:
    @pytest.mark.asyncio
    async def test_a_benchmark_run_is_refused_before_any_transport(self):
        """A sandbox tenant isolates the database, not the outside world. The
        gws tool path refuses mutating mail under `is_benchmark`; the channel is
        the second door out of the same building."""
        gws = _FakeGws()
        smtp = _SMTPFactory()
        run = _run()
        run.is_benchmark = True

        receipt = await _channel(gws=gws, smtp=smtp).send(
            WILLING, "hello", config=_config(), run=run
        )

        assert receipt.status == "failed:email_benchmark"
        assert (gws.calls, smtp.calls) == ([], [])


class TestCleartextCredentialsAreRefusedRatherThanSent:
    @pytest.fixture
    def cleartext(self, monkeypatch):
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_EMAIL_SMTP_STARTTLS", "false")
        reset_settings()

    @pytest.mark.asyncio
    async def test_a_cleartext_login_is_refused_not_attempted(self, cleartext):
        """`ROBOTHOR_EMAIL_SMTP_STARTTLS=false` on a submission port publishes
        the password on the wire. The setting's own description says so; nothing
        enforced it."""
        smtp = _SMTPFactory()
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws_present=False, smtp=smtp).send(
                WILLING, "hello", config=_config(), run=_run()
            )

        assert receipt.status == "failed:email_no_transport"
        assert smtp.calls == [], "a password was about to go out in the clear"
        assert smtp.client.logins == []

    @pytest.mark.asyncio
    async def test_a_relay_with_no_credential_still_sends_in_the_clear(
        self, cleartext, monkeypatch
    ):
        """There is nothing to publish when there is no credential. An internal
        relay that authenticates by sending host is a supported deployment."""
        from robothor.settings import reset_settings

        monkeypatch.delenv("ROBOTHOR_EMAIL_SMTP_USER", raising=False)
        monkeypatch.delenv("ROBOTHOR_EMAIL_SMTP_PASSWORD", raising=False)
        reset_settings()

        smtp = _SMTPFactory()
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws_present=False, smtp=smtp).send(
                WILLING, "hello", config=_config(), run=_run()
            )

        assert receipt.acknowledged == 1
        assert smtp.client.started_tls is False

    @pytest.mark.asyncio
    async def test_implicit_tls_needs_no_starttls(self, cleartext, monkeypatch):
        """Port 465 is already TLS, so STARTTLS=false is correct there."""
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_EMAIL_SMTP_PORT", "465")
        reset_settings()

        smtp = _SMTPFactory()
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel(gws_present=False, smtp=smtp).send(
                WILLING, "hello", config=_config(), run=_run()
            )

        assert receipt.acknowledged == 1
        assert smtp.calls == [(SMTP_HOST, 465)]
        assert smtp.client.logins == [(SMTP_USER, FAKE_SMTP_PASSWORD)]

    @pytest.mark.asyncio
    async def test_verify_names_the_refusal_rather_than_calling_it_unconfigured(self, cleartext):
        """Something IS configured and it is wrong. Reporting "never set up"
        would be exit 2 — an install gate would read it as a pass."""
        from robothor.engine.channels.base import UNCONFIGURED_STEP

        steps = await _channel(gws_present=False).verify(WILLING)

        by_step = {step: (ok, detail) for step, ok, detail in steps}
        assert UNCONFIGURED_STEP not in by_step
        assert by_step["transport"][0] is False
        assert "cleartext" in by_step["transport"][1].lower()


class TestTheGuardrailWriteStaysOffTheEventLoop:
    @pytest.mark.asyncio
    async def test_the_guardrail_write_happens_in_a_worker_thread(self):
        """``log_guardrail_event`` is psycopg2. Every other database call in the
        module goes through ``asyncio.to_thread`` and the gws path files the
        same event from inside a thread; this one blocked the loop."""
        import threading

        writers: list[int] = []

        def _record(*_args: Any, **_kwargs: Any) -> None:
            writers.append(threading.get_ident())

        with (
            patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out(FLAGGED)),
            patch("robothor.engine.tracking.log_guardrail_event", side_effect=_record),
        ):
            await _channel().send(FLAGGED, "hello", config=_config(), run=_run())

        assert writers, "the refusal filed no guardrail event"
        assert writers[0] != threading.get_ident(), (
            "the guardrail write ran on the event loop; a slow database now stalls "
            "every other delivery in the same process"
        )


class TestVerifyReadsTheRightTenantsOptOutList:
    """``DEFAULT_TENANT`` is frozen from ``os.environ`` at import time, and
    ``robothor.constants`` is already imported before ``genus``'s own
    ``load_instance_env()`` runs. On an instance that pins its tenant in
    ``/etc/robothor/robothor.env`` — the documented shape — verify queried the
    ``default`` tenant's (empty) opt-out list, reported the address clear, and
    mailed a person who had opted out.
    """

    @pytest.fixture
    def pinned_tenant(self, monkeypatch):
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", TENANT)
        reset_settings()

    @staticmethod
    def _recording_lookup(seen: list[str]):
        def _lookup(emails, tenant_id: str = "default") -> set[str]:
            seen.append(tenant_id)
            return set()

        return _lookup

    @pytest.mark.asyncio
    async def test_verify_uses_the_tenant_this_instance_is_configured_for(self, pinned_tenant):
        seen: list[str] = []
        with patch(
            "robothor.crm.dal.do_not_contact_emails", side_effect=self._recording_lookup(seen)
        ):
            steps = await _channel().verify(WILLING)

        assert seen == [TENANT], (
            "verify read the import-time DEFAULT_TENANT, so a flagged person in "
            "the real tenant reads as clear"
        )
        detail = {step: d for step, _ok, d in steps}["send"]
        assert TENANT in detail, "a wrong-tenant clear is indistinguishable from a right one"

    @pytest.mark.asyncio
    async def test_an_explicit_tenant_wins(self, pinned_tenant):
        seen: list[str] = []
        with patch(
            "robothor.crm.dal.do_not_contact_emails", side_effect=self._recording_lookup(seen)
        ):
            await _channel().verify(WILLING, tenant="tenant-b")

        assert seen == ["tenant-b"]


class TestTheProtocol:
    @pytest.mark.asyncio
    async def test_ask_raises_not_implemented(self):
        """Email has no inbound half. ``None`` would mean "nobody answered"."""
        with pytest.raises(NotImplementedError):
            await _channel().ask("is this alright?", ("yes", "no"))

    def test_the_channel_satisfies_the_protocol(self):
        from robothor.engine.channels import Channel

        assert isinstance(EmailChannel(), Channel)

    @pytest.mark.asyncio
    async def test_the_body_is_carried_back_on_the_receipt(self):
        with patch("robothor.crm.dal.do_not_contact_emails", side_effect=_opt_out()):
            receipt = await _channel().send(
                WILLING, "the morning briefing", config=_config(), run=_run()
            )

        assert "the morning briefing" in receipt.body
        assert receipt.post_delivery is True
