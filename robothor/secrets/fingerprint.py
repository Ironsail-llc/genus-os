"""A short, stable, non-reversible name for a credential.

The question an operator and an assistant both need to answer without anybody
reading a credential is "which one is stored?" — after a rotation, between two
stores, across the Helm page and a tool result. A fingerprint answers it: the
same value gives the same eight characters everywhere, a different value does
not, and nothing about the value can be recovered from them.

Keyed (HMAC) rather than a bare digest, so the fingerprint of a leaked token
cannot be looked up in a rainbow table of leaked tokens and matched to this
instance. Still labelled ``sha256:`` because that is the algorithm a reader
sees, and because the convention it replaces — printing the last four
characters — prints real key material.

This lives in ``robothor.secrets`` rather than in ``engine/key_pool`` (where it
was born) because four surfaces now print one: the provider API, the settings
API, the vault tools and the ``genus secrets status`` table. Four
implementations of one digest is four digests the moment one of them is
"improved", and a fingerprint whose value depends on which code path printed it
answers nothing.
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["FINGERPRINT_PREFIX", "fingerprint"]

#: Domain separation, not a secret: it only has to make this digest a different
#: function from a plain SHA-256 of the same value.
_FINGERPRINT_KEY = b"genus-key-fingerprint"

FINGERPRINT_PREFIX = "sha256:"


def fingerprint(value: str) -> str:
    """``sha256:xxxxxxxx`` for ``value``. Safe to print, log and return."""
    digest = hmac.new(_FINGERPRINT_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return FINGERPRINT_PREFIX + digest[:8]
