"""A credential the platform has been handed is redacted by VALUE, not shape.

The shape pass catches ``sk-``, ``ghp_``, ``xoxb-`` and their kin. A weather,
maps or analytics API key is often 32 bare hex characters: no prefix, nothing
to recognise. Pasted into chat, it went through every redactor untouched and
landed in the history, ``chat_messages`` and ``agent_runs.task_text``.

Once ``vault_set`` has stored a value the platform KNOWS it, so from then on
every ``redact`` call replaces that exact value with its fingerprint. Only
values long enough to be a credential are taken: redacting a short value would
eat ordinary words out of every log line.

No assertion here prints a value: leak checks compute a bool first.
"""

from __future__ import annotations

import pytest

from robothor.secrets import redaction

SHAPELESS = "c0ffee42" * 4


@pytest.fixture(autouse=True)
def _clean():
    redaction.forget_known_values()
    yield
    redaction.forget_known_values()


def test_a_shapeless_key_is_not_caught_by_shape():
    """The premise: without the value, the redactor cannot see it."""
    line = f"my key is {SHAPELESS}"
    assert redaction.redact(line) == line


def test_redact_known_value_replaces_every_occurrence():
    out = redaction.redact_known_value(f"{SHAPELESS} and {SHAPELESS}", SHAPELESS, "<x>")
    assert out == "<x> and <x>"


def test_a_registered_value_is_redacted_by_every_later_call():
    assert redaction.register_known_value(SHAPELESS, fingerprint="b2:abcd1234") is True
    out = redaction.redact(f"my key is {SHAPELESS}, thanks")
    leaked = SHAPELESS in out
    assert not leaked
    assert out == "my key is <redacted:b2:abcd1234>, thanks"


def test_a_registered_value_is_redacted_from_a_chat_message():
    redaction.register_known_value(SHAPELESS, fingerprint="b2:abcd1234")
    cleaned = redaction.redact_message({"role": "user", "content": f"key: {SHAPELESS}"})
    leaked = SHAPELESS in cleaned["content"]
    assert not leaked


@pytest.mark.parametrize("value", ["", "short", "1234567"])
def test_a_value_under_eight_characters_is_not_registered(value):
    assert redaction.register_known_value(value, fingerprint="b2:abcd1234") is False
    assert redaction.redact("short 1234567 words") == "short 1234567 words"


def test_whitespace_around_a_value_is_not_part_of_it():
    redaction.register_known_value(f"  {SHAPELESS}\n", fingerprint="b2:abcd1234")
    leaked = SHAPELESS in redaction.redact(f"x {SHAPELESS} y")
    assert not leaked


def test_the_registry_is_bounded():
    for n in range(redaction.MAX_KNOWN_VALUES + 10):
        redaction.register_known_value(f"value-{n:08d}-padding", fingerprint=f"b2:{n:08x}")
    assert redaction.known_value_count() == redaction.MAX_KNOWN_VALUES


def test_a_longer_value_wins_over_a_value_it_contains():
    """Replacing the short one first would leave the long one's tail behind."""
    short = "abcdefgh12345678"
    long = short + "ZZZZ9999"
    redaction.register_known_value(short, fingerprint="b2:00000001")
    redaction.register_known_value(long, fingerprint="b2:00000002")
    assert redaction.redact(long) == "<redacted:b2:00000002>"
