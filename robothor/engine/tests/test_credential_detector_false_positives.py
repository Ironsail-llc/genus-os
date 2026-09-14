"""A credential's NAME, its TYPE, and a placeholder for it are not credentials.

Measured on this instance in the 24 hours to 2026-09-13: the assignment-shaped
detector behind `no_sensitive_data` warned 26 times on `list_tasks`, 11 on
`list_my_tasks`, 9 on `read_file` and several on `search_records` — and the
main agent then told the operator "Credential exposure flagged this run" twice
in one day. Every one of those warnings was on ordinary CRM and repo text:

* a documented PLACEHOLDER in a task objective (`api_key=ghp_...`),
* the LABEL `access-token` sitting next to its schema word in task prose,
* a pydantic settings line, `ACME_API_PASSWORD: SecretStr = Field(...)`,
  whose "value" was the string `Field(...`,
* an env-var NAME in a file listing, reported as `nACME_API_PASSWORD` —
  the `n` is the tail of the `\\n` that `str(payload)` escapes — whose "value"
  was the next line of the listing.

A warning that fires on the word for a thing rather than the thing trains its
reader to ignore it, and this one reached the operator in prose. The corpus
below is those shapes, paraphrased off the instance (example.com addresses,
placeholder ids). Each entry is annotated with the rule that must reject it,
so removing a rule reds a named test rather than "some test somewhere".

The second corpus is the other half of the bargain: loosening a detector is
only correct if what it exists to catch still trips it. Nothing here may be
weakened to make the first corpus pass.
"""

from __future__ import annotations

import pytest

from robothor.engine.guardrails import (
    GuardrailEngine,
    _first_assigned_credential,
    redact_secrets,
)

# ── Fakes, shaped like the real formats so the production patterns match ──
FAKE_GH_TOKEN = "ghp_" + "A1b2C3d4E5" * 3 + "A1b2C3"  # ghp_ + 36 alnum
FAKE_OPENAI_KEY = "sk-proj-" + "A1b2C3d4E5f6G7h8i9J0"
# Assembled rather than written out: a whole one is shaped well enough that
# GitHub's push protection rejects the branch carrying it.
FAKE_SLACK_TOKEN = "xoxb-" + "1234567890" + "-" + "AbCdEfGhIjKlMnOpQrSt"
FAKE_AWS_KEY = "AKIA" + "QRSTUVWX9012ZZZZ"


def _out(text: str) -> dict[str, str]:
    return {"content": text}


class _Step:
    def __init__(self, tool_name: str, tool_output: object) -> None:
        self.tool_name = tool_name
        self.tool_output = tool_output


def _warned(tool: str, output: object) -> bool:
    engine = GuardrailEngine(enabled_policies=["no_sensitive_data"])
    return engine.check_post_execution(tool, output).action == "warned"


# ─────────────────────────── false positives ───────────────────────────
#
# (rule, tool, payload) — `rule` names the rule that must reject the value, so
# the mutation check has a one-to-one map: delete that rule, this row reds.

