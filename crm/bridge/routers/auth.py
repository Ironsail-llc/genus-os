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
import os
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from robothor.auth import accounts, tokens
from robothor.auth.deps import get_current_user
from robothor.auth.tokens import REFRESH_TTL_SECONDS
from robothor.constants import DEFAULT_TENANT

router = APIRouter(prefix="/api/auth", tags=["auth"])


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
    secret = os.environ.get("GENUS_BRIDGE_SSO_SECRET")
    if not secret or provided is None:
        return False
    return hmac.compare_digest(provided, secret)


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
