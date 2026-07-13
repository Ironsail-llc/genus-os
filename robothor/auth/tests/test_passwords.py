"""Password hashing: round-trip, mismatch, no-hash."""

from __future__ import annotations

import pytest

from robothor.auth import passwords


def test_hash_verify_round_trip():
    h = passwords.hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"  # not plaintext
    assert passwords.verify_password("correct horse battery staple", h) is True


def test_wrong_password_fails():
    h = passwords.hash_password("s3cret")
    assert passwords.verify_password("wrong", h) is False


def test_no_hash_is_false_not_error():
    assert passwords.verify_password("anything", None) is False
    assert passwords.verify_password("anything", "") is False


def test_garbage_hash_is_false_not_error():
    assert passwords.verify_password("x", "not-a-real-argon2-hash") is False


def test_empty_password_rejected():
    with pytest.raises(ValueError):
        passwords.hash_password("")


def test_hashes_are_salted_unique():
    assert passwords.hash_password("same") != passwords.hash_password("same")