FALSE_POSITIVES: list[tuple[str, str, object]] = [
    (
        "elided",
        "list_tasks",
        {
            "tasks": [
                {
                    "id": "task-0001",
                    "title": "Rotate the deploy PAT",
                    "objective": (
                        "The runbook documents the placeholder api_key=ghp_......... "
                        "— replace it with the value from the vault, then notify "
                        "ops@example.com."
                    ),
                }
            ]
        },
    ),
    (
        "elided",
        "read_file",
        _out(
            "class Settings(BaseSettings):\n"
            '    ACME_API_PASSWORD: SecretStr = Field(..., env="ACME_API_PASSWORD")\n'
        ),
    ),
    (
        "elided",
        "search_records",
        {
            "results": [
                {"note": 'the masked value we show in the UI is "access_token": "sk-***********"'}
            ]
        },
    ),
    (
        "type_word",
        "list_my_tasks",
        {
            "tasks": [
                {
                    "id": "task-0002",
                    "description": (
                        "Document the bridge's auth field: access-token: opaque-bearer, "
                        "sent on every request."
                    ),
                }
            ]
        },
    ),
    ("type_word", "read_file", _out('api_key: "SecretStr"')),
    (
        "no_entropy",
        "read_file",
        _out("Required variables:\nACME_API_PASSWORD: required\nACME_API_USER: required\n"),
    ),
    ("no_entropy", "read_file", _out("api_key: undefined")),
    ("placeholder_word", "read_file", _out('auth_token = "your-token"')),
    (
        "placeholder_word",
        "list_tasks",
        {"tasks": [{"objective": "set api_key: your-api-key-here"}]},
    ),
    ("name_reference", "read_file", _out("password = DB_PASSWORD_FILE")),
    ("name_reference", "read_file", _out("ACME_API_PASSWORD=\nACME_API_USER=\n")),
    ("name_reference", "read_file", _out("password = settings.db_password")),
    # A call with arguments. The value pattern excludes `)` but not `(` or
    # `=`, so the capture is `Field(repr=False` — a call reference that stops
    # one character short of being recognised as one.
    ("name_reference", "read_file", _out("providerReference: SecretStr = Field(repr=False)")),
    ("name_reference", "read_file", _out("api_secret: str = Field(default=None)")),
]

#: Shapes that already pass. Pinned so a future widening cannot reintroduce
#: them, and so the documented ones (three literal periods, `***`, `<...>`)
#: are covered whether or not they clear the 8-character floor.
ALREADY_CLEAN: list[object] = [
    {"tasks": [{"objective": 'the runbook shows "access_token": "ghp_..." as the placeholder'}]},
    _out('password = "***"'),
    _out('password = "..."'),
    _out('password = "…"'),
    _out('password = "<redacted>"'),
    _out('password = "<...>"'),
    _out('password = "${DB_PASSWORD}"'),
    _out('password = "{{ vault_pw }}"'),
    _out('"access-token": {"type": "string", "format": "opaque-bearer"}'),
]


class TestTheObservedFalsePositivesDoNotWarn:
    @pytest.mark.parametrize(
        ("rule", "tool", "payload"),
        FALSE_POSITIVES,
        ids=[f"{rule}-{tool}-{i}" for i, (rule, tool, _p) in enumerate(FALSE_POSITIVES)],
    )
    def test_no_warning(self, rule: str, tool: str, payload: object) -> None:
        assert not _warned(tool, payload), f"{rule}: warned on ordinary {tool} output"

    @pytest.mark.parametrize(
        ("rule", "tool", "payload"),
        FALSE_POSITIVES,
        ids=[f"{rule}-{i}" for i, (rule, _t, _p) in enumerate(FALSE_POSITIVES)],
    )
    def test_the_detector_underneath_reports_nothing(
        self, rule: str, tool: str, payload: object
    ) -> None:
        """The unit, so a fix higher up cannot fake the row above."""
        assert _first_assigned_credential(str(payload)) is None, rule

    @pytest.mark.parametrize("payload", ALREADY_CLEAN)
    def test_documented_placeholders_stay_clean(self, payload: object) -> None:
        assert not _warned("read_file", payload)

    def test_a_publish_is_not_blocked_by_a_placeholder(self) -> None:
        """`no_secret_publication` shares the detector, so a false positive
        there is a hard block, not a warning. That is the 14-blocks-in-48h
        failure mode, one shape further on."""
        engine = GuardrailEngine(enabled_policies=["no_secret_publication"])
        for _rule, tool, payload in FALSE_POSITIVES:
            r = engine.check_pre_execution(
                "exec",
                {"command": "git commit -am 'update docs'"},
                agent_id="a",
                prior_steps=[_Step(tool, payload)],
            )
            assert r.allowed is True, r.reason


