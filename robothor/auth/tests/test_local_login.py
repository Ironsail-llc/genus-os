"""Local email+password sign-in: generic failures, lockout, MFA, owner policy.

The DB is mocked exactly as ``test_accounts.py`` mocks it — these tests are
about the decision logic, and a live Postgres would make them slow without
testing anything more.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from robothor.auth import local_login, mfa_secrets, totp
from robothor.auth.passwords import hash_password

GOOD_PASSWORD = "correct horse battery staple"
USER_ID = "11111111-1111-1111-1111-111111111111"
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
        "id": USER_ID,
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
        "id": USER_ID,
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
        patch.object(local_login, "_record_noop"),
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
    with (
        patch.object(local_login, "_load_account", return_value=None),
        patch.object(local_login, "_record_noop"),
    ):
        unknown = local_login.authenticate("default", "nobody@example.com", "x", ip="10.0.0.1")
    wrong, _, _ = _authenticate(account(), password="wrong")
    assert unknown.ok is False
    assert unknown.error == wrong.error == "invalid credentials"
    assert unknown.mfa_required is wrong.mfa_required is False


def test_unknown_email_still_performs_a_hash_comparison() -> None:
    """Otherwise the response time tells an attacker which emails exist."""
    with (
        patch.object(local_login, "_load_account", return_value=None),
        patch.object(local_login, "_record_noop"),
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
        local_login._record_failure(USER_ID)
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
    ok.assert_called_once_with(USER_ID, mfa_step=None)


# ── MFA ──────────────────────────────────────────────────────────────


def _mfa_account(secret: str, **overrides):
    return account(
        mfa_enabled=True,
        mfa_secret_enc=mfa_secrets.encrypt_secret(secret, user_id=USER_ID),
        **overrides,
    )


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
    row = account(
        mfa_enabled=False, mfa_secret_enc=mfa_secrets.encrypt_secret(secret, user_id=USER_ID)
    )
    result, _, ok = _authenticate(row, code=totp.generate(secret))
    assert result.ok is True  # password alone is enough; MFA is not yet on
    ok.assert_called_once()


# ── owner MFA policy ─────────────────────────────────────────────────


def test_owner_without_mfa_must_set_it_up_while_local_login_is_on() -> None:
    result, _, _ = _authenticate(account(role="owner"))
    assert result.ok is True and result.mfa_setup_required is True


def test_owner_is_still_forced_when_oidc_issuers_are_configured(monkeypatch) -> None:
    """GENUS_OIDC_ISSUERS is not a sign-in method, and must not turn the policy off.

    It is the BRIDGE's allowlist of issuers whose tokens it will accept. A human
    can only sign in with OIDC when the DASHBOARD has AUTH_OIDC_ISSUER and
    AUTH_OIDC_CLIENT_ID — a disjoint pair of variables. Deciding the policy from
    it meant a stale value, or the one infra/robothor.env.example tells the
    operator to set for Cloudflare Access, silently left the owner with a single
    password as the entire authentication for the appliance.
    """
    monkeypatch.setenv("GENUS_OIDC_ISSUERS", "https://idp.example.test")
    result, _, _ = _authenticate(account(role="owner"))
    assert result.ok is True and result.mfa_setup_required is True


def test_owner_is_still_forced_when_cloudflare_access_is_configured(monkeypatch) -> None:
    """CF_ACCESS_* are dashboard-only secrets, unset in the bridge process."""
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud-1")
    result, _, _ = _authenticate(account(role="owner"))
    assert result.mfa_setup_required is True


def test_the_operator_can_turn_the_policy_off_explicitly(monkeypatch) -> None:
    monkeypatch.setenv("GENUS_OWNER_MFA_REQUIRED", "false")
    result, _, _ = _authenticate(account(role="owner"))
    assert result.ok is True and result.mfa_setup_required is False


def test_no_one_is_forced_when_local_login_is_off(monkeypatch) -> None:
    """Nothing to enrol against: with no password endpoint there is no password."""
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "false")
    from robothor.settings import reset_settings

    reset_settings()
    assert local_login.mfa_setup_required_for(account(role="owner")) is False


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
        patch.object(local_login, "_record_noop"),
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
        assert local_login.change_password(USER_ID, "nope", "a-new-long-password") is False
    store.assert_not_called()


def test_change_password_stores_an_argon2id_hash() -> None:
    row = account()
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_store_password_hash") as store,
        patch("robothor.auth.accounts.revoke_user_sessions"),
    ):
        assert local_login.change_password(USER_ID, GOOD_PASSWORD, "a-new-long-password") is True
    stored = store.call_args[0][1]
    assert stored.startswith("$argon2id$")
    assert "a-new-long-password" not in stored


# ── enrollment ───────────────────────────────────────────────────────


def test_enrollment_stores_the_secret_encrypted_and_disabled() -> None:
    with (
        patch.object(local_login, "_load_account_by_id", return_value=account()),
        patch.object(local_login, "_store_mfa_secret") as store,
    ):
        out = local_login.begin_enrollment(USER_ID, GOOD_PASSWORD)
    assert out["secret"] and out["otpauth_uri"].startswith("otpauth://totp/")
    stored = store.call_args[0][1]
    assert out["secret"] not in stored
    assert mfa_secrets.decrypt_secret(stored, user_id=USER_ID) == out["secret"]
    assert store.call_args.kwargs["enabled"] is False


def test_confirm_enrollment_requires_the_right_code() -> None:
    secret = totp.new_secret()
    row = account(
        mfa_enabled=False, mfa_secret_enc=mfa_secrets.encrypt_secret(secret, user_id=USER_ID)
    )
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_confirm_enrollment_atomically", return_value=True) as enable,
    ):
        assert local_login.confirm_enrollment(USER_ID, "000000") is False
        enable.assert_not_called()
        assert local_login.confirm_enrollment(USER_ID, totp.generate(secret)) is True
        assert enable.call_args[0][0] == USER_ID


def test_disable_mfa_requires_both_password_and_code() -> None:
    secret = totp.new_secret()
    row = _mfa_account(secret)
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_clear_mfa") as clear,
        patch.object(local_login, "_record_mfa_step"),
    ):
        assert local_login.disable_mfa(USER_ID, "wrong", totp.generate(secret)) is False
        assert local_login.disable_mfa(USER_ID, GOOD_PASSWORD, "000000") is False
        clear.assert_not_called()
        assert local_login.disable_mfa(USER_ID, GOOD_PASSWORD, totp.generate(secret)) is True
        clear.assert_called_once_with(USER_ID)


# ── feature gate ─────────────────────────────────────────────────────


def test_local_login_is_off_unless_explicitly_enabled(monkeypatch) -> None:
    from robothor.settings import reset_settings

    monkeypatch.delenv("GENUS_LOCAL_LOGIN", raising=False)
    reset_settings()
    assert local_login.local_login_enabled() is False
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "false")
    reset_settings()
    assert local_login.local_login_enabled() is False
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "true")
    reset_settings()
    assert local_login.local_login_enabled() is True


def test_authenticate_refuses_when_the_feature_is_off(monkeypatch) -> None:
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "false")
    with patch.object(local_login, "_load_account") as load:
        result = local_login.authenticate("default", "alice@example.com", GOOD_PASSWORD)
    assert result.ok is False
    load.assert_not_called()


# ── hostile-review findings ──────────────────────────────────────────


def test_an_account_with_no_password_still_costs_a_hash_comparison() -> None:
    """verify_password(x, None) returns False instantly. Without an explicit
    equalizer, response time tells an attacker which accounts are SSO-only."""
    with (
        patch.object(local_login, "_load_account", return_value=account(password_hash=None)),
        patch.object(local_login, "_record_failure"),
        patch.object(local_login, "verify_password", return_value=False) as verify,
    ):
        local_login.authenticate("default", "alice@example.com", "x", ip="10.0.0.1")
    assert verify.call_args[0][1] == local_login._dummy_hash()


def test_enrollment_refuses_to_overwrite_a_confirmed_factor() -> None:
    """Otherwise POST /mfa/enroll from a hijacked session silently turns the
    victim's second factor OFF by replacing the secret with a pending one."""
    secret = totp.new_secret()
    row = _mfa_account(secret)
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_store_mfa_secret") as store,
    ):
        with pytest.raises(local_login.MfaAlreadyEnabledError):
            local_login.begin_enrollment(USER_ID, GOOD_PASSWORD)
    store.assert_not_called()


