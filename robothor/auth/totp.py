"""RFC 6238 time-based one-time passwords (HOTP/RFC 4226 over a time counter).

Hand-rolled on ``hmac`` + ``struct`` rather than adding ``pyotp``: the whole
algorithm is thirty lines of standard library, and a second-factor verifier is
exactly the kind of code that should not arrive through a supply chain we do
not already audit. Defaults match what every authenticator app assumes when a
provisioning URI omits them — SHA-1, 6 digits, a 30 second period.

Secrets are 160 random bits (``secrets.token_bytes(20)``), the size RFC 4226
§4 R6 recommends for HMAC-SHA-1, carried as unpadded-free base32 because that
is what ``otpauth://`` URIs and QR scanners speak.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

ISSUER = "Genus OS"
DIGITS = 6
PERIOD = 30
SECRET_BYTES = 20


def new_secret() -> str:
    """Return a fresh base32-encoded 160-bit TOTP secret."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii")


def _decode_secret(secret: str) -> bytes:
    """Decode a base32 secret, tolerating lower case and missing padding.

    Raises ``ValueError`` for anything that is not usable; callers in this
    module translate that into a failed verification rather than a 500.
    """
    cleaned = (secret or "").strip().replace(" ", "").replace("-", "").upper()
    if not cleaned:
        raise ValueError("empty TOTP secret")
    padded = cleaned + "=" * (-len(cleaned) % 8)
    return base64.b32decode(padded, casefold=True)


def _hotp(key: bytes, counter: int, digits: int) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), "sha1").digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


def generate(
    secret: str, *, timestamp: float | None = None, digits: int = DIGITS, period: int = PERIOD
) -> str:
    """Return the code for *secret* at *timestamp* (default: now)."""
    moment = time.time() if timestamp is None else timestamp
    return _hotp(_decode_secret(secret), int(moment) // period, digits)


def matching_step(
    secret: str,
    code: str | None,
    *,
    window: int = 1,
    timestamp: float | None = None,
    digits: int = DIGITS,
    period: int = PERIOD,
) -> int | None:
    """The time step *code* matches for *secret*, or ``None``.

    Callers need the step, not just a yes: RFC 6238 §5.2 requires that a code
    be accepted at most once, and the only way to enforce that is to remember
    which step was spent. ``robothor.auth.local_login`` stores it in
    ``user_accounts.mfa_last_used_step`` and refuses anything at or below it.

    Never raises: an unusable secret, a malformed code and a wrong code are
    all one answer, because the caller must not be able to tell them apart.
    Every comparison goes through ``hmac.compare_digest`` so a network-visible
    timing difference cannot leak how many leading digits were right.
    """
    candidate = (code or "").strip()
    if len(candidate) != digits or not candidate.isdigit():
        return None
    try:
        key = _decode_secret(secret)
    except (ValueError, TypeError):
        return None
    moment = time.time() if timestamp is None else timestamp
    counter = int(moment) // period
    found: int | None = None
    for drift in range(-window, window + 1):
        step = counter + drift
        if step < 0:
            continue
        # No early return: every step is compared on every call, so the loop
        # costs the same whether the match is the first candidate or the last.
        if hmac.compare_digest(_hotp(key, step, digits), candidate):
            found = step
    return found


def verify(
    secret: str,
    code: str | None,
    *,
    window: int = 1,
    timestamp: float | None = None,
    digits: int = DIGITS,
    period: int = PERIOD,
) -> bool:
    """Whether *code* is valid for *secret* within ±*window* steps.

    The boolean face of ``matching_step``. Anything enforcing single-use must
    call that instead — this cannot tell a fresh code from a replayed one.
    """
    return (
        matching_step(
            secret, code, window=window, timestamp=timestamp, digits=digits, period=period
        )
        is not None
    )


def provisioning_uri(secret: str, *, email: str, issuer: str = ISSUER) -> str:
    """Return the ``otpauth://totp/...`` URI an authenticator app scans.

    The label is percent-encoded, so an account name containing ``?``/``&``
    cannot smuggle its own ``issuer=`` or ``secret=`` parameter into the URI
    the operator is about to scan.
    """
    # ":" separates issuer from account name in the Key URI label and is left
    # literal; everything else — including any ":" inside the account name — is
    # escaped, because the issuer prefix is added here rather than by the caller.
    label = f"{quote(issuer, safe='')}:{quote(email, safe='@')}"
    query = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD,
        }
    )
    return f"otpauth://totp/{label}?{query}"
