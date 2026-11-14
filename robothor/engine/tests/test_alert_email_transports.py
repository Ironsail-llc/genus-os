"""The email fallback for alerts: the doctor's verdict and the send agree.

The stock image ships the Google Workspace CLI. The email channel used to treat
"the binary exists" as "there is a transport", so on an instance whose CLI was
never signed in, ``genus doctor`` reported the email fallback as able to send
while every real send failed for lack of credentials -- and a configured SMTP
server was never tried, because SMTP was used only when the binary was absent.

Three rules, each pinned below:

* gws counts as a transport only when it is installed AND has a credential to
  send with; a binary with no credential is no transport at all.
* An operator alert whose first transport fails is tried once on each further
  transport this instance has (gws, then SMTP), and reports delivered through
  the one that succeeded.
* The doctor and the send path read the same answer, so for every combination
  of binary, credential and SMTP the doctor says "can send" exactly when a send
  through a fake transport layer lands.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import pytest

import robothor.crm.dal as dal
from robothor.engine import alerts
from robothor.engine.delivery import set_telegram_sender

ALERT_TARGET = "agent@example.com"


class _FakeGws:
    """The gws CLI as the channel sees it: a send that lands only when signed in."""

    def __init__(self, *, signed_in: bool) -> None:
        self.signed_in = signed_in
        self.calls = 0

    def __call__(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        self.calls += 1
        if not self.signed_in:
            return {"error": "auth: gws is not signed in (No credentials)"}
        return {"id": "gws-message-1"}


class _FakeSMTP:
    """An SMTP server that accepts everything, counting connections."""

    def __init__(self) -> None:
        self.connections = 0
        self.messages: list[Any] = []

    def __call__(self, host: str, port: int, *, timeout: float = 30.0) -> _FakeSMTP:
        self.connections += 1
        return self

    def starttls(self) -> None:
        return

    def login(self, user: str, password: str) -> None:
        return

    def send_message(self, message: Any) -> dict[str, Any]:
        self.messages.append(message)
        return {}

    def quit(self) -> None:
        return


@pytest.fixture
def instance(monkeypatch, tmp_path):
    """A clean instance: no Telegram, an email alert fallback, no gws credential.

    Returns a function that sets the three switches and gives back the real
    EmailChannel (registered as ``email``) over fake transports.
    """
    import os

    from robothor.engine.channels import email as email_module
    from robothor.engine.channels import registry
    from robothor.engine.tools.handlers import gws
    from robothor.settings import reset_settings

    for name in list(os.environ):
        if name.startswith(("ROBOTHOR_", "GENUS_", "GOOGLE_WORKSPACE_CLI_", "TELEGRAM_")):
            monkeypatch.delenv(name, raising=False)
    # The credential directory gws reads, empty: this box's own sign-in must
    # not decide a test.
    config_dir = tmp_path / "gws-config"
    config_dir.mkdir()
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ROBOTHOR_ALERT_FALLBACK_CHANNELS", f"email:{ALERT_TARGET}")

    monkeypatch.setattr(dal, "send_notification", lambda **kw: "notif-1")
    monkeypatch.setattr(email_module, "_opt_out_lookup", lambda *_a, **_kw: set())
    monkeypatch.setattr(email_module, "_dnc_mode", lambda: "enforce")
    set_telegram_sender(None)  # type: ignore[arg-type]

    def _build(*, binary: bool, signed_in: bool, smtp: bool):
        monkeypatch.setattr(gws, "gws_available", lambda: binary)
        if signed_in:
            (config_dir / "credentials.enc").write_bytes(b"encrypted")
        if smtp:
            monkeypatch.setenv("ROBOTHOR_EMAIL_SMTP_HOST", "smtp.example.com")
            monkeypatch.setenv("ROBOTHOR_EMAIL_FROM", "alerts@example.com")
        reset_settings()
        channel = email_module.EmailChannel()
        fake_gws = _FakeGws(signed_in=signed_in)
        fake_smtp = _FakeSMTP()
        channel.gws_send = fake_gws
        channel.smtp_factory = fake_smtp
        monkeypatch.setattr(
            registry, "get_channel", lambda name: channel if name == "email" else None
        )
        return channel, fake_gws, fake_smtp

    yield _build
    set_telegram_sender(None)  # type: ignore[arg-type]
    reset_settings()


def _doctor_rows() -> list[Any]:
    from robothor.doctor.checks import channels as channel_checks
    from robothor.doctor.tests.conftest import make_ctx

    check = next(item for item in channel_checks.CHECKS if item.id == "alerts.delivery")
    answer = asyncio.run(check.run(make_ctx()))
    return answer if isinstance(answer, list) else [answer]


def _doctor_says_email_can_send() -> bool:
    return any(row.sub_id == "email" and row.status == "pass" for row in _doctor_rows())


COMBINATIONS = list(itertools.product((True, False), repeat=3))


@pytest.mark.parametrize(
    ("binary", "signed_in", "smtp"),
    COMBINATIONS,
    ids=[
        f"gws-{'present' if b else 'absent'}-{'signed-in' if s else 'no-credential'}-"
        f"smtp-{'configured' if m else 'unset'}"
        for b, s, m in COMBINATIONS
    ],
)
def test_the_doctor_says_can_send_exactly_when_the_alert_lands(
    instance, binary, signed_in, smtp
) -> None:
    _channel, fake_gws, fake_smtp = instance(binary=binary, signed_in=signed_in, smtp=smtp)

    verdict = _doctor_says_email_can_send()
    delivered = asyncio.run(alerts.alert("critical", "Disk full", "the volume is at 99%"))

    assert verdict == delivered, (
        f"the doctor said can-send={verdict} but the alert delivered={delivered}"
    )
    # And both tell the truth: a transport that can send exists exactly when the
    # CLI is installed and signed in, or SMTP is configured.
    assert delivered == ((binary and signed_in) or smtp)
    # One attempt per transport, never a loop.
    assert fake_gws.calls <= 1
    assert fake_smtp.connections <= 1


def test_an_unsigned_gws_with_no_smtp_fails_the_doctor_and_names_why(instance) -> None:
    instance(binary=True, signed_in=False, smtp=False)

    rows = _doctor_rows()
    email_row = next(row for row in rows if row.sub_id == "email")
    assert email_row.status == "fail", rows
    assert "signed in" in email_row.detail or "credential" in email_row.detail, email_row.detail


def test_an_alert_falls_through_to_smtp_when_gws_fails(instance) -> None:
    """A credential that exists but is rejected (revoked, expired) is found
    only by sending; the alert then tries SMTP once and lands there."""
    channel, fake_gws, fake_smtp = instance(binary=True, signed_in=True, smtp=True)
    fake_gws.signed_in = False  # the stored credential is refused at send time

    delivered = asyncio.run(alerts.alert("critical", "Disk full", "the volume is at 99%"))

    assert delivered is True
    assert fake_gws.calls == 1
    assert fake_smtp.connections == 1
    assert [m["To"] for m in fake_smtp.messages] == [ALERT_TARGET]


def test_an_ordinary_send_does_not_fall_through_after_a_gws_error(instance) -> None:
    """Outside an alert, a gws error is a receipt, not a reason to send the
    same mail from a different address."""
    from types import SimpleNamespace

    from robothor.engine.channels.email import STATUS_SEND

    channel, fake_gws, fake_smtp = instance(binary=True, signed_in=True, smtp=True)
    fake_gws.signed_in = False
    run = SimpleNamespace(id="", tenant_id="test-tenant", is_benchmark=False)

    receipt = asyncio.run(channel.send("bob@example.com", "hello", run=run))

    assert receipt.status == STATUS_SEND
    assert fake_gws.calls == 1
    assert fake_smtp.connections == 0


def test_an_ordinary_send_uses_smtp_when_gws_has_no_credential(instance) -> None:
    """A CLI with no credential is no transport, for every send."""
    from types import SimpleNamespace

    channel, fake_gws, fake_smtp = instance(binary=True, signed_in=False, smtp=True)
    run = SimpleNamespace(id="", tenant_id="test-tenant", is_benchmark=False)

    receipt = asyncio.run(channel.send("bob@example.com", "hello", run=run))

    assert receipt.complete
    assert fake_gws.calls == 0
    assert fake_smtp.connections == 1


# ── what counts as a gws credential ─────────────────────────────────────────


@pytest.fixture
def credential_env(monkeypatch, tmp_path):
    from robothor.settings import reset_settings

    for name in ("GOOGLE_WORKSPACE_CLI_TOKEN", "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    config_dir = tmp_path / "gws-config"
    config_dir.mkdir()
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", str(config_dir))
    reset_settings()
    yield config_dir
    reset_settings()


def test_no_credential_anywhere_is_not_signed_in(credential_env) -> None:
    from robothor.engine.tools.handlers.gws import gws_signed_in

    assert gws_signed_in() is False


@pytest.mark.parametrize("filename", ["credentials.enc", "credentials.json"])
def test_a_stored_credential_is_signed_in(credential_env, filename) -> None:
    from robothor.engine.tools.handlers.gws import gws_signed_in

    (credential_env / filename).write_text("{}")
    assert gws_signed_in() is True


def test_a_credentials_file_or_token_in_the_environment_is_signed_in(
    credential_env, monkeypatch, tmp_path
) -> None:
    from robothor.engine.tools.handlers.gws import gws_signed_in
    from robothor.settings import reset_settings

    exported = tmp_path / "exported.json"
    exported.write_text("{}")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE", str(exported))
    reset_settings()
    assert gws_signed_in() is True

    monkeypatch.delenv("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_TOKEN", "not-a-real-token")
    reset_settings()
    assert gws_signed_in() is True


def test_the_email_transport_row_does_not_claim_an_unsigned_gws(instance) -> None:
    """``email.transport`` reads the same rule: an installed CLI with no
    credential is not "the email channel sends through it"."""
    from robothor.doctor.checks import channels as channel_checks
    from robothor.doctor.tests.conftest import make_ctx

    instance(binary=True, signed_in=False, smtp=False)
    check = next(item for item in channel_checks.CHECKS if item.id == "email.transport")
    answer = asyncio.run(check.run(make_ctx()))
    row = answer[0] if isinstance(answer, list) else answer
    assert row.status != "pass", row
