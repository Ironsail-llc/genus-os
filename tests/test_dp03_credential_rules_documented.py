"""DP-03's rule table must describe the detector that actually runs.

Nothing read `docs/compliance/SECURITY_CONTROLS.md` from a test, and the
control it describes had just been narrowed. The result was a sentence — "any
mixed-class literal of eight or more characters bound to a credential
identifier" still fires — that was false for four distinct shapes of uppercase
key material the narrowing had started dropping. A reader deciding whether the
control still covered their `.env` file would have been misled by the document
written to answer exactly that question.

So each example the section names is checked twice: it is in the document, and
the predicate agrees with the claim the document makes about it. Change one
without the other and this reds.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from robothor.engine.guardrails import GuardrailEngine

DOC = Path(__file__).resolve().parents[1] / "docs/compliance/SECURITY_CONTROLS.md"

#: Examples DP-03 lists as NOT counting as a credential, with the assignment
#: each is written into. The template matters: the name-shaped rule applies
#: only to unquoted values, which is itself one of the document's claims.
IGNORED: list[tuple[str, str]] = [
    ("changeme", 'password = "{v}"'),
    ("your-token", 'auth_token = "{v}"'),
    ("${VAR}", 'password = "{v}"'),
    ("{{ var }}", 'password = "{v}"'),
    ("<redacted>", 'password = "{v}"'),
    ("***", 'password = "{v}"'),
    ("ghp_...", "api_key = {v}"),
    ("sk-***********", 'access_token = "{v}"'),
    ("Field(", "api_secret: str = {v}default=None)"),
    ("str", 'api_key: "{v}"'),
    ("SecretStr", 'api_key: "{v}"'),
    ("string", 'api_key: "{v}"'),
    ("bearer", '"access-token": "{v}"'),
    ("opaque-bearer", '"access-token": "{v}"'),
    ("access-token", '"api_key": "{v}"'),
    ("DB_PASSWORD_FILE", "password = {v}"),
    ("settings.db_password", "password = {v}"),
    ("OPENROUTER_API_KEY_2", "api_key = {v}"),
    ("required", "ACME_API_PASSWORD: {v}"),
    ("undefined", "api_key: {v}"),
]

#: Examples DP-03 names as firing anyway — the half of the claim that a reader
#: relies on. Each is a shape the rules above sit next to, not far from.
STILL_FIRES: list[tuple[str, str]] = [
    ("X9K2M_4TQ7P_ZR31_WD8V", "password={v}"),
    ("configuration", "password: {v}"),
]

#: Examples DP-03 lists under Limitations as NOT detected. A future widening
#: that starts catching one of these is welcome — but the document says it is
#: a loss, so the document has to be corrected in the same change.
DOCUMENTED_LOSSES: list[tuple[str, str]] = [
    ("SUPER_SECRET_VALUE_42", "password={v}"),
    ("QWERTY9_ZXCVB8_ASDFG7", "export DB_PASSWORD={v}"),
    ("svc_prod.tok_9q2m.sig_7zt4", "export CLIENT_SECRET={v}"),
]


def _dp03() -> str:
    text = DOC.read_text()
    start = text.index("### DP-03")
    return text[start : text.index("\n### ", start + 1)]


def _warns(text: str) -> bool:
    engine = GuardrailEngine(enabled_policies=["no_sensitive_data"])
    return engine.check_post_execution("read_file", {"content": text}).action == "warned"


class TestTheDocumentNamesTheseExamples:
    @pytest.mark.parametrize(
        "example",
        [e for e, _t in IGNORED + STILL_FIRES + DOCUMENTED_LOSSES],
    )
    def test_the_example_appears_in_dp03(self, example: str) -> None:
        assert f"`{example}" in _dp03(), (
            f"DP-03 no longer names {example!r}; this test pins the document to "
            "the code, so update both or neither"
        )

    def test_the_false_claim_is_gone(self) -> None:
        """The exact sentence C-2 found: it promised coverage the narrowing
        had removed, for the shape most likely to be in a real `.env`."""
        assert "any mixed-class literal of eight or more" not in _dp03()

    def test_the_measured_residual_is_stated(self) -> None:
        """DP-03 must not imply the false positives are finished."""
        assert re.search(r"\b213\b", _dp03()), "DP-03 no longer states the measured residual"


class TestThePredicateAgreesWithTheDocument:
    @pytest.mark.parametrize(("example", "template"), IGNORED, ids=[e for e, _t in IGNORED])
    def test_a_documented_non_credential_does_not_warn(self, example: str, template: str) -> None:
        assert not _warns(template.format(v=example)), example

    @pytest.mark.parametrize(("example", "template"), STILL_FIRES, ids=[e for e, _t in STILL_FIRES])
    def test_a_documented_detection_still_warns(self, example: str, template: str) -> None:
        assert _warns(template.format(v=example)), example

    @pytest.mark.parametrize(
        ("example", "template"), DOCUMENTED_LOSSES, ids=[e for e, _t in DOCUMENTED_LOSSES]
    )
    def test_a_documented_loss_is_really_lost(self, example: str, template: str) -> None:
        """If this reds because the value is now caught, that is good news —
        and DP-03's Limitations paragraph has to stop claiming it is lost."""
        assert not _warns(template.format(v=example)), example
