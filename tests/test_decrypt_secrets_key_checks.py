"""The startup secret check must actually check what it claims to.

2026-08-27: ``REQUIRED_KEYS`` listed ``OPENROUTER_API_KEY`` twice. The
fourth slot was a copy-paste of the first, so the validation that runs on
every boot verified the primary credential twice and nothing else. The
spare key the platform's own ``key_pool.py`` needs was absent from the
SOPS store for two days and no boot ever said so — which is why a module
written specifically to survive a dead key was running with one key.

A duplicate in a validation list is always a bug: it is a slot that was
meant to check something else.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "decrypt-secrets.sh"


def _array(name: str) -> list[str]:
    text = SCRIPT.read_text()
    m = re.search(rf"^{name}=\((.*?)\n\)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(1))


def test_required_keys_has_no_duplicates():
    keys = _array("REQUIRED_KEYS")
    assert keys, "REQUIRED_KEYS not found — did the script move?"
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"duplicated slot(s) in REQUIRED_KEYS: {sorted(dupes)}"


def test_the_credential_spare_is_reported_on():
    """The spare need not be REQUIRED, but its absence must be visible."""
    text = SCRIPT.read_text()
    assert "OPENROUTER_API_KEY_2" in text, (
        "nothing in the boot path mentions the spare credential, so a pool of "
        "one stays silent — the precondition for the 2026-08-27 outage"
    )


def test_the_spare_is_advisory_not_required():
    """A missing spare must warn, never block a boot."""
    assert "OPENROUTER_API_KEY_2" not in _array("REQUIRED_KEYS")
