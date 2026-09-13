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

from robothor.secrets.redaction import PLACEHOLDER, redact

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


class TestItIsSafeOnTheFailurePathItLivesOn:
    """Every caller is already reporting a failure. A redactor that could fail
    there would be the second bug in one line."""

    def test_it_never_raises_on_odd_input(self) -> None:
        for odd in ("%s %d {}", "\\x00binary\\xff", "a" * 100_000, "🔑 xoxb-emoji-adjacent"):
            assert isinstance(redact(odd), str)