class TestTheEscapedNewlineIsNotPartOfTheName:
    r"""`str(payload)` escapes newlines, so a name at the start of a line is
    preceded by the literal characters `\` and `n` — and the `n` is a word
    character, so it joins the identifier. The operator was shown
    `nACME_API_PASSWORD`, a variable that does not exist in any file."""

    def test_the_reported_name_has_no_escape_prefix(self) -> None:
        text = str(_out("# settings\nACME_API_PASSWORD = Xy9-staging-secret-42\n"))
        assert _first_assigned_credential(text) == "ACME_API_PASSWORD"


# ─────────────────────────── true positives ───────────────────────────

#: (label, payload, the value that must not survive). The third element is
#: what makes the redaction assertion below generic: every row states its own
#: secret rather than the test carrying a hand-maintained list that drifts
#: away from the corpus it is supposed to cover.
TRUE_POSITIVES: list[tuple[str, object, str]] = [
    ("github token in a file", _out(f"GITHUB_TOKEN={FAKE_GH_TOKEN}\n"), FAKE_GH_TOKEN),
    ("openai key assigned", _out(f'api_key = "{FAKE_OPENAI_KEY}"'), FAKE_OPENAI_KEY),
    ("slack bot token", _out(f"SLACK_BOT_TOKEN={FAKE_SLACK_TOKEN}\n"), FAKE_SLACK_TOKEN),
    ("aws access key", _out(f"aws_secret_access_key = {FAKE_AWS_KEY}\n"), FAKE_AWS_KEY),
    ("mixed-class password", _out("password=Tr0ub4dor3-staging-99x"), "Tr0ub4dor3-staging-99x"),
    (
        "mixed-class token",
        _out("access_token=9f8Ac21bD40eF7g8H1jK3mN5pQ7rS9tU"),
        "9f8Ac21bD40eF7g8H1jK3mN5pQ7rS9tU",
    ),
    ("json credential", _out('{"db_password": "s3cr3t-staging-99"}'), "s3cr3t-staging-99"),
    ("typed assignment", _out('password: "SecretStr" = "hunter2hunter2ab"'), "hunter2hunter2ab"),
    ("short but mixed", _out('client_password = "s3cr3tpw9"'), "s3cr3tpw9"),
    # One character class, but far too long to be a schema word or a label:
    # the entropy rule is bounded at 12 characters for exactly this reason.
    (
        "long passphrase",
        _out("password=correcthorsebatterystaple"),
        "correcthorsebatterystaple",
    ),
    (
        "private key",
        _out("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n"),
        "-----BEGIN RSA PRIVATE KEY-----",
    ),
    # ── The shapes the rejection rules sit next to. ──
    #
    # Every unquoted row above carries a mixed-class value that no rule even
    # looks at, so the rules could be widened a long way and the corpus would
    # stay green — the suite would be certifying the accepted losses by
    # omission, which is this project's own recurring failure (a control with
    # tests asserting its inert state was correct). These rows put a real
    # secret INSIDE each rule's reach, so widening one reds a named test.
    #
    # Unquoted and all-caps — what `_ENV_VAR_NAME` must NOT swallow. Real
    # env-var names put their digits at the end of a word (`..._KEY_2`); key
    # material interleaves them.
    ("uppercase key material", _out("password=X9K2M_4TQ7P_ZR31_WD8V"), "X9K2M_4TQ7P_ZR31_WD8V"),
    ("grouped hex key", _out("api_key: A7F3_B2C1_D0E5_F4A8"), "A7F3_B2C1_D0E5_F4A8"),
    ("base32 seed", _out("secret=JBSWY3DPEHPK3PXP_MZXW6YTB"), "JBSWY3DPEHPK3PXP_MZXW6YTB"),
    (
        "long uppercase secret",
        _out("password=ABCDEFGHIJ_KLMNOPQRST_UVWXYZ0123"),
        "ABCDEFGHIJ_KLMNOPQRST_UVWXYZ0123",
    ),
    # Unquoted and dotted — what `_ATTRIBUTE_PATH` must NOT swallow. An
    # attribute path is lowercase snake_case; a JWT and a random secret that
    # happens to contain a dot are not.
    (
        "jwt-shaped token",
        _out("auth_token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI5OSJ9.k3Qm7Zt4Rv8Nw2Lp"),
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI5OSJ9.k3Qm7Zt4Rv8Nw2Lp",
    ),
    ("a secret containing a dot", _out("password: Hj3k92mQx7.Zr41Tn8Pv"), "Hj3k92mQx7.Zr41Tn8Pv"),
    # Doubled punctuation inside a value — what `_ELIDED_VALUE` must NOT read
    # as "the author elided the value". `*` is a standard generated-password
    # symbol; an elision marker is three or more.
    ("asterisks inside a password", _out("password=P4ss**w0rd**X9K2"), "P4ss**w0rd**X9K2"),
    ("two dots inside a key", _out("password=a9f3..b2c1d0e5f4a8b7"), "a9f3..b2c1d0e5f4a8b7"),
    (
        "two dots inside a token",
        _out("api_key: 9q2m..7zt4rv8nw3xk1bd"),
        "9q2m..7zt4rv8nw3xk1bd",
    ),
    # A bracket inside a password — what `_CALL_REFERENCE` must NOT swallow.
    # A real call's arguments contain a separator (`=`, `,`, `.`, a quote);
    # key material that happens to contain `(` does not. The captured value
    # stops at `)`, which the value charset excludes, so the row states the
    # captured span as the secret.
    ("a bracket inside a password", _out("password=Passw0rd(2024)Xk9"), "Passw0rd(2024"),
    (
        "a bracket inside a secret",
        _out("password=secret(9q2mLp7Zt4Rv8"),
        "secret(9q2mLp7Zt4Rv8",
    ),
    ("a bracket inside a token", _out("api_key: tok9q2m(Lp7Zt4Rv8Nw3"), "tok9q2m(Lp7Zt4Rv8Nw3"),
    # A long all-caps run — what `_ENV_VAR_NAME` must NOT swallow. The digit
    # placement rule is right, but no real env-var name has a 33-character
    # word in it, so each segment stays bounded as well.
    (
        "a long all-caps run",
        _out("password=ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEF_GH"),
        "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEF_GH",
    ),
    (
        "a long all-caps key",
        _out("api_key: QWERTYUIOPASDFGHJKLZXCVBNMQWERT_YUIOP"),
        "QWERTYUIOPASDFGHJKLZXCVBNMQWERT_YUIOP",
    ),
    (
        "a long all-caps run with digits",
        _out("password=ABCDEFGHIJKLMNOPQRSTUVWXYZ12_ABCDEFGHIJ34"),
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ12_ABCDEFGHIJ34",
    ),
    # The same false-positive shapes with a REAL value in them: a placeholder
    # earlier in the text must not shield what comes after it.
    (
        "a real key below a placeholder",
        _out(f'api_key = "your-token"\nservice_api_key = "{FAKE_OPENAI_KEY}"\n'),
        FAKE_OPENAI_KEY,
    ),
    (
        "a real key in a settings class",
        _out(
            "class Settings(BaseSettings):\n"
            "    ACME_API_PASSWORD: SecretStr = Field(...)\n"
            f'    fallback_api_key: str = "{FAKE_OPENAI_KEY}"\n'
        ),
        FAKE_OPENAI_KEY,
    ),
]

