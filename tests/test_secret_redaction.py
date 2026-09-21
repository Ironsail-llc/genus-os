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

    #: Forty credential names this platform, its CI, or something it talks to
    #: actually issues — the POSITIVE list. Every one is checked with a
    #: SHAPELESS value ("hunter2"), because a value with a shape of its own is
    #: caught by the other patterns and would make this list pass for the wrong
    #: reason.
    #:
    #: The list exists because the first narrowing overshot in the opposite
    #: direction: requiring an "authority qualifier" before KEY/TOKEN made
    #: GITHUB_TOKEN, VAULT_TOKEN, DEPLOY_KEY and a dozen others invisible. A
    #: rule tuned against false positives alone will do that every time, so the
    #: two lists are kept side by side and both are asserted.
    REAL_CREDENTIAL_NAMES = [
        # CI, registries and forges — none of these carry a qualifier.
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "HF_TOKEN",
        "NPM_TOKEN",
        "GITLAB_TOKEN",
        "ARGOCD_TOKEN",
        "NATS_TOKEN",
        "VAULT_TOKEN",
        "TELEGRAM_TOKEN",
        "GHCR_TOKEN",
        "PYPI_TOKEN",
        "CODECOV_TOKEN",
        # Keys whose first word is a noun, not an authority word.
        "DEPLOY_KEY",
        "SSH_KEY",
        "SOPS_AGE_KEY",
        # No separator at all. libpq's own spelling.
        "PGPASSWORD",
        # A rotated slot: the trailing _2 used to defeat the word boundary.
        "OPENROUTER_API_KEY_2",
        "GITHUB_TOKEN_OLD",
        # Provider keys.
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        # This platform's own.
        "AUTH_SECRET",
        "GENUS_BRIDGE_SSO_SECRET",
        "GENUS_AUTH_SIGNING_KEY",
        "ROBOTHOR_TELEGRAM_BOT_TOKEN",
        "ROBOTHOR_DB_PASSWORD",
        "REDIS_PASSWORD",
        # Channels and mail.
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SMTP_PASSWORD",
        "--smtp-password",
        "webhook_secret",
        # OIDC / OAuth.
        "CLIENT_SECRET",
        "OIDC_CLIENT_SECRET",
        "ACCESS_TOKEN",
        "REFRESH_TOKEN",
        # Cloud.
        "AWS_SECRET_ACCESS_KEY",
    ]

    @pytest.mark.parametrize("name", REAL_CREDENTIAL_NAMES)
    def test_a_real_credential_name_takes_its_value_with_it(self, name: str) -> None:
        """No shape of its own — a GitHub PAT or an SMTP password is whatever
        the provider issued — so the NAME is the only thing there is to match
        on, and it has to match the names that are actually in use."""
        cleaned = redact(f"{name}=hunter2")

        assert "hunter2" not in cleaned, name
        assert name.lstrip("-").split("=")[0] in cleaned, "the NAME must survive"

    def test_the_positive_list_is_the_size_it_claims(self) -> None:
        """A list that silently shrank would take its coverage with it."""
        assert len(self.REAL_CREDENTIAL_NAMES) == 40
        assert len(set(self.REAL_CREDENTIAL_NAMES)) == 40

    @pytest.mark.parametrize("name", ["token", "MY_KEY", "x-api-key", "REGISTRY_PASSWORD_2"])
    def test_a_credential_name_needs_no_qualifier(self, name: str) -> None:
        """The shape of the rule, not just its current list.

        A qualifier requirement is what made the round-1 misses, so this pins
        the general case: any ``*_KEY`` that is not an ordinary noun, any
        ``*TOKEN``, any ``*PASSWORD``, with or without a rotation suffix.
        """
        assert "hunter2" not in redact(f"{name}=hunter2")

    @pytest.mark.parametrize(
        "innocent",
        [
            # A name ABOUT a credential is not the credential.
            "ROBOTHOR_TOKEN_PATH=/run/robothor/token",
            "API_KEY_PREFIX=sk-",
            "KEY_COUNT=3",
            "SIGNING_KEY_FILE=/etc/robothor/key.pem",
            "ACCESS_TOKEN_TTL=900",
            # Two more compounds where "key" is the English noun.
            "composite_key=a+b",
            "natural_key=email",
        ],
    )
    def test_a_name_about_a_credential_is_not_one(self, innocent: str) -> None:
        """The rotation suffix (``…_KEY_2``) must not turn ``…_KEY_POOL_SIZE``
        into a key by giving it something to end with."""
        assert redact(innocent) == innocent

    @pytest.mark.parametrize("assignment", ['GENUS_AUTH_SIGNING_KEY="hunter2"'])
    def test_a_quoted_credential_goes_too(self, assignment: str) -> None:
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
            "group_key=tenant_id",
            "shard_key=7",
            "https://example.invalid/v1/token/abc?page=2 returned 404",
        ],
    )
    def test_an_ambiguous_key_is_not_a_credential(self, innocent: str) -> None:
        """``KEY`` is an ordinary word in a dozen ordinary compounds.

        ``/api/logs`` redacts every line of every unit, third-party libraries
        included, and this repo does not control their ``key=`` idiom. The
        worst of the set is ``public_key=``: a public key is not a secret, and
        a rule that ate only its first token would leave half of it visible —
        redaction that neither protects nor informs.

        Every one of these is a ``*_KEY`` name. Round 0 produced NO ``*_TOKEN``
        false positive, which is why ``TOKEN`` needs no qualifier and ``KEY``
        is filtered by a deny-list of the nouns that actually collide.
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


# ── N9: what redaction must NOT eat ──────────────────────────────────────────
#
# Every entry below is a real sentence an agent or an operator writes, which the
# redactor mangled. None of them loses a credential; each loses information the
# agent may need later in the same session — and a history that quietly rewrites
# the operator's own words is its own kind of failure.


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (
            "use Bearer authentication for the API",
            "`Bearer\\s+\\S+` ate the next English word",
        ),
        (
            "the Bearer token goes in the header",
            "same shape, and this is how an agent explains itself",
        ),
        (
            "sk-learn-compatible-estimators",
            "`sk-` is a real prefix and also the start of an ordinary hyphenated word",
        ),
        (
            "SSH_KEY=~/.ssh/id_ed25519",
            "a PATH to a key is not a key; the agent needs the path",
        ),
        (
            "GITHUB_TOKEN=$(gh auth token)",
            "a command substitution is a command, and redacting half of it leaves "
            "syntactically broken text in the history",
        ),
        (
            "OPENAI_API_KEY=${OPENROUTER_API_KEY}",
            "a variable reference is not a value",
        ),
        (
            "TOKEN_PATH=/etc/robothor/token",
            "a path again",
        ),
    ],
)
def test_redaction_leaves_legitimate_text_alone(text, why):
    assert redact(text) == text, f"{why}: {redact(text)!r}"


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer ghp_FAKE0000aaaaaaaaaaaaaaaaaaaaaaaa",
        "Bearer eyJhbGciOiJIUzI1NiJ9.aaaaaaaaaaaaaaa.bbbbbbbbbbbbbbb",
        "Bearer sk-FAKE0000aaaaaaaaaaaaaaaaaaaa",
        "OPENAI_API_KEY=sk-FAKE0000aaaaaaaaaaaaaaaaaaaa",
        "ghp_FAKE0000aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ],
)
def test_the_real_shapes_are_still_taken(text):
    """The tightening must not buy its precision with a miss."""
    assert redact(text) != text, f"a credential survived: {text[:20]}…"


class TestRedactCarriesNoProductProse:
    """``redact`` is the platform-wide primitive, not a chat feature.

    It runs on logs, audit fields, scrubbed page text and exception messages
    on every instance, enrolled in autonomy or not. For one release it also
    ran the autonomy chat backstop, which appended an advisory sentence —
    ``[Payment details were withheld from chat. Enroll securely at
    /account/autonomy.]`` — to arbitrary strings. Two consequences, both bad:

    * Every Luhn-valid 12-19 digit run is a "card". Every IMEI is Luhn-valid
      by construction, and carrier tracking and order numbers hit it too, so
      the redactor destroyed ordinary operational identifiers in logs.
    * The appended sentence is ``[...]``-shaped, which is how this platform
      writes system notes to the model. A merchant page or an inbound message
      only had to contain a Luhn-valid digit run to get a pseudo-system line
      into the model's context — prompt injection through the redactor.

    The backstop belongs at the chat-intake boundary that wants it
    (``robothor.autonomy.intake.protect_payment_text``). ``redact`` returns a
    redaction and nothing else.
    """

    #: Valid IMEI (Luhn check digit 8). Every IMEI is Luhn-valid by design.
    IMEI = "490154203237518"

    #: Luhn-valid, 12 and 15 digits, and deliberately NOT card-shaped: no
    #: network issues an IIN beginning 8 or 9. The point of these cases is
    #: that a long Luhn-valid run is not evidence of a card, so using a real
    #: Visa test PAN to make it — as the first version of this file did —
    #: teaches the next reader the opposite of the lesson.
    CONSIGNMENT = "800123456785"
    ORDER = "900111222333441"

    def test_an_imei_survives_intact(self):
        text = f"IMEI {self.IMEI} registered"
        assert redact(text) == text

    def test_a_tracking_number_survives_intact(self):
        text = f"carrier consignment {self.CONSIGNMENT} delivered"
        assert redact(text) == text

    def test_no_advisory_text_is_appended_to_any_string(self):
        for text in (
            # A real PAN too: even here `redact` must stay a redactor. The
            # chat boundary is where a card is withheld, and it is the only
            # place that may say so.
            "log line: card 4242424242424242 charged ok",
            f"IMEI {self.IMEI} registered",
            f"order {self.ORDER} shipped",
        ):
            out = redact(text)
            assert "[" not in out and "]" not in out, out
            assert "/account/autonomy" not in out, out
            assert "withheld" not in out, out

    def test_the_trailing_separator_is_not_eaten(self):
        """``(?:\\d[ -]?){12,19}`` consumed the space after the last digit.

        ``card 4242424242424242 charged`` came back as ``…withheld]charged``:
        the words either side of a redaction ran together, which is the same
        class of damage as the ``Bearer <english word>`` bug above.
        """
        from robothor.autonomy.intake import protect_payment_text

        out = protect_payment_text("card 4242424242424242 charged ok")
        assert "withheld] charged ok" in out, out

    def test_the_chat_boundary_still_withholds_a_real_pan(self):
        from robothor.autonomy.intake import protect_payment_text

        out = protect_payment_text("Use my card 4242 4242 4242 4242 cvv: 123")
        assert "4242" not in out
        assert "/account/autonomy" in out

    def test_a_secure_marker_payload_is_still_scrubbed_without_prose(self):
        """``/secure`` is a real secret marker, so the payload still goes.

        But it goes as a plain redaction. Truncating a log at an attacker's
        ``/secure`` line and replacing the tail with a ``[...]`` sentence was
        the same injection surface as the payment advisory.
        """
        out = redact('/secure profile\n{"legal_name":"private-person-canary"}')
        assert "private-person-canary" not in out
        assert "[" not in out and "]" not in out, out
