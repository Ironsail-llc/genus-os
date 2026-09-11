"""Auth endpoints — the bridge is the session-token authority.

Flow: the dashboard server authenticates the user with the org IdP (Auth.js,
OIDC/SAML), then calls ``POST /api/auth/sso`` with the VERIFIED claims plus a
shared dashboard↔bridge secret. The bridge JIT-provisions the account and mints
the access + refresh tokens. The dashboard stores them in httpOnly cookies and
forwards the access token as ``Authorization: Bearer`` on proxied calls; the
``AuthMiddleware`` verifies it. The bridge stays IdP-agnostic (it only verifies
its own JWT).
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from robothor.auth import accounts, tokens
from robothor.auth.deps import get_current_user
from robothor.auth.tokens import REFRESH_TTL_SECONDS
from robothor.constants import DEFAULT_TENANT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# The NAME of the environment variable — never its value. It is deliberately
# not interpolated into any log line or response body, and every message below
# spells it out as a literal instead.
#
# CodeQL's py/clear-text-logging-sensitive-data classifies an identifier
# containing "SECRET" as sensitive by NAME, and failed PR #483 at HIGH severity
# for passing this constant to logger.error — even though what it holds is the
# spelling of the variable, and the branch is reachable only when the value is
# empty. Arguing a false positive with a scanner is a recurring cost; a logger
# that can only ever be handed literals ends the argument permanently, and
# test_the_alarm_message_is_a_literal_with_no_arguments keeps it that way.
_SSO_SECRET_VAR = "GENUS_BRIDGE_SSO_SECRET"

# Raised at most once per outage rather than once per request: a login storm
# against a secretless bridge would otherwise bury the line that explains it.
_sso_secret_alarm_raised = False


def reset_sso_secret_alarm() -> None:
    """Re-arm the once-only alarm. For tests; production never calls it."""
    global _sso_secret_alarm_raised
    _sso_secret_alarm_raised = False


def _sso_secret_is_configured() -> bool:
    """Whether the shared secret is set and non-empty — and nothing else.

    The single place the value is read for a presence test. It returns a bool,
    so the secret itself has no path out of this function: no caller, no
    format string and no log record can reach it.
    """
    return bool(os.environ.get(_SSO_SECRET_VAR))


def sso_secret_present() -> bool:
    """Is the dashboard↔bridge shared secret configured?

    Logs once, at ERROR, the first time it is found missing. The refusal in
    ``_sso_secret_ok`` was already correct — fail-closed — but silent: on
    2026-09-03 the bridge started twelve seconds after a reboot, before the
    engine's ``ExecStartPre`` had decrypted ``/run/robothor/secrets.env``, and
    its ``EnvironmentFile=-`` is optional. It then refused every SSO exchange
    for eight days with nothing in the journal, /health or /ready to say why.
    """
    global _sso_secret_alarm_raised
    if _sso_secret_is_configured():
        # Re-arm, so a secret that is later lost is reported again.
        _sso_secret_alarm_raised = False
        return True
    if not _sso_secret_alarm_raised:
        _sso_secret_alarm_raised = True
        # One literal, no arguments. See the note on _SSO_SECRET_VAR above.
        logger.error(
            "GENUS_BRIDGE_SSO_SECRET is not set: every SSO exchange will be refused "
            "with 403 and no user can sign in. It is decrypted into "
            "/run/robothor/secrets.env — check that robothor-secrets.service ran "
            "before this process started."
        )
    return False


def sso_secret_readiness_check(*, bind_host: str | None = None) -> str:
    """Readiness contract string for the shared secret ("ok" / "error:...").

    Gated on ``auth_required``: a loopback development bridge legitimately runs
    with no shared secret and never performs an SSO exchange, and marking it
    not-ready forever is not a warning but an outage — under Helm the readiness
    probe removes the pod from its Service, so a check meant to expose a broken
    login would take down a deployment that never had one. The contract has
    only "ok" and "error:…", so "not applicable" has to read as ok; the log
    line above still fires, and the 403 refusal is unchanged either way.

    A raise from ``auth_required`` (an unsafe dev-mode combination) is treated
    as auth being required — fail closed, and let the check say so.
    """
    if sso_secret_present():
        return "ok"
    from robothor.auth.runtime import auth_required

    host = (
        bind_host if bind_host is not None else os.environ.get("ROBOTHOR_BRIDGE_HOST", "127.0.0.1")
    )
    try:
        required = auth_required(bind_host=host)
    except Exception:
        required = True
    if not required:
        return "ok"
    # A literal for the same reason the log line is one — this string is also
    # served in the /ready payload.
    return "error:GENUS_BRIDGE_SSO_SECRET-not-set"


class SsoExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(min_length=1, max_length=2048)
    subject: str = Field(min_length=1, max_length=2048)
    email: str = Field(min_length=3, max_length=320)
    email_verified: StrictBool
    display_name: str = ""


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


def _sso_secret_ok(provided: str | None) -> bool:
    """Constant-time comparison. The only place the value is read at all, and
    it goes straight into ``compare_digest`` — never into a message, a response
    or a log record."""
    if not sso_secret_present():
        return False
    if provided is None:
        return False
    return hmac.compare_digest(provided, os.environ[_SSO_SECRET_VAR])


def _oidc_issuer_allowed(issuer: str) -> bool:
    """Restrict production JIT provisioning to configured verified IdPs."""
    from robothor.auth.runtime import is_production

    if not issuer.strip():
        return False
    configured = {
        item.strip().rstrip("/")
        for item in os.environ.get("GENUS_OIDC_ISSUERS", "").split(",")
        if item.strip()
    }
    if not configured:
        return not is_production()
    return issuer.rstrip("/") in configured


def _issue_for_account(
    account: dict[str, Any], *, user_agent: str | None, ip: str | None
) -> dict[str, Any]:
    if account.get("status") != "active":
        raise accounts.AccountInactiveError("account is not active")
    access = tokens.issue_access_token(str(account["id"]), account["tenant_id"], account["role"])
    raw_refresh, refresh_hash = tokens.new_refresh_token()
    accounts.create_session(
        str(account["id"]),
        refresh_hash,
        ttl_seconds=REFRESH_TTL_SECONDS,
        user_agent=user_agent,
        ip=ip,
    )
    return {
        "access_token": access,
        "refresh_token": raw_refresh,
        "user": {
            "id": str(account["id"]),
            "email": str(account["email"]),
            "display_name": account["display_name"],
            "role": account["role"],
            "tenant_id": account["tenant_id"],
        },
    }


@router.post("/sso", response_model=None)
def sso_exchange(
    body: SsoExchangeRequest,
    request: Request,
    x_bridge_auth: str | None = Header(None, alias="X-Bridge-Auth"),
) -> dict[str, Any] | JSONResponse:
    """Exchange verified IdP claims (from the dashboard) for bridge tokens."""
    if not _sso_secret_ok(x_bridge_auth):
        return JSONResponse({"error": "sso exchange not authorized"}, status_code=403)
    if not _oidc_issuer_allowed(body.issuer):
        return JSONResponse({"error": "identity provider not authorized"}, status_code=403)
    if body.email_verified is not True:
        return JSONResponse({"error": "verified email required"}, status_code=403)
    try:
        account = accounts.jit_provision(
            issuer=body.issuer.strip(),
            subject=body.subject.strip(),
            email=body.email,
            display_name=body.display_name,
            # Tenant selection is operator-controlled through
            # ROBOTHOR_DEFAULT_TENANT, never accepted from the SSO caller.
            tenant_id=DEFAULT_TENANT,
        )
    except accounts.AccountProvisioningError:
        # Do not reveal whether an email maps to a privileged, invited, or
        # disabled local account.
        return JSONResponse({"error": "account not eligible for SSO"}, status_code=403)
    ip = request.client.host if request.client else None
    try:
        return _issue_for_account(account, user_agent=request.headers.get("user-agent"), ip=ip)
    except accounts.AccountInactiveError:
        return JSONResponse({"error": "account not eligible for SSO"}, status_code=403)


@router.post("/refresh", response_model=None)
def refresh(body: RefreshRequest, request: Request) -> dict[str, Any] | JSONResponse:
    """Rotate the refresh token and reissue an access token."""
    h = tokens.hash_refresh_token(body.refresh_token)
    session = accounts.consume_active_session(h)
    if not session:
        return JSONResponse({"error": "invalid or expired refresh token"}, status_code=401)
    account = accounts.get_account_by_id(str(session["user_id"]))
    if not account or account.get("status") != "active":
        return JSONResponse({"error": "account inactive"}, status_code=401)
    ip = request.client.host if request.client else None
    return _issue_for_account(account, user_agent=request.headers.get("user-agent"), ip=ip)


@router.post("/logout")
def logout(body: LogoutRequest) -> dict[str, bool]:
    revoked = accounts.revoke_session(tokens.hash_refresh_token(body.refresh_token))
    return {"success": True, "revoked": revoked}


@router.get("/me", response_model=None)
def me(request: Request) -> dict[str, Any] | JSONResponse:
    """Return the current verified user (or 401)."""
    try:
        ctx = get_current_user(request)
    except Exception:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    account = accounts.get_account_by_id(ctx.user_id)
    if not account or account.get("status") != "active":
        return JSONResponse({"error": "account not found"}, status_code=404)
    return {
        "id": ctx.user_id,
        "tenant_id": ctx.tenant_id,
        "role": ctx.role,
        "email": str(account["email"]),
        "display_name": account["display_name"],
    }