_TP_IDS = [label for label, _p, _s in TRUE_POSITIVES]


class TestRealCredentialsStillWarn:
    @pytest.mark.parametrize(("label", "payload", "secret"), TRUE_POSITIVES, ids=_TP_IDS)
    def test_the_warning_still_fires(self, label: str, payload: object, secret: str) -> None:
        assert _warned("read_file", payload), label

    @pytest.mark.parametrize(("label", "payload", "secret"), TRUE_POSITIVES, ids=_TP_IDS)
    def test_publication_is_still_blocked(self, label: str, payload: object, secret: str) -> None:
        engine = GuardrailEngine(enabled_policies=["no_secret_publication"])
        r = engine.check_pre_execution(
            "exec",
            {"command": "git push origin main"},
            agent_id="a",
            prior_steps=[_Step("read_file", payload)],
        )
        assert r.allowed is False, label

    @pytest.mark.parametrize(("label", "payload", "secret"), TRUE_POSITIVES, ids=_TP_IDS)
    def test_the_value_never_reaches_the_transcript(
        self, label: str, payload: object, secret: str
    ) -> None:
        """A guardrail that quotes the secret is the leak it exists to stop."""
        engine = GuardrailEngine(enabled_policies=["no_sensitive_data"])
        reason = engine.check_post_execution("read_file", payload).reason or ""
        assert secret not in reason, label


