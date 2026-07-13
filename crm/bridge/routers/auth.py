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

import os

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from robothor.auth import accounts, tokens
from robothor.auth.deps import get_current_user
from robothor.auth.tokens import REFRESH_TTL_SECONDS
from robothor.constants import DEFAULT_TENANT

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SsoExchangeRequest(BaseModel):
    issuer: str
    subject: str
    email: str
    display_name: str = ""
    tenant_id: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


def _sso_secret_ok(provided: str | None) -> bool:
    secret = os.environ.get("GENUS_BRIDGE_SSO_SECRET")
    return bool(secret) and provided == secret


def _issue_for_account(account: dict, *, user_agent: str | None, ip: str | None) -> dict:
    access = tokens.issue_access_token(str(account["id"]), account["tenant_id"], account["role"])
    raw_refresh, refresh_hash = tokens.new_refresh_token()
    accounts.create_session(
        str(account["id"]), refresh_hash, ttl_seconds=REFRESH_TTL_SECONDS, user_agent=user_agent, ip=ip
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


@router.post("/sso")
async def sso_exchange(
    body: SsoExchangeRequest,
    request: Request,
    x_bridge_auth: str | None = Header(None, alias="X-Bridge-Auth"),
):
    """Exchange verified IdP claims (from the dashboard) for bridge tokens."""
    if not _sso_secret_ok(x_bridge_auth):
        return JSONResponse({"error": "sso exchange not authorized"}, status_code=403)
    account = accounts.jit_provision(
        issuer=body.issuer,
        subject=body.subject,
        email=body.email,
        display_name=body.display_name,
        tenant_id=body.tenant_id or DEFAULT_TENANT,
    )
    ip = request.client.host if request.client else None
    return _issue_for_account(account, user_agent=request.headers.get("user-agent"), ip=ip)


@router.post("/refresh")
async def refresh(body: RefreshRequest, request: Request):
    """Rotate the refresh token and reissue an access token."""
    h = tokens.hash_refresh_token(body.refresh_token)
    session = accounts.get_active_session(h)
    if not session:
        return JSONResponse({"error": "invalid or expired refresh token"}, status_code=401)
    account = accounts.get_account_by_id(str(session["user_id"]))
    if not account or account.get("status") != "active":
        return JSONResponse({"error": "account inactive"}, status_code=401)
    accounts.revoke_session(h)  # rotate — old refresh is single-use
    ip = request.client.host if request.client else None
    return _issue_for_account(account, user_agent=request.headers.get("user-agent"), ip=ip)


@router.post("/logout")
async def logout(body: LogoutRequest):
    revoked = accounts.revoke_session(tokens.hash_refresh_token(body.refresh_token))
    return {"success": True, "revoked": revoked}


@router.get("/me")
async def me(request: Request):
    """Return the current verified user (or 401)."""
    try:
        ctx = get_current_user(request)
    except Exception:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    account = accounts.get_account_by_id(ctx.user_id)
    if not account:
        return JSONResponse({"error": "account not found"}, status_code=404)
    return {
        "id": ctx.user_id,
        "tenant_id": ctx.tenant_id,
        "role": ctx.role,
        "email": str(account["email"]),
        "display_name": account["display_name"],
    }
