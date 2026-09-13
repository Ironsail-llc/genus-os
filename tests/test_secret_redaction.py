"""``robothor.secrets.redaction`` — the last line before a credential is printed.

Two real leaks motivated it, and they share a shape: **the platform did not
build the string**. A value-based redactor cannot help there, because the
process is not holding the value — it arrived from outside.

* ``slack_sdk`` raising ``ValueError: Invalid header value b'Bearer xoxb-…'``
  for a token that reached it with a trailing newline, whose message the Slack
  channel logged verbatim.
* argparse printing ``unrecognized arguments: --bot-tokn xoxb-…`` for a
  one-letter typo, from the very command that refuses tokens on a command line.

So the tests that matter are: does it catch the shapes this platform actually
handles, and does it leave an ordinary error message readable? A redactor that
ate every message would trade one unusable outcome for another.
"""

from __future__ import annotations

import pytest

from robothor.secrets.redaction import PLACEHOLDER, redact, redact_unrecognized_arguments

#: Visibly fake, every one of them. This is platform code.
SLACK_BOT = "xoxb-test-not-a-real-token"
SLACK_APP = "xapp-test-not-a-real-token"
SLACK_USER = "xoxp-test-not-a-real-token"
TELEGRAM = "1234567:AAfake-telegram-token-value-nnnnnnnnnn"


class TestItCatchesTheShapesThisPlatformHandles:
    @pytest.mark.parametrize(
        "token",
        [SLACK_BOT, SLACK_APP, SLACK_USER, TELEGRAM],
        ids=["slack-bot", "slack-app", "slack-user", "telegram-bot"],
    )
    def test_a_token_never_survives(self, token: str) -> None:
        cleaned = redact(f"unrecognized arguments: --bot-tokn {token}")

        assert token not in cleaned
        assert PLACEHOLDER in cleaned

    def test_the_authorization_header_goes_too(self) -> None:
        """How a token reaches an HTTP client's OWN exception text, which is the
        one place the platform never gets to format."""
        cleaned = redact(f"Invalid header value b'Bearer {SLACK_BOT}'")
        assert SLACK_BOT not in cleaned
        assert "Bearer " + SLACK_BOT not in cleaned

    def test_a_token_embedded_mid_sentence_is_found(self) -> None:
        cleaned = redact(f"the vault refused {SLACK_BOT} for channels/slack/bot_token")
        assert SLACK_BOT not in cleaned
        assert "channels/slack/bot_token" in cleaned, "the key name is not a secret"

    def test_several_tokens_in_one_line_all_go(self) -> None:
        cleaned = redact(f"{SLACK_BOT} and {SLACK_APP}")
        assert SLACK_BOT not in cleaned
        assert SLACK_APP not in cleaned

    def test_case_does_not_rescue_a_token(self) -> None:
        assert "XOXB" not in redact("XOXB-TEST-NOT-A-REAL-TOKEN")


class TestItLeavesAnErrorReadable:
    """A false positive here costs an operator their own error message."""

    @pytest.mark.parametrize(
        "innocent",
        [
            "unrecognized arguments: --bot-tokn",
            "error: the following arguments are required: name",
            "partial:2/3",
            "failed:slack_client:transport",
            "ROBOTHOR_SLACK_BOT_TOKEN is set nowhere this instance reads",
            "channels/slack/bot_token",
            "run at 12:30 with a 4000 character body",
            "commit a49761be57fb51f2a5b19d96c64face894b9319b",
            "2026-09-13T05:41:20Z",
        ],
    )
    def test_ordinary_text_is_untouched(self, innocent: str) -> None:
        assert redact(innocent) == innocent

    def test_the_flag_name_survives_so_the_operator_can_see_the_typo(self) -> None:
        cleaned = redact(f"unrecognized arguments: --bot-tokn {SLACK_BOT}")
        assert "--bot-tokn" in cleaned

    def test_an_empty_string_is_returned_unchanged(self) -> None:
        assert redact("") == ""