# ─────────────────────── the redactor agrees ───────────────────────


class TestTheRedactorLeavesNonCredentialsAlone:
    """Whatever the detector ignores, the redactor must not mangle.

    They share one predicate on purpose: two scanners with two opinions is a
    silent disagreement about what counts, and the observable version of that
    was `password: "SecretStr" = "<secret>"` losing the word `SecretStr` while
    keeping the credential.
    """

    @pytest.mark.parametrize(
        ("rule", "tool", "payload"),
        FALSE_POSITIVES,
        ids=[f"{rule}-{i}" for i, (rule, _t, _p) in enumerate(FALSE_POSITIVES)],
    )
    def test_a_false_positive_survives_redaction_unchanged(
        self, rule: str, tool: str, payload: object
    ) -> None:
        assert redact_secrets(payload) == payload, rule

    @pytest.mark.parametrize("payload", ALREADY_CLEAN)
    def test_a_documented_placeholder_survives_redaction_unchanged(self, payload: object) -> None:
        assert redact_secrets(payload) == payload

    def test_the_placeholder_text_itself_is_still_readable(self) -> None:
        """An agent asked to replace `ghp_...` has to be able to see it."""
        text = "the runbook documents api_key=ghp_......... as the placeholder"
        assert redact_secrets(text) == text

    def test_redaction_never_produces_broken_source(self) -> None:
        """`providerReference: SecretStr = Field(repr=False)` used to come back
        as `providerReference: SecretStr=[REDACTED: credential])` — an agent
        asked to read that module was handed syntactically broken Python, for
        a line that holds no credential at all."""
        for line in (
            "providerReference: SecretStr = Field(repr=False)",
            "api_secret: str = Field(default=None)",
            "password: str = Field(default_factory=dict)",
        ):
            assert redact_secrets(line) == line, line

    @pytest.mark.parametrize(("label", "payload", "secret"), TRUE_POSITIVES, ids=_TP_IDS)
    def test_a_real_credential_is_still_redacted(
        self, label: str, payload: object, secret: str
    ) -> None:
        out = str(redact_secrets(payload))
        assert secret not in out, f"{label}: the value survived redaction"


class TestUrlValuedAssignments:
    """A query parameter whose value is a URL is not a credential.

    Live shape (10 warnings/day on ``gws_gmail_get``): Gmail's own
    ``apikey_highlighted_url=https%3A%2F%2F...`` link parameter. The name
    contains "apikey" and the value has plenty of entropy — but the value is
    an address, and an address is not key material. A token *inside* a URL
    query (``?access-token=<random>``) still warns.
    """

    def test_a_url_value_is_not_a_credential(self) -> None:
        from robothor.engine.guardrails import _first_assigned_credential

        assert (
            _first_assigned_credential(
                'href="https://mail.example.com/?apikey_highlighted_url='
                'https%3A%2F%2Fexample.com%2Fdocs%2Fapi-keys&view=pt"'
            )
            is None
        )
        assert _first_assigned_credential("apikey_highlighted_url=https://example.com/x") is None

    def test_a_token_inside_a_url_query_still_warns(self) -> None:
        from robothor.engine.guardrails import _first_assigned_credential

        assert (
            _first_assigned_credential("https://api.example.com/v1?access-token=Qx9v2LmT7pRz4Kd1wq")
            == "access-token"
        )
