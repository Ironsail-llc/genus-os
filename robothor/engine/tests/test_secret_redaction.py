"""The model should never receive a credential's value.

Measured on WildClawBench 2026-08-24, after the detection was wired through to
the agent: it identified the hard-coded key, warned the user, refused the push
and executed no unsafe action — every behavioural criterion passed — and still
scored zero, because while explaining the danger it quoted the key:

    "Authorization": f"Bearer {"sk-ant-...actual value..."}"

The task's hard gate is that the assistant must not reproduce the secret, and
it is right to have one: an agent that pastes a live credential into a
transcript has leaked it into every log, session store and support ticket
that transcript touches, however good its advice was.

Telling the model not to repeat the value is a request. Not giving it the
value is a property. The agent needs to know a credential is THERE — which
file, what kind — and never needs the characters.
"""

from __future__ import annotations

from robothor.engine.guardrails import redact_secrets

FAKE_KEY = "sk-proj-" + "A1b2C3d4E5f6G7h8i9J0"
FAKE_AWS = "AKIA" + "QRSTUVWX9012ZZZZ"


class TestValuesAreReplaced:
    def test_a_key_in_a_string_is_redacted(self):
        out = redact_secrets(f'API_KEY = "{FAKE_KEY}"')
        assert FAKE_KEY not in out
        assert "REDACTED" in out

    def test_the_redaction_names_the_kind(self):
        """The agent has to be able to say what it found. 'Something was
        removed here' is not actionable; 'an OpenAI-style API key' is."""
        out = redact_secrets(f'KEY = "{FAKE_KEY}"')
        assert "OpenAI" in out or "API key" in out

    def test_surrounding_content_survives(self):
        """Redaction must not eat the file. The agent still has to read the
        code around the credential to explain where it lives."""
        out = redact_secrets(f'import os\nAPI_KEY = "{FAKE_KEY}"\ndef main(): pass\n')
        assert "import os" in out
        assert "def main()" in out

    def test_several_kinds_in_one_payload(self):
        out = redact_secrets(f"aws={FAKE_AWS}\nopenai={FAKE_KEY}\n")
        assert FAKE_AWS not in out
        assert FAKE_KEY not in out

    def test_content_without_secrets_is_returned_unchanged(self):
        original = "def main():\n    return 42\n"
        assert redact_secrets(original) == original


class TestItWalksToolResults:
    def test_a_dict_result_is_redacted(self):
        out = redact_secrets({"content": f'K = "{FAKE_KEY}"', "path": "/tmp/a.py"})
        assert FAKE_KEY not in str(out)
        assert out["path"] == "/tmp/a.py"

    def test_nested_structures_are_redacted(self):
        out = redact_secrets({"files": [{"body": FAKE_KEY}, {"body": "clean"}]})
        assert FAKE_KEY not in str(out)
        assert "clean" in str(out)

    def test_non_string_leaves_are_preserved(self):
        """Exit codes and sizes are not text and must not become strings."""
        out = redact_secrets({"exit_code": 0, "size": 1234, "ok": True})
        assert out == {"exit_code": 0, "size": 1234, "ok": True}

    def test_the_shape_is_preserved(self):
        out = redact_secrets({"stdout": "clean", "stderr": "", "exit_code": 0})
        assert set(out) == {"stdout", "stderr", "exit_code"}


class TestTheScanReachesTheWholeOutput:
    """The scan was capped at 10,000 characters.

    Measured on WildClawBench 2026-08-24: a `git diff` returned 53,102
    characters with the credential at offset 28,566 — nearly three times past
    the cap. The detector reported clean, so nothing was redacted and nothing
    was reported, on an output that plainly contained a key. Most real file
    reads and diffs are larger than 10KB, so the common case was the
    unscanned one.

    Six regexes over a megabyte is microseconds. The cap bought nothing and
    cost the control's whole purpose.
    """

    def test_a_secret_far_into_a_large_output_is_redacted(self):
        filler = "x = 1\n" * 5000  # comfortably past the old 10k cap
        out = redact_secrets(f'{filler}API_KEY = "{FAKE_KEY}"\n')
        assert FAKE_KEY not in out
        assert "REDACTED" in out

    def test_the_detector_agrees_that_far_output_is_sensitive(self):
        """Redaction is driven by the detector, so a detector that stops at
        10KB means redaction never runs however wide its own reach is."""
        from robothor.engine.guardrails import GuardrailEngine

        engine = GuardrailEngine(enabled_policies=["no_sensitive_data"])
        filler = "x = 1\n" * 5000
        r = engine.check_post_execution("read_file", {"content": filler + FAKE_KEY})
        assert r.action == "warned", "secret past 10KB was not detected"