def test_changing_a_password_revokes_every_refresh_session() -> None:
    """A password change that leaves a stolen refresh token working has not
    actually locked the attacker out."""
    with (
        patch.object(local_login, "_load_account_by_id", return_value=account()),
        patch.object(local_login, "_store_password_hash"),
        patch("robothor.auth.accounts.revoke_user_sessions") as revoke,
    ):
        assert local_login.change_password(USER_ID, GOOD_PASSWORD, "a-new-long-password") is True
    revoke.assert_called_once_with(USER_ID, except_refresh_hash=None)


def test_set_password_revokes_every_refresh_session_too() -> None:
    with (
        patch.object(local_login, "_store_password_hash"),
        patch("robothor.auth.accounts.revoke_user_sessions") as revoke,
    ):
        local_login.set_password(USER_ID, "a-new-long-password")
    revoke.assert_called_once_with(USER_ID)


def test_sign_in_attempts_cannot_consume_another_routes_quota() -> None:
    """The limiter is one map. A login key and a /mfa key for the same string
    must not be the same bucket."""
    local_login.reset_rate_limiter()
    for _ in range(local_login.RATE_LIMIT_ATTEMPTS):
        assert local_login.throttled("mfa:uid-1", "10.0.0.1") is False
    assert local_login.throttled("mfa:uid-1", "10.0.0.1") is True
    with (
        patch.object(local_login, "_load_account", return_value=None),
        patch.object(local_login, "_record_noop"),
        patch.object(local_login, "_record_failure"),
    ):
        result = local_login.authenticate("default", "mfa:uid-1", "x", ip="10.0.0.1")
    assert result.rate_limited is False


