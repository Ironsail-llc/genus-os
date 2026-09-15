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


class TestTheJournalLine:
    """The shapes a LOG LINE carries, which is the third place the platform
    does not build the string.

    ``GET /api/logs`` serves journald to the Helm, and a journal line is
    whatever a process wrote — including the environment it was started with.
    ``OPENROUTER_API_KEY=sk-or-…`` in a traceback or a startup banner reaches
    the browser of anyone who can open that page, which is a wider audience
    than the box's shell.
    """

    #: Visibly fake. The prefix is real (OpenAI, OpenRouter and Anthropic all
    #: issue ``sk-``-prefixed keys); the body is not a key.
    API_KEY = "sk-or-notarealkey-000000000000"

    def test_a_bare_api_key_goes(self) -> None:
        cleaned = redact(f"calling openrouter with {self.API_KEY} failed")
        assert self.API_KEY not in cleaned
        assert PLACEHOLDER in cleaned

    def test_an_environment_assignment_loses_its_value(self) -> None:
        cleaned = redact(f"env: OPENROUTER_API_KEY={self.API_KEY}")
        assert self.API_KEY not in cleaned
        assert "OPENROUTER_API_KEY" in cleaned, "the NAME is what the operator needs to see"

    @pytest.mark.parametrize(
        "assignment",
        [
            "SMTP_PASSWORD=hunter2",
            "--smtp-password=hunter2",
            'GENUS_AUTH_SIGNING_KEY="hunter2"',
            "webhook_secret=hunter2",
            "SLACK_BOT_TOKEN=hunter2",
        ],
    )
    def test_a_credential_shaped_name_takes_its_value_with_it(self, assignment: str) -> None:
        """No shape of its own — an SMTP password is whatever the provider
        issued — so the NAME is the only thing there is to match on."""
        assert "hunter2" not in redact(assignment)

    @pytest.mark.parametrize(
        "innocent",
        [
            "monkey=business",
            "ROBOTHOR_LOG_DIR=/srv/app/logs",
            "ROBOTHOR_SLACK_BOT_TOKEN is set nowhere this instance reads",
            "the API key was rejected",
            "task-management-service started",
            "risk-weighted-average-of-the-quarter",
            "MAX_TOKENS=4096",
            "ROBOTHOR_KEY_POOL_SIZE=3",
        ],
    )
    def test_ordinary_log_lines_are_untouched(self, innocent: str) -> None:
        assert redact(innocent) == innocent

    @pytest.mark.parametrize(
        "innocent",
        [
            "sort_key=created_at",
            "Cache-Key=home-page-v2",
            "idempotency-key=abc123",
            "primary_key=id, foreign_key=user_id",
            "key=value pairs are fine",
            "public_key=ssh-rsa AAAAB3Nza",
            "partition_key=tenant",
            "row_key=42",
        ],
    )
    def test_an_ambiguous_key_is_not_a_credential(self, innocent: str) -> None:
        """``KEY`` and ``TOKEN`` are ordinary words; ``API_KEY`` is not.

        ``/api/logs`` redacts every line of every unit, third-party libraries
        included, and this repo does not control their ``key=`` idiom. The
        worst of the set is ``public_key=``: a public key is not a secret, and
        a rule that ate only its first token would leave half of it visible —
        redaction that neither protects nor informs.
        """
        assert redact(innocent) == innocent

    def test_an_assignment_inside_json_leaves_the_json_parseable(self) -> None:
        """The audit CSV's ``details`` cell is compact JSON by contract.

        A value class that ran to the next space would swallow the closing
        quote and brace and truncate the record — an export that mangles its
        own details is an integrity problem, not a cosmetic one.
        """
        import json as _json

        blob = _json.dumps({"note": f"retry with OPENROUTER_API_KEY={self.API_KEY}", "n": 1})
        cleaned = redact(blob)

        assert self.API_KEY not in cleaned
        assert _json.loads(cleaned)["n"] == 1, cleaned

    @pytest.mark.parametrize("closer", ['"', "'", "}", "]", ">", ")"])
    def test_the_value_stops_at_a_delimiter(self, closer: str) -> None:
        cleaned = redact(f"API_KEY={self.API_KEY}{closer}tail")
        assert self.API_KEY not in cleaned
        assert cleaned.endswith(f"{closer}tail"), cleaned

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_a_quoted_value_keeps_its_quotes(self, quote: str) -> None:
        """A shell or env-file line stays a shell or env-file line.

        Replacing ``KEY="x"`` with ``KEY=<redacted>`` would leave the operator
        unable to tell a quoted setting from an unquoted one — and in a
        ``.env`` excerpt that difference is the bug they are reading the log to
        find.
        """
        cleaned = redact(f"export API_KEY={quote}{self.API_KEY}{quote} # rotated")

        assert self.API_KEY not in cleaned
        assert cleaned == f"export API_KEY={quote}{PLACEHOLDER}{quote} # rotated"

    def test_a_long_hyphenated_run_is_linear_not_quadratic(self) -> None:
        """The name's prefix group is bounded so it cannot backtrack.

        ``/api/logs`` runs this over up to 1000 journal lines per request. An
        unbounded ``(?:segment[-_])*`` took 0.54s on this input, which is half a
        second per line on a request path.
        """
        import time

        pathological = "ab-" * 3000 + "="
        start = time.perf_counter()
        redact(pathological)
        assert time.perf_counter() - start < 0.05


class TestItIsSafeOnTheFailurePathItLivesOn:
    """Every caller is already reporting a failure. A redactor that could fail
    there would be the second bug in one line."""

    def test_it_never_raises_on_odd_input(self) -> None:
        for odd in ("%s %d {}", "\\x00binary\\xff", "a" * 100_000, "🔑 xoxb-emoji-adjacent"):
            assert isinstance(redact(odd), str)
            assert isinstance(redact_unrecognized_arguments(odd), str)