class TestTheSMTPAuthLine:
    """An SMTP password has no shape of its own; the AUTH line carrying it does.

    ``smtplib`` quotes the server's reply back inside
    ``SMTPAuthenticationError``, and a rejected ``AUTH PLAIN <base64>`` decodes
    straight to the password.
    """

    #: Visibly fake. `AUTH LOGIN` sends the username and then the password as
    #: two SEPARATE base64 lines, which is the half the first cut missed.
    USERNAME_B64 = "dXNlcm5hbWVAZXhhbXBsZS5jb20="
    PASSWORD_B64 = "bm90LWEtcmVhbC1zbXRwLXBhc3N3b3Jk"

    def test_auth_plain_goes(self) -> None:
        assert self.PASSWORD_B64 not in redact(f"AUTH PLAIN {self.PASSWORD_B64}")

    def test_the_auth_login_conversation_goes_line_by_line(self) -> None:
        """``redact("AUTH LOGIN\\n<user>\\n<password>")`` used to return
        ``<redacted>\\n<user>\\n<password>``: the shape matched the first line
        and stopped, leaving the credential on the next one."""
        cleaned = redact(f"AUTH LOGIN\n{self.USERNAME_B64}\n{self.PASSWORD_B64}")

        assert self.PASSWORD_B64 not in cleaned
        assert self.USERNAME_B64 not in cleaned

    def test_a_reply_quoting_the_auth_line_goes(self) -> None:
        reply = (
            f"(535, b'5.7.8 Username and Password not accepted: AUTH PLAIN {self.PASSWORD_B64}')"
        )
        assert self.PASSWORD_B64 not in redact(reply)

    def test_prose_about_authentication_survives(self) -> None:
        """A false positive here costs an operator their own error message."""
        for innocent in (
            "SMTP AUTH is required by this server",
            "the AUTH LOGIN mechanism is not offered",
            "AUTH extension unavailable",
        ):
            assert redact(innocent) == innocent


class TestARejectedFlagsValueIsNotPrinted:
    """argparse prints ``unrecognized arguments: --smtp-passwrd <value>`` for a
    one-letter typo — from the very command that refuses credentials on a
    command line. :func:`redact` catches the SHAPED credentials, and an SMTP
    password has no shape, so the message itself has to be scrubbed.
    """

    #: No shape at all. That is the point: `redact` cannot see it.
    SHAPELESS = "zzTOPSECRETzz-9999"

    def test_the_value_of_an_unrecognized_flag_goes(self) -> None:
        cleaned = redact_unrecognized_arguments(
            f"unrecognized arguments: --smtp-passwrd {self.SHAPELESS}"
        )

        assert self.SHAPELESS not in cleaned
        assert PLACEHOLDER in cleaned

    def test_the_flag_name_survives_so_the_operator_can_see_the_typo(self) -> None:
        cleaned = redact_unrecognized_arguments(
            f"unrecognized arguments: --smtp-passwrd {self.SHAPELESS}"
        )
        assert "--smtp-passwrd" in cleaned

    def test_the_equals_form_goes_too(self) -> None:
        cleaned = redact_unrecognized_arguments(
            f"unrecognized arguments: --smtp-passwrd={self.SHAPELESS}"
        )

        assert self.SHAPELESS not in cleaned
        assert "--smtp-passwrd" in cleaned

    def test_every_other_message_is_left_exactly_alone(self) -> None:
        for innocent in (
            "error: the following arguments are required: name",
            "argument --to: invalid choice: 'vualt'",
            "usage: genus channel add [-h] name",
        ):
            assert redact_unrecognized_arguments(innocent) == innocent


class TestItIsSafeOnTheFailurePathItLivesOn:
    """Every caller is already reporting a failure. A redactor that could fail
    there would be the second bug in one line."""

    def test_it_never_raises_on_odd_input(self) -> None:
        for odd in ("%s %d {}", "\\x00binary\\xff", "a" * 100_000, "🔑 xoxb-emoji-adjacent"):
            assert isinstance(redact(odd), str)
            assert isinstance(redact_unrecognized_arguments(odd), str)