# ── fix round 1 ──────────────────────────────────────────────────────


def test_a_totp_code_cannot_be_used_twice() -> None:
    """RFC 6238 §5.2. The verifier has a ±1 step window, so without a
    watermark the same six digits keep working for ninety seconds — long
    enough for a code read over a shoulder, phished, or scraped from a log."""
    secret = totp.new_secret()
    code = totp.generate(secret)
    first, _, ok = _authenticate(_mfa_account(secret), code=code)
    assert first.ok is True
    step = ok.call_args.kwargs["mfa_step"]
    assert isinstance(step, int)

    replayed = _mfa_account(secret, mfa_last_used_step=step)
    second, fail, ok2 = _authenticate(replayed, code=code)
    assert second.ok is False and second.mfa_required is True
    ok2.assert_not_called()
    fail.assert_called_once()


def test_an_older_step_inside_the_window_is_also_refused() -> None:
    secret = totp.new_secret()
    now = int(time.time())
    previous = totp.generate(secret, timestamp=now - 30)
    row = _mfa_account(secret, mfa_last_used_step=now // 30)
    result, _, ok = _authenticate(row, code=previous)
    assert result.ok is False and result.mfa_required is True
    ok.assert_not_called()


def test_a_fresh_step_after_a_burned_one_still_works() -> None:
    secret = totp.new_secret()
    now = int(time.time())
    row = _mfa_account(secret, mfa_last_used_step=(now // 30) - 5)
    result, _, ok = _authenticate(row, code=totp.generate(secret))
    assert result.ok is True
    # >= rather than ==: the clock can cross a step boundary between
    # generating the code and verifying it, and that is a pass, not a flake.
    assert ok.call_args.kwargs["mfa_step"] >= now // 30


def test_confirming_an_enrollment_burns_the_step_it_used() -> None:
    secret = totp.new_secret()
    row = account(
        mfa_enabled=False, mfa_secret_enc=mfa_secrets.encrypt_secret(secret, user_id=USER_ID)
    )
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_confirm_enrollment_atomically", return_value=True) as burn,
    ):
        assert local_login.confirm_enrollment(USER_ID, totp.generate(secret)) is True
    assert burn.call_args[0][0] == USER_ID
    assert isinstance(burn.call_args[0][1], int)


def test_enrollment_requires_the_current_password() -> None:
    """A stolen session alone must not bind an authenticator: after that, the
    real owner's password is no longer enough to get back in."""
    with (
        patch.object(local_login, "_load_account_by_id", return_value=account()),
        patch.object(local_login, "_store_mfa_secret") as store,
    ):
        with pytest.raises(local_login.EnrollmentDeniedError):
            local_login.begin_enrollment(USER_ID, "not-the-password")
    store.assert_not_called()


def test_enrollment_uses_the_accounts_own_email_not_a_caller_supplied_one() -> None:
    with (
        patch.object(local_login, "_load_account_by_id", return_value=account()),
        patch.object(local_login, "_store_mfa_secret"),
    ):
        out = local_login.begin_enrollment(USER_ID, GOOD_PASSWORD)
    assert "alice@example.com" in out["otpauth_uri"]


def test_enrollment_binds_the_secret_to_this_account_row() -> None:
    with (
        patch.object(local_login, "_load_account_by_id", return_value=account()),
        patch.object(local_login, "_store_mfa_secret") as store,
    ):
        out = local_login.begin_enrollment(USER_ID, GOOD_PASSWORD)
    stored = store.call_args[0][1]
    other = "22222222-2222-2222-2222-222222222222"
    assert mfa_secrets.decrypt_secret(stored, user_id=other) is None
    assert mfa_secrets.decrypt_secret(stored, user_id=USER_ID) == out["secret"]


def test_a_transplanted_secret_refuses_the_sign_in_rather_than_accepting_it() -> None:
    secret = totp.new_secret()
    stolen = mfa_secrets.encrypt_secret(secret, user_id="22222222-2222-2222-2222-222222222222")
    row = account(mfa_enabled=True, mfa_secret_enc=stolen)
    result, _, ok = _authenticate(row, code=totp.generate(secret))
    assert result.ok is False and result.mfa_required is True
    ok.assert_not_called()


def test_changing_a_password_can_spare_the_callers_own_session() -> None:
    with (
        patch.object(local_login, "_load_account_by_id", return_value=account()),
        patch.object(local_login, "_store_password_hash"),
        patch("robothor.auth.accounts.revoke_user_sessions") as revoke,
    ):
        assert (
            local_login.change_password(
                USER_ID, GOOD_PASSWORD, "a-new-long-password", keep_refresh_hash="hash-of-mine"
            )
            is True
        )
    revoke.assert_called_once_with(USER_ID, except_refresh_hash="hash-of-mine")


def test_every_failure_branch_performs_an_equal_cost_write() -> None:
    """The wrong-password branch used to UPDATE and COMMIT while the
    unknown-email, disabled and locked branches returned straight away — a
    difference an attacker can time, which is the enumeration oracle the single
    failure message exists to close."""
    from datetime import UTC, datetime, timedelta

    branches = {
        "unknown email": None,
        "disabled": account(status="disabled"),
        "locked": account(locked_until=datetime.now(UTC) + timedelta(minutes=5)),
    }
    for label, row in branches.items():
        with (
            patch.object(local_login, "_load_account", return_value=row),
            patch.object(local_login, "_record_noop") as noop,
            patch.object(local_login, "_record_failure") as fail,
        ):
            local_login.reset_rate_limiter()
            local_login.authenticate("default", "alice@example.com", GOOD_PASSWORD, ip="10.0.0.1")
        assert noop.call_count == 1, f"{label} branch performed no write"
        fail.assert_not_called()

    with (
        patch.object(local_login, "_load_account", return_value=account()),
        patch.object(local_login, "_record_noop") as noop,
        patch.object(local_login, "_record_failure") as fail,
    ):
        local_login.reset_rate_limiter()
        local_login.authenticate("default", "alice@example.com", "wrong", ip="10.0.0.1")
    assert fail.call_count == 1 and noop.call_count == 0


def test_the_dummy_hash_is_not_computed_at_import() -> None:
    """argon2 is deliberately expensive; paying for it on every `genus`
    invocation for a value most of them never read is pure latency."""
    import importlib

    module = importlib.reload(local_login)
    assert module._dummy_hash_cache is None
    first = module._dummy_hash()
    assert first.startswith("$argon2id$")
    assert module._dummy_hash() is first


def test_the_audit_subject_is_keyed_stable_and_not_the_address() -> None:
    handle = local_login.audit_subject("Alice@Example.com")
    assert handle == local_login.audit_subject("alice@example.com")
    assert handle != local_login.audit_subject("bob@example.com")
    assert "alice" not in handle.lower()
    assert len(handle) == 32


# ── fix round 2 ──────────────────────────────────────────────────────


def test_the_sign_in_lookup_uses_lower_not_casefold() -> None:
    """See test_email_canonicalisation_uses_lower_not_casefold: casefold turns
    Straße into strasse, which is someone else's mailbox."""
    seen: list[str] = []

    def _capture(tenant, email):
        seen.append(email)
        return

    with (
        patch.object(local_login, "_load_account", side_effect=_capture),
        patch.object(local_login, "_record_noop"),
    ):
        local_login.authenticate("default", " Straße@Example.COM ", "x", ip="10.0.0.1")
    assert seen == ["straße@example.com"]


def test_the_audit_subject_uses_lower_and_keeps_distinct_addresses_distinct() -> None:
    assert local_login.audit_subject("Straße@Example.COM") == local_login.audit_subject(
        "straße@example.com"
    )
    assert local_login.audit_subject("Straße@example.com") != local_login.audit_subject(
        "strasse@example.com"
    )


def test_the_audit_subject_is_an_hmac_under_a_derived_key() -> None:
    """The address is the MESSAGE and the derived key is the KEY. Feeding both
    into HKDF's input keying material instead makes the "key" just more salt —
    the construction is only as strong as an unkeyed hash of a low-entropy,
    enumerable input, i.e. reversible by anyone with a list of addresses."""
    import hashlib
    import hmac as hmac_mod

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    from robothor.auth.tokens import signing_key

    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"genus-audit-email").derive(
        signing_key().encode("utf-8")
    )
    expected = hmac_mod.new(key, b"alice@example.com", hashlib.sha256).hexdigest()[:32]
    assert local_login.audit_subject("Alice@Example.com") == expected


def test_a_replayed_step_lost_at_the_database_refuses_the_sign_in() -> None:
    """_record_success claims the step in the same statement that clears the
    failure state. Losing that claim means another request spent this code a
    moment ago — a correct password and a correct code, and still a replay."""
    secret = totp.new_secret()
    row = _mfa_account(secret)
    with (
        patch.object(local_login, "_load_account", return_value=row),
        patch.object(local_login, "_record_failure") as fail,
        patch.object(local_login, "_record_noop"),
        patch.object(local_login, "_record_success", return_value=False) as ok,
        patch("robothor.auth.accounts.issue_for_account") as issue,
    ):
        result = local_login.authenticate(
            "default", "alice@example.com", GOOD_PASSWORD, totp.generate(secret), ip="10.0.0.1"
        )
    assert result.ok is False and result.mfa_required is True
    ok.assert_called_once()
    issue.assert_not_called()
    fail.assert_called_once()


def test_confirming_an_enrollment_enables_and_claims_in_one_transaction() -> None:
    secret = totp.new_secret()
    row = account(
        mfa_enabled=False, mfa_secret_enc=mfa_secrets.encrypt_secret(secret, user_id=USER_ID)
    )
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_confirm_enrollment_atomically", return_value=True) as once,
        patch.object(local_login, "_set_mfa_enabled") as separate_enable,
        patch.object(local_login, "_record_mfa_step") as separate_burn,
    ):
        assert local_login.confirm_enrollment(USER_ID, totp.generate(secret)) is True
    once.assert_called_once()
    separate_enable.assert_not_called()
    separate_burn.assert_not_called()


def test_a_lost_confirm_claim_does_not_enable_the_factor() -> None:
    secret = totp.new_secret()
    row = account(
        mfa_enabled=False, mfa_secret_enc=mfa_secrets.encrypt_secret(secret, user_id=USER_ID)
    )
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_confirm_enrollment_atomically", return_value=False),
    ):
        assert local_login.confirm_enrollment(USER_ID, totp.generate(secret)) is False


def test_disabling_mfa_claims_the_step_before_stripping_the_factor() -> None:
    secret = totp.new_secret()
    row = _mfa_account(secret)
    with (
        patch.object(local_login, "_load_account_by_id", return_value=row),
        patch.object(local_login, "_record_mfa_step", return_value=False),
        patch.object(local_login, "_clear_mfa") as clear,
    ):
        assert local_login.disable_mfa(USER_ID, GOOD_PASSWORD, totp.generate(secret)) is False
    clear.assert_not_called()
