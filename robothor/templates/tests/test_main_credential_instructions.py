"""The main agent's starter instructions say what to do with a handed-over key.

Without a sentence here, a model handed an API key in chat falls back on what
it learned elsewhere: "put it in your settings page", "set an environment
variable", or it repeats the key back to confirm it. Each of those is wrong on
this platform, where the agent stores the key itself and answers with a
fingerprint.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[3]
    / "templates"
    / "agents"
    / "core"
    / "main"
    / "instructions.template.md"
)


def _section() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"^## Credentials\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, "main's instructions have no Credentials section"
    return match.group(1)


def test_it_names_the_tools_that_store_and_prove_a_credential():
    body = _section()
    assert "vault_set" in body
    assert "vault_test" in body
    assert "providers/<vendor>/api_key" in body


def test_it_answers_with_the_fingerprint_and_never_the_value():
    body = _section().lower()
    assert "fingerprint" in body
    assert "never repeat" in body


def test_it_keeps_cards_out_of_chat():
    body = _section().lower()
    assert "card" in body
    assert "enrollment link" in body


def test_it_says_what_to_do_when_refused():
    body = _section().lower()
    assert "gate" in body and "unlock" in body


def test_it_does_not_send_the_owner_away():
    body = _section().lower()
    for phrase in ("yourself", "environment variable", "settings page"):
        if phrase in body:
            # Allowed only as the thing NOT to do.
            assert re.search(rf"(never|not|don't|do not)[^.]*{re.escape(phrase)}", body), phrase
