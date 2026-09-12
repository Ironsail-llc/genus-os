"""Fail-closed runtime security mode."""

from __future__ import annotations

import pytest

from robothor.auth.runtime import (
    AuthConfigurationError,
    auth_required,
    insecure_dev_mode,
    is_loopback_host,
    validate_auth_configuration,
)


def test_authentication_is_required_by_default(monkeypatch):
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)
    assert auth_required() is True


def test_explicit_dev_mode_requires_loopback(monkeypatch):
    monkeypatch.setenv("GENUS_INSECURE_DEV_MODE", "true")
    monkeypatch.delenv("GENUS_ENVIRONMENT", raising=False)
    assert insecure_dev_mode(bind_host="127.0.0.1") is True
    assert auth_required(bind_host="localhost") is False
    with pytest.raises(AuthConfigurationError, match="loopback"):
        insecure_dev_mode(bind_host="0.0.0.0")


def test_production_rejects_insecure_mode(monkeypatch):
    monkeypatch.setenv("GENUS_ENVIRONMENT", "production")
    monkeypatch.setenv("GENUS_INSECURE_DEV_MODE", "true")
    with pytest.raises(AuthConfigurationError, match="forbidden"):
        validate_auth_configuration(bind_host="127.0.0.1")


def test_production_requires_sso_and_oidc_configuration(monkeypatch):
    monkeypatch.setenv("GENUS_ENVIRONMENT", "production")
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "x" * 32)
    monkeypatch.delenv("GENUS_BRIDGE_SSO_SECRET", raising=False)
    monkeypatch.delenv("GENUS_OIDC_ISSUERS", raising=False)
    with pytest.raises(AuthConfigurationError, match="SSO_SECRET"):
        validate_auth_configuration(bind_host="0.0.0.0")


def test_production_requires_operator_provisioned_signing_key(monkeypatch):
    monkeypatch.setenv("GENUS_ENVIRONMENT", "production")
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)
    monkeypatch.setenv("GENUS_BRIDGE_SSO_SECRET", "bridge-sso-secret")
    monkeypatch.setenv("GENUS_OIDC_ISSUERS", "https://idp.example.test")
    with pytest.raises(AuthConfigurationError, match="SIGNING_KEY"):
        validate_auth_configuration(bind_host="0.0.0.0")


def test_token_verifier_does_not_require_bridge_sso_configuration(monkeypatch):
    monkeypatch.setenv("GENUS_ENVIRONMENT", "production")
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "x" * 32)
    monkeypatch.delenv("GENUS_BRIDGE_SSO_SECRET", raising=False)
    monkeypatch.delenv("GENUS_OIDC_ISSUERS", raising=False)

    validate_auth_configuration(
        bind_host="0.0.0.0",
        require_sso_configuration=False,
    )


def test_token_verifier_still_requires_production_signing_key(monkeypatch):
    monkeypatch.setenv("GENUS_ENVIRONMENT", "production")
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)

    with pytest.raises(AuthConfigurationError, match="SIGNING_KEY"):
        validate_auth_configuration(
            bind_host="0.0.0.0",
            require_sso_configuration=False,
        )


@pytest.mark.parametrize("host", ["127.0.0.1", "127.9.8.7", "::1", "localhost"])
def test_loopback_hosts(host):
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.1", "bridge", "example.com"])
def test_non_loopback_hosts(host):
    assert is_loopback_host(host) is False


def test_local_login_satisfies_the_sign_in_provider_requirement(monkeypatch):
    """An appliance whose only sign-in method is local email+password is a
    configured deployment, not a misconfigured one — that is the whole point
    of shipping local login."""
    monkeypatch.setenv("GENUS_ENVIRONMENT", "production")
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "x" * 32)
    monkeypatch.setenv("GENUS_BRIDGE_SSO_SECRET", "bridge-sso-secret")
    monkeypatch.delenv("GENUS_OIDC_ISSUERS", raising=False)
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "true")

    validate_auth_configuration(bind_host="0.0.0.0")


def test_production_with_neither_provider_still_fails(monkeypatch):
    monkeypatch.setenv("GENUS_ENVIRONMENT", "production")
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "x" * 32)
    monkeypatch.setenv("GENUS_BRIDGE_SSO_SECRET", "bridge-sso-secret")
    monkeypatch.delenv("GENUS_OIDC_ISSUERS", raising=False)
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "false")
    with pytest.raises(AuthConfigurationError, match="OIDC_ISSUERS"):
        validate_auth_configuration(bind_host="0.0.0.0")


def test_local_login_does_not_excuse_the_bridge_shared_secret(monkeypatch):
    """Local login does not use the SSO exchange, but the dashboard still
    proxies every authenticated call through the bridge — relaxing this would
    be a weakening the feature does not need."""
    monkeypatch.setenv("GENUS_ENVIRONMENT", "production")
    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "x" * 32)
    monkeypatch.delenv("GENUS_BRIDGE_SSO_SECRET", raising=False)
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "true")
    with pytest.raises(AuthConfigurationError, match="SSO_SECRET"):
        validate_auth_configuration(bind_host="0.0.0.0")
