"""Runtime security mode for Genus OS HTTP services.

Authentication is fail-closed by default.  The only supported compatibility
escape hatch is ``GENUS_INSECURE_DEV_MODE=true`` while a service is bound to a
loopback address and the runtime is not explicitly marked as production.

``GENUS_AUTH_ENFORCE`` is retained as a one-way compatibility switch: setting
it to a truthy value forces authentication on, but setting it to false no
longer disables authentication.  This prevents a stale production variable
from silently reopening the legacy trusted-header path.
"""

from __future__ import annotations

import ipaddress
import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})


class AuthConfigurationError(RuntimeError):
    """The requested authentication mode is unsafe for the runtime."""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def runtime_environment() -> str:
    """Return the explicitly configured runtime environment, if any."""
    return (
        (os.environ.get("GENUS_ENVIRONMENT") or os.environ.get("ROBOTHOR_ENVIRONMENT") or "")
        .strip()
        .lower()
    )


def is_production() -> bool:
    return runtime_environment() in _PRODUCTION_ENVIRONMENTS


def is_loopback_host(host: str) -> bool:
    """Return whether *host* is an unambiguous loopback bind target."""
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def insecure_dev_mode(*, bind_host: str = "127.0.0.1") -> bool:
    """Validate and return the explicit insecure-development setting.

    Merely naming an environment ``development`` is not sufficient.  The
    unsafe mode must be opted into and cannot be combined with a non-loopback
    bind or an explicitly production runtime.
    """
    if not _truthy(os.environ.get("GENUS_INSECURE_DEV_MODE")):
        return False
    if is_production():
        raise AuthConfigurationError(
            "GENUS_INSECURE_DEV_MODE is forbidden in a production environment"
        )
    if not is_loopback_host(bind_host):
        raise AuthConfigurationError(
            "GENUS_INSECURE_DEV_MODE requires a loopback service bind address"
        )
    return True


def auth_required(*, bind_host: str = "127.0.0.1") -> bool:
    """Return whether verified authentication is required for private routes."""
    if _truthy(os.environ.get("GENUS_AUTH_ENFORCE")):
        return True
    return not insecure_dev_mode(bind_host=bind_host)


def legacy_headers_allowed(*, bind_host: str = "127.0.0.1") -> bool:
    """Return whether unverified identity headers may be used."""
    return not auth_required(bind_host=bind_host)


def validate_auth_configuration(
    *,
    bind_host: str = "127.0.0.1",
    require_sso_configuration: bool = True,
) -> None:
    """Raise when the selected runtime authentication configuration is unsafe.

    The Bridge is the SSO exchange authority, so callers retain the default
    requirement for its shared secret and OIDC issuer allowlist. Other token-
    verifying services, such as the Engine, set ``require_sso_configuration``
    to false and validate only the common authentication mode and signing key.
    """
    # Evaluating the mode performs the production and bind validation.
    auth_required(bind_host=bind_host)

    configured_key = os.environ.get("GENUS_AUTH_SIGNING_KEY")
    if configured_key is not None and len(configured_key.encode("utf-8")) < 32:
        raise AuthConfigurationError("GENUS_AUTH_SIGNING_KEY must contain at least 32 bytes")

    if is_production():
        if not configured_key:
            raise AuthConfigurationError("GENUS_AUTH_SIGNING_KEY is required in production")
        if require_sso_configuration:
            if not os.environ.get("GENUS_BRIDGE_SSO_SECRET"):
                raise AuthConfigurationError("GENUS_BRIDGE_SSO_SECRET is required in production")
            if not os.environ.get("GENUS_OIDC_ISSUERS"):
                raise AuthConfigurationError("GENUS_OIDC_ISSUERS is required in production")
