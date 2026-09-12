"""RFC 6238 TOTP: published test vectors, window behaviour, constant-time compare."""

from __future__ import annotations

import base64
import hmac
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from robothor.auth import totp

# RFC 6238 Appendix B, SHA-1, seed "12345678901234567890" (ASCII).
# The published vectors are 8 digits; a 6-digit code is their last six.
RFC_SECRET_ASCII = b"12345678901234567890"
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


def _rfc_secret_b32() -> str:
    return base64.b32encode(RFC_SECRET_ASCII).decode("ascii")


@pytest.mark.parametrize(("moment", "expected8"), RFC_VECTORS)
def test_rfc6238_vectors_8_digits(moment: int, expected8: str) -> None:
    assert totp.generate(_rfc_secret_b32(), timestamp=moment, digits=8) == expected8


@pytest.mark.parametrize(("moment", "expected8"), RFC_VECTORS)
def test_rfc6238_vectors_6_digits(moment: int, expected8: str) -> None:
    assert totp.generate(_rfc_secret_b32(), timestamp=moment) == expected8[-6:]


def test_generated_secret_is_160_random_bits_in_base32() -> None:
    secret = totp.new_secret()
    raw = base64.b32decode(secret, casefold=False)
    assert len(raw) == 20
    assert secret != totp.new_secret()


def test_verify_accepts_the_current_step() -> None:
    secret = totp.new_secret()
    code = totp.generate(secret, timestamp=1_000_000_000)
    assert totp.verify(secret, code, timestamp=1_000_000_000) is True


def test_verify_accepts_one_step_either_side_but_not_two() -> None:
    secret = totp.new_secret()
    now = 1_000_000_000
    assert totp.verify(secret, totp.generate(secret, timestamp=now - 30), timestamp=now) is True
    assert totp.verify(secret, totp.generate(secret, timestamp=now + 30), timestamp=now) is True
    assert totp.verify(secret, totp.generate(secret, timestamp=now - 60), timestamp=now) is False
    assert totp.verify(secret, totp.generate(secret, timestamp=now + 60), timestamp=now) is False


def test_verify_window_zero_rejects_the_neighbouring_step() -> None:
    secret = totp.new_secret()
    now = 1_000_000_000
    assert (
        totp.verify(secret, totp.generate(secret, timestamp=now - 30), timestamp=now, window=0)
        is False
    )


def test_verify_rejects_junk_without_raising() -> None:
    secret = totp.new_secret()
    for bad in ("", "   ", "abcdef", "12345", "1234567", None, "12 34 56"):
        assert totp.verify(secret, bad, timestamp=1_000_000_000) is False  # type: ignore[arg-type]


def test_verify_rejects_an_unusable_secret_instead_of_raising() -> None:
    assert totp.verify("not-base32!!", "123456", timestamp=1) is False
    assert totp.verify("", "123456", timestamp=1) is False


def test_verify_compares_in_constant_time() -> None:
    secret = totp.new_secret()
    now = 1_000_000_000
    with patch.object(totp.hmac, "compare_digest", wraps=hmac.compare_digest) as cmp_:
        totp.verify(secret, totp.generate(secret, timestamp=now), timestamp=now)
    assert cmp_.called, "code comparison must go through hmac.compare_digest"


def test_provisioning_uri_shape() -> None:
    secret = totp.new_secret()
    uri = totp.provisioning_uri(secret, email="alice@example.com")
    parsed = urlparse(uri)
    assert parsed.scheme == "otpauth"
    assert parsed.netloc == "totp"
    assert parsed.path == "/Genus%20OS:alice@example.com"
    params = parse_qs(parsed.query)
    assert params["secret"] == [secret]
    assert params["issuer"] == ["Genus OS"]
    assert params["digits"] == ["6"]
    assert params["period"] == ["30"]


def test_provisioning_uri_escapes_a_hostile_label() -> None:
    secret = totp.new_secret()
    uri = totp.provisioning_uri(secret, email="a b&issuer=Evil@example.com")
    assert "&issuer=Evil" not in uri.split("?", 1)[0]
    params = parse_qs(urlparse(uri).query)
    assert params["issuer"] == ["Genus OS"]
