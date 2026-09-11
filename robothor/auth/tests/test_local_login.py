"""Local email+password sign-in: generic failures, lockout, MFA, owner policy.

The DB is mocked exactly as ``test_accounts.py`` mocks it — these tests are
about the decision logic, and a live Postgres would make them slow without
testing anything more.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.auth import local_login, mfa_secrets, totp
from robothor.auth.passwords import hash_password

GOOD_PASSWORD = "correct horse battery staple"
SIGNING_KEY = "test-signing-key-at-least-32-bytes-long-xyz"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from robothor.auth import tokens

    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "true")
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.delenv("GENUS_OIDC_ISSUERS", raising=False)
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    tokens.reset_signing_key_cache()
    mfa_secrets.reset_key_cache()
    local_login.reset_rate_limiter()
    yield
    tokens.reset_signing_key_cache()
    mfa_secrets.reset_key_cache()
    local_login.reset_rate_limiter()


def account(**overrides):
    row = {
        "id": "uid-1",
        "tenant_id": "default",
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "member",
        "status": "active",
        "password_hash": hash_password(GOOD_PASSWORD),
        "mfa_enabled": False,
        "mfa_secret_enc": None,
        "failed_login_count": 0,
        "locked_until": None,
    }
    row.update(overrides)
    return row


TOKENS = {
    "access_token": "a",
    "refresh_token": "r",
    "user": {
        "id": "uid-1",
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "member",
        "tenant_id": "default",
    },
}


def _authenticate(row, password=GOOD_PASSWORD, code=None, ip="10.0.0.1"):
    """Run authenticate() against ``row`` with the DAL and issuance mocked.

    Returns ``(result, failure_recorder, success_recorder)``.
    """
    with (
        patch.object(local_login, "_load_account", return_value=row),
        patch.object(local_login, "_record_failure") as fail,
        patch.object(local_login, "_record_success") as ok,
        patch("robothor.auth.accounts.issue_for_account", return_value=dict(TOKENS)),
    ):
        result = local_login.authenticate("default", "alice@example.com", password, code, ip=ip)
    return result, fail, ok


# ── generic failure surface ──────────────────────────────────────────


def test_wrong_password_is_a_generic_failure_and_counts() -> None:
    result, fail, ok = _authenticate(account(), password="wrong")
    assert result.ok is False and result.mfa_required is False
    assert result.error == "invalid credentials"
    fail.assert_called_once()
    ok.assert_not_called()


def test_unknown_email_gives_the_same_message_as_a_wrong_password() -> None:
    with patch.object(local_login, "_load_account", return_value=None):
        unknown = local_login.authenticate("default", "nobody@example.com", "x", ip="10.0.0.1")
    wrong, _, _ = _authenticate(account(), password="wrong")
    assert unknown.ok is False
    assert unknown.error == wrong.error == "invalid credentials"
    assert unknown.mfa_required is wrong.mfa_required is False


def test_unknown_email_still_performs_a_hash_comparison() -> None:
    """Otherwise the response time tells an attacker which emails exist."""
    with (
        patch.object(local_login, "_load_account", return_value=None),
        patch.object(local_login, "verify_password", return_value=False) as verify,
    ):
        local_login.authenticate("default", "nobody@example.com", "x", ip="10.0.0.1")
    verify.assert_called_once()


def test_inactive_account_is_a_generic_failure() -> None:
    result, _, ok = _authenticate(account(status="disabled"))
    assert result.ok is False and result.error == "invalid credentials"
    assert result.mfa_required is False
    ok.assert_not_called()


def test_account_without_a_password_hash_cannot_sign_in() -> None:
    result, _, _ = _authenticate(account(password_hash=None))
    assert result.ok is False and result.error == "invalid credentials"


# ── lockout ──────────────────────────────────────────────────────────


def test_tenth_consecutive_failure_locks_for_fifteen_minutes() -> None:
    cur = MagicMock()
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cur
    cur.fetchone.return_value = {"failed_login_count": 10, "locked_until": "later"}
    with patch("robothor.auth.local_login.get_connection", return_value=conn):
        local_login._record_failure("uid-1")
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "failed_login_count" in sql and "locked_until" in sql
    params = cur.execute.call_args[0][1]
    assert local_login.LOCKOUT_THRESHOLD in params
    assert local_login.LOCKOUT_SECONDS in params
    assert local_login.LOCKOUT_SECONDS == 15 * 60
    assert local_login.LOCKOUT_THRESHOLD == 10


def test_a_locked_account_is_refused_generically_even_with_the_right_password() -> None:
    from datetime import UTC, datetime, timedelta

    locked = account(failed_login_count=10, locked_until=datetime.now(UTC) + timedelta(minutes=5))
    result, fail, ok = _authenticate(locked)
    assert result.ok is False and result.error == "invalid credentials"
    assert result.mfa_required is False
    ok.assert_not_called()
    # A locked account must not have its clock pushed further out by retries.
    fail.assert_not_called()


def test_an_expired_lock_no_longer_blocks() -> None:
    from datetime import UTC, datetime, timedelta

    expired = account(failed_login_count=10, locked_until=datetime.now(UTC) - timedelta(seconds=1))
    result, _, ok = _authenticate(expired)
    assert result.ok is True
    ok.assert_called_once()


def test_success_resets_the_counter() -> None:
    result, _, ok = _authenticate(account(failed_login_count=4))
    assert result.ok is True
    ok.assert_called_once_with("uid-1")


# ── MFA ──────────────────────────────────────────────────────────────


def _mfa_account(secret: str, **overrides):
    return account(mfa_enabled=True, mfa_secret_enc=mfa_secrets.encrypt_secret(secret), **overrides)


def test_mfa_required_only_after_a_correct_password() -> None:
    secret = totp.new_secret()
    wrong, _, _ = _authenticate(_mfa_account(secret), password="wrong")
    assert wrong.mfa_required is False and wrong.error == "invalid credentials"

    right, _, _ = _authenticate(_mfa_account(secret))
    assert right.ok is False and right.mfa_required is True


def test_correct_password_and_code_signs_in() -> None:
    secret = totp.new_secret()
    result, _, ok = _authenticate(_mfa_account(secret), code=totp.generate(secret))
    assert result.ok is True and result.tokens == TOKENS
    ok.assert_called_once()


def test_a_wrong_code_does_not_unlock_and_counts_as_a_failure() -> None:
    secret = totp.new_secret()
    result, fail, ok = _authenticate(_mfa_account(secret), code="000000")
    assert result.ok is False and result.mfa_required is True
    assert result.tokens is None
    ok.assert_not_called()
    fail.assert_called_once()


def test_an_undecryptable_secret_refuses_rather_than_bypassing() -> None:
    row = account(mfa_enabled=True, mfa_secret_enc="not-a-ciphertext")
    result, _, ok = _authenticate(row, code="000000")
    assert result.ok is False and result.mfa_required is True
    ok.assert_not_called()


def test_a_pending_unconfirmed_secret_never_satisfies_a_challenge() -> None:
    """mfa_enabled is False, so the stored secret is an unfinished enrollment."""
    secret = totp.new_secret()
    row = account(mfa_enabled=False, mfa_secret_enc=mfa_secrets.encrypt_secret(secret))
    result, _, ok = _authenticate(row, code=totp.generate(secret))
    assert result.ok is True  # password alone is enough; MFA is not yet on
    ok.assert_called_once()


# ── owner MFA policy ─────────────────────────────────────────────────


def test_owner_without_mfa_and_no_other_provider_must_set_it_up() -> None:
    result, _, _ = _authenticate(account(role="owner"))
    assert result.ok is True and result.mfa_setup_required is True


def test_owner_with_oidc_configured_is_not_forced(monkeypatch) -> None:
    monkeypatch.setenv("GENUS_OIDC_ISSUERS", "https://idp.example.test")
    result, _, _ = _authenticate(account(role="owner"))
    assert result.ok is True and result.mfa_setup_required is False


def test_owner_with_cloudflare_access_configured_is_not_forced(monkeypatch) -> None:
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud-1")
    result, _, _ = _authenticate(account(role="owner"))
    assert result.mfa_setup_required is False


def test_owner_with_mfa_enabled_is_not_forced() -> None:
    secret = totp.new_secret()
    result, _, _ = _authenticate(_mfa_account(secret, role="owner"), code=totp.generate(secret))
    assert result.ok is True and result.mfa_setup_required is False


def test_non_owner_is_never_forced() -> None:
    result, _, _ = _authenticate(account(role="member"))
    assert result.ok is True and result.mfa_setup_required is False


# ── rate limiting ────────────────────────────────────────────────────


def test_sixth_attempt_in_a_minute_is_rate_limited() -> None:
    for _ in range(local_login.RATE_LIMIT_ATTEMPTS):
        result, _, _ = _authenticate(account(), password="wrong")
        assert result.rate_limited is False
    result, fail, _ = _authenticate(account(), password="wrong")
    assert result.rate_limited is True
    # A throttled attempt must not be charged against the lockout counter.
    fail.assert_not_called()


def test_the_limiter_is_scoped_per_email_and_ip() -> None:
    for _ in range(local_login.RATE_LIMIT_ATTEMPTS + 1):
        _authenticate(account(), password="wrong", ip="10.0.0.1")
    with (
        patch.object(local_login, "_load_account", return_value=account()),
        patch.object(local_login, "_record_failure"),
    ):
        other_ip = local_login.authenticate("default", "alice@example.com", "wrong", ip="10.0.0.2")
        other_email = local_login.authenticate("default", "bob@example.com", "wrong", ip="10.0.0.1")
    assert other_ip.rate_limited is False
    assert other_email.rate_limited is False


def test_the_limiter_does_not_grow_without_bound() -> None:
    for n in range(local_login.RATE_LIMIT_MAX_KEYS + 50):
        local_login._rate_limited(f"user{n}@example.com", f"10.1.{n // 256}.{n % 256}")
    assert len(local_login._ATTEMPTS) <= local_login.RATE_LIMIT_MAX_KEYS


# ── password policy ──────────────────────────────────────────────────


def test_a_short_password_is_refused() -> None:
    with pytest.raises(local_login.WeakPasswordError):
        local_login.validate_password("short")


def test_an_absurdly_long_password_is_refused() -> None:
    with pytest.raises(local_login.WeakPasswordError):
        local_login.validate_password("x" * 5000)


def test_a_twelve_character_password_is_accepted() -> None:
    local_login.validate_password("x" * local_login.MIN_PASSWORD_LENGTH)


def test_change_password_refuses_a_wrong_current_password() -> None:
    row = account()
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_store_password_hash") as store,
    ):
        assert local_login.change_password("uid-1", "nope", "a-new-long-password") is False
    store.assert_not_called()


def test_change_password_stores_an_argon2id_hash() -> None:
    row = account()
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_store_password_hash") as store,
    ):
        assert local_login.change_password("uid-1", GOOD_PASSWORD, "a-new-long-password") is True
    stored = store.call_args[0][1]
    assert stored.startswith("$argon2id$")
    assert "a-new-long-password" not in stored


# ── enrollment ───────────────────────────────────────────────────────


def test_enrollment_stores_the_secret_encrypted_and_disabled() -> None:
    with patch.object(local_login, "_store_mfa_secret") as store:
        out = local_login.begin_enrollment("uid-1", "alice@example.com")
    assert out["secret"] and out["otpauth_uri"].startswith("otpauth://totp/")
    stored = store.call_args[0][1]
    assert out["secret"] not in stored
    assert mfa_secrets.decrypt_secret(stored) == out["secret"]
    assert store.call_args.kwargs["enabled"] is False


def test_confirm_enrollment_requires_the_right_code() -> None:
    secret = totp.new_secret()
    row = account(mfa_enabled=False, mfa_secret_enc=mfa_secrets.encrypt_secret(secret))
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_set_mfa_enabled") as enable,
    ):
        assert local_login.confirm_enrollment("uid-1", "000000") is False
        enable.assert_not_called()
        assert local_login.confirm_enrollment("uid-1", totp.generate(secret)) is True
        enable.assert_called_once_with("uid-1", True)


def test_disable_mfa_requires_both_password_and_code() -> None:
    secret = totp.new_secret()
    row = _mfa_account(secret)
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_clear_mfa") as clear,
    ):
        assert local_login.disable_mfa("uid-1", "wrong", totp.generate(secret)) is False
        assert local_login.disable_mfa("uid-1", GOOD_PASSWORD, "000000") is False
        clear.assert_not_called()
        assert local_login.disable_mfa("uid-1", GOOD_PASSWORD, totp.generate(secret)) is True
        clear.assert_called_once_with("uid-1")


# ── feature gate ─────────────────────────────────────────────────────


def test_local_login_is_off_unless_explicitly_enabled(monkeypatch) -> None:
    monkeypatch.delenv("GENUS_LOCAL_LOGIN", raising=False)
    assert local_login.local_login_enabled() is False
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "false")
    assert local_login.local_login_enabled() is False
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "true")
    assert local_login.local_login_enabled() is True


def test_authenticate_refuses_when_the_feature_is_off(monkeypatch) -> None:
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "false")
    with patch.object(local_login, "_load_account") as load:
        result = local_login.authenticate("default", "alice@example.com", GOOD_PASSWORD)
    assert result.ok is False
    load.assert_not_called()
