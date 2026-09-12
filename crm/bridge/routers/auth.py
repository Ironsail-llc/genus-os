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
import json
import logging
import os
from typing import TYPE_CHECKING, Annotated, Any, TypeAlias

from robothor.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from robothor.auth import accounts, local_login, tokens
from robothor.auth.deps import get_current_user
from robothor.constants import DEFAULT_TENANT

from ._audit import audited

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
    """Thin alias for the shared issuance point.

    The body moved to ``robothor.auth.accounts.issue_for_account`` when local
    email+password login arrived, so that both sign-in paths mint a session
    through exactly one implementation. The name stays here because the SSO
    routes below read as they always did.
    """
    return accounts.issue_for_account(account, user_agent=user_agent, ip=ip)


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
    # Field-by-field, never `**account`: the row also carries password_hash and
    # mfa_secret_enc, and a spread would serve both to the browser.
    return {
        "id": ctx.user_id,
        "tenant_id": ctx.tenant_id,
        "role": ctx.role,
        "email": str(account["email"]),
        "display_name": account["display_name"],
        "mfa_enabled": bool(account.get("mfa_enabled")),
        "mfa_setup_required": local_login.mfa_setup_required_for(account),
    }


# ── Local email + password sign-in ───────────────────────────────────
#
# Off unless GENUS_LOCAL_LOGIN=true, in which case /methods reports it and
# /login is a PUBLIC route (see AuthMiddleware._PUBLIC_PATHS). Everything
# these handlers are allowed to say about a failure is decided in
# robothor/auth/local_login.py — this layer only maps a LoginResult onto a
# status code, and must never add a detail of its own.


_MAX_CODE_LENGTH = 16
_MAX_EMAIL_LENGTH = 320
# tokens.new_refresh_token() is token_urlsafe(48) — 64 characters. 512 leaves
# generous room for a future format without leaving the field unbounded.
_MAX_TOKEN_LENGTH = 512

_NOT_FOUND = JSONResponse({"error": "Not found"}, status_code=404)
_UNAUTHORIZED = JSONResponse({"error": local_login.GENERIC_FAILURE}, status_code=401)
_THROTTLED = JSONResponse({"error": "too many attempts"}, status_code=429)
_BAD_REQUEST = JSONResponse({"error": "invalid request"}, status_code=422)
# Fixed string, like every other refusal here: a size complaint must not quote
# the body it is complaining about.
_TOO_LARGE = JSONResponse({"error": "request too large"}, status_code=413)
_SERVICE_REFUSED = JSONResponse(
    {"error": "service credentials cannot manage human credentials"}, status_code=403
)
# Names the policy, never the submitted value.
_WEAK_PASSWORD = JSONResponse(
    {"error": f"password must be at least {local_login.MIN_PASSWORD_LENGTH} characters"},
    status_code=422,
)


class BodyTooLarge:
    """Sentinel: the request body exceeded ``MAX_CREDENTIAL_BODY_BYTES``.

    A distinct value rather than ``None`` so the handlers can answer 413 instead
    of folding an oversized body into the same 422 as a malformed one — and so
    the refusal can be made before the bytes are deserialised at all.
    """

    __slots__ = ()


BODY_TOO_LARGE = BodyTooLarge()


async def _bounded_body(request: Request) -> bytes | None:
    """Read at most ``MAX_CREDENTIAL_BODY_BYTES``, or None if there is more.

    ``await request.json()`` buffers whatever arrives first and only then are
    the field ceilings applied, so a single large POST was a cheaper memory
    lever than argon2 — and a chunked request declares no ``Content-Length`` to
    refuse it by, so the read itself has to stop.
    """
    cap = local_login.MAX_CREDENTIAL_BODY_BYTES
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def credential_body(
    required: tuple[str, ...], optional: tuple[str, ...] = ()
) -> Callable[[Request], Awaitable[dict[str, str] | BodyTooLarge | None]]:
    """A dependency that parses a credential-bearing JSON body WITHOUT pydantic.

    FastAPI serializes pydantic's ``input`` field into its 422 response, and
    for a ``missing`` error that input is the ENTIRE request body. A
    ``LoginRequest`` model answered ``{"password": "hunter2"}`` (email omitted)
    with the password quoted back in the response — into the browser, the
    access log, and every proxy between them. A ``max_length`` on the password
    leaked it the same way, verbatim.

    So the credential routes validate by hand and answer with a fixed string;
    the only thing a rejection tells a caller is that the body was not
    acceptable. It is a *dependency* rather than a call inside the handler so
    the handlers can stay ``def``: FastAPI runs those in its worker threadpool,
    which is where argon2 and psycopg2 belong — see
    ``test_only_genuinely_async_routes_run_on_the_event_loop``.

    Returns ``None`` for anything malformed and ``BODY_TOO_LARGE`` for a body
    over the cap; the caller turns those into its own no-echo refusals.
    """

    allowed = set(required) | set(optional)

    async def parse(request: Request) -> dict[str, str] | BodyTooLarge | None:
        declared = request.headers.get("content-length")
        if declared is not None:
            # Cheapest possible refusal: from the header, before a byte of the
            # body is read or parsed.
            try:
                if int(declared) > local_login.MAX_CREDENTIAL_BODY_BYTES:
                    return BODY_TOO_LARGE
            except ValueError:
                return None
        body = await _bounded_body(request)
        if body is None:
            return BODY_TOO_LARGE
        try:
            raw = json.loads(body)
        except Exception:
            return None
        if not isinstance(raw, dict) or set(raw) - allowed:
            return None  # extra="forbid", by hand
        parsed: dict[str, str] = {}
        for name in required:
            value = raw.get(name)
            if not isinstance(value, str) or not value:
                return None
            parsed[name] = value
        for name in optional:
            value = raw.get(name)
            if value is None:
                continue
            if not isinstance(value, str):
                return None
            parsed[name] = value
        return parsed

    return parse


def _credential_lengths_ok(parsed: dict[str, str]) -> bool:
    """Bound every field before anything reaches argon2 or the database.

    An unauthenticated caller must not be able to hand argon2 a megabyte to
    hash; a login must still not reveal the *minimum* length, so only the
    ceiling is enforced here and the floor belongs to the password-change
    route and ``local_login.validate_password``.
    """
    for name, value in parsed.items():
        if name == "email" and (len(value) > _MAX_EMAIL_LENGTH or not _email_shape_ok(value)):
            return False
        if name == "code" and len(value) > _MAX_CODE_LENGTH:
            return False
        if "password" in name and len(value) > local_login.MAX_PASSWORD_LENGTH:
            return False
        # A refresh token is a fixed-width random string. Leaving the one field
        # here unbounded would hand a caller who has a session and nothing else
        # a free megabyte of SHA-256 per request.
        if name == "keep_refresh_token" and len(value) > _MAX_TOKEN_LENGTH:
            return False
    return True


def _email_shape_ok(value: str) -> bool:
    """The cheapest check that a string could be an address at all.

    Deliberately NOT a validating regex — RFC 5322 is not something to
    re-implement at a login endpoint, and a stricter rule here would start
    refusing addresses the IdP happily issues. This only rejects strings that
    cannot be an address under any reading, so an argon2 hash is never spent
    on ``"alice"`` or on a probe that is really a payload.
    """
    local_part, separator, domain = value.partition("@")
    if not separator or "@" in domain:
        return False
    if not local_part.strip() or not domain.strip():
        return False
    return not any(ch.isspace() for ch in value)


def _local_login_off() -> bool:
    return not local_login.local_login_enabled()


_CLIENT_IP_HEADER = "X-Client-IP"


def _peer_ip(request: Request) -> str | None:
    """The address the TCP connection actually came from."""
    return request.client.host if request.client else None


def _trusted_proxies() -> set[str]:
    return {item.strip() for item in get_settings().auth.trusted_proxies.split(",") if item.strip()}


def _peer_is_trusted(peer: str | None) -> bool:
    """Whether *peer* may speak for someone else's address.

    ONLY peers named in ``GENUS_TRUSTED_PROXIES`` may — as an address or a CIDR
    range (a Kubernetes pod address changes on every restart, so a range is the
    only usable way to say "the dashboard" there). An empty variable trusts
    nobody, which is the safe default for a deployment that has not thought
    about it.

    Loopback is NOT trusted implicitly, and that is the whole point of this
    function. A tunnel on the same host — cloudflared, a reverse proxy, an SSH
    forward — makes every remote client a loopback peer. Trusting loopback by
    default would therefore let every one of them choose its own rate-limit
    bucket by sending a header, deleting the limiter entirely on the strength
    of a deployment detail nobody wrote down. An operator who wants it says so:
    ``GENUS_TRUSTED_PROXIES=127.0.0.1/32``.

    A malformed entry is skipped, not fatal: a typo in an environment variable
    must not turn every sign-in into a 500.
    """
    if not peer:
        return False
    import ipaddress

    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for entry in _trusted_proxies():
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            logger.warning("GENUS_TRUSTED_PROXIES contains an entry that is not an address or CIDR")
    return False


def _client_ip(request: Request) -> str | None:
    """The END USER's address, for the rate limiter and the audit trail.

    Without this the limiter's IP dimension was inert. The dashboard calls the
    bridge SERVER-side, so ``request.client.host`` is the dashboard pod for
    every sign-in on the planet: one bucket for the whole internet. That is
    worse than no dimension at all — five attempts from anywhere lock every
    user of that email out for a minute, and a distributed spray is not slowed
    down by a shared bucket it can refill from any source.

    ``X-Client-IP`` is honoured ONLY from a peer listed in
    ``GENUS_TRUSTED_PROXIES`` — loopback included, which must be named
    explicitly. A header anyone can set is a limiter anyone can bypass by
    varying it, so the untrusted case falls back to the real peer address, and
    so does a value that is not a parseable IP.
    """
    peer = _peer_ip(request)
    if not _peer_is_trusted(peer):
        return peer
    forwarded = (request.headers.get(_CLIENT_IP_HEADER) or "").strip()
    if not forwarded:
        return peer
    import ipaddress

    try:
        ipaddress.ip_address(forwarded)
    except ValueError:
        return peer
    return forwarded


def _oidc_issuers() -> list[str]:
    return [
        item.strip() for item in os.environ.get("GENUS_OIDC_ISSUERS", "").split(",") if item.strip()
    ]


@router.get("/methods", response_model=None)
def auth_methods() -> dict[str, Any]:
    """Which sign-in methods this deployment offers.

    Public and unauthenticated by necessity — the sign-in page asks it what to
    render before anyone has a session. It answers with NAMES and booleans
    only: issuer URLs are already public metadata, and no secret (the bridge
    shared secret, the Cloudflare audience, a client secret) appears here.

    Not rate limited, unlike ``/login``. A limiter is there to make guessing
    expensive, and there is nothing here to guess: the handler reads three
    environment variables, touches no database and performs no crypto, so it is
    no more of a lever than ``/ready`` — which is already public. Throttling it
    would instead risk hiding the sign-in form from a whole office behind one
    NAT address.
    """
    return {
        "local": local_login.local_login_enabled(),
        "oidc": _oidc_issuers(),
        "cloudflare_access": bool(
            get_settings().auth.cf_access_team_domain.strip()
            and get_settings().auth.cf_access_aud.strip()
        ),
    }


_Body: TypeAlias = "dict[str, str] | BodyTooLarge | None"
_LoginBody = Annotated[_Body, Depends(credential_body(("email", "password"), ("code",)))]
_PasswordBody = Annotated[
    _Body,
    Depends(
        credential_body(("current_password", "new_password"), ("keep_refresh_token",)),
    ),
]
_MfaCodeBody = Annotated[_Body, Depends(credential_body(("code",)))]
_MfaDisableBody = Annotated[_Body, Depends(credential_body(("password", "code")))]
_MfaEnrollBody = Annotated[_Body, Depends(credential_body(("password",)))]


@router.post("/login", response_model=None)
def local_login_route(body: _LoginBody, request: Request) -> dict[str, Any] | JSONResponse:
    """Exchange an email + password (+ TOTP code) for bridge tokens."""
    if _local_login_off():
        return _NOT_FOUND
    # The aggregate ceiling, keyed on the TCP peer and on this process — the
    # only two quantities on this route that a caller cannot choose. Ahead of
    # the body, the per-(email, IP) window, the account load and argon2.
    if local_login.flood_limited(_peer_ip(request)):
        return _THROTTLED
    if isinstance(body, BodyTooLarge):
        return _TOO_LARGE
    if body is None or not _credential_lengths_ok(body):
        return _BAD_REQUEST
    ip = _client_ip(request)
    result = local_login.authenticate(
        DEFAULT_TENANT,
        body["email"],
        body["password"],
        body.get("code"),
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    # A failed sign-in needs a SUBJECT or the audit trail cannot answer "which
    # account was sprayed, from where" — the first question an incident asks.
    # It is a keyed hash, not the address: this log is exported to a SIEM, and
    # on a FAILURE the address may be an attacker's guess rather than a user of
    # this instance. Equal addresses hash equally, so a spray is still one
    # visible pattern. See local_login.audit_subject.
    subject = local_login.audit_subject(body["email"])
    if result.rate_limited:
        audited(
            request,
            "auth.login",
            action="local",
            status="denied",
            reason="rate_limited",
            subject=subject,
            ip=ip,
        )
        return _THROTTLED
    if result.mfa_required:
        audited(
            request,
            "auth.login",
            action="local",
            status="denied",
            reason="mfa_required",
            subject=subject,
            ip=ip,
        )
        return JSONResponse({"error": local_login.MFA_REQUIRED}, status_code=401)
    if not result.ok or not result.tokens:
        # One message for every reason. The audit trail records that a local
        # sign-in failed; it does not record which of the reasons it was,
        # because the reason is derived from the submitted credential.
        audited(request, "auth.login", action="local", status="denied", subject=subject, ip=ip)
        return _UNAUTHORIZED
    audited(
        request,
        "auth.login",
        action="local",
        user_id=result.tokens["user"]["id"],
        mfa_setup_required=result.mfa_setup_required,
        subject=subject,
        ip=ip,
    )
    return {**result.tokens, "mfa_setup_required": result.mfa_setup_required}


def _caller_is_service(request: Request) -> bool:
    """Whether this request carries a verified SERVICE (agent) token."""
    try:
        return bool(getattr(get_current_user(request), "is_service", False))
    except Exception:
        return False


def _verified_caller(request: Request) -> Any | None:
    """The verified human behind this request, or None.

    A *service* token is treated as no caller at all on these routes. It has no
    password and no authenticator, so there is nothing here it could legitimately
    manage — and the middleware deliberately exempts ``/api/auth/*`` from its
    scope checks, which leaves the agent-capability manifest as the only other
    thing in the way. That manifest is instance-owned and its ``default_policy``
    may be ``allow``, so an appliance could ship an agent able to POST
    /api/auth/password. Refusing here does not depend on anyone's configuration.
    """
    try:
        ctx = get_current_user(request)
    except Exception:
        return None
    if getattr(ctx, "is_service", False):
        return None
    return ctx


@router.post("/password", response_model=None)
def change_password_route(body: _PasswordBody, request: Request) -> dict[str, Any] | JSONResponse:
    """Change the caller's own password. Requires the current one."""
    if _local_login_off():
        return _NOT_FOUND
    # The aggregate ceiling, keyed on the TCP peer and on this process — the
    # only two quantities on this route that a caller cannot choose. Ahead of
    # the body, the per-(email, IP) window, the account load and argon2.
    if local_login.flood_limited(_peer_ip(request)):
        return _THROTTLED
    if _caller_is_service(request):
        return _SERVICE_REFUSED
    ctx = _verified_caller(request)
    if ctx is None:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    if isinstance(body, BodyTooLarge):
        return _TOO_LARGE
    if body is None or not _credential_lengths_ok(body):
        return _BAD_REQUEST
    if len(body["new_password"]) < local_login.MIN_PASSWORD_LENGTH:
        # Checked here as well as in local_login.validate_password: this is the
        # one route where the minimum is a thing the caller is entitled to be
        # told, and a too-short password must never reach argon2 at all.
        return _WEAK_PASSWORD
    # Throttled on the identity, not the body: a stolen session must not get
    # unlimited guesses at the current password either.
    if local_login.throttled(f"password:{ctx.user_id}", _client_ip(request)):
        return _THROTTLED
    # The caller's own session is spared so the operator is not thrown out of
    # the panel mid-change. The dashboard's server-only route supplies the raw
    # refresh token; only its hash is ever compared, and neither value leaves
    # this function.
    raw_keep = body.get("keep_refresh_token")
    keep_hash = tokens.hash_refresh_token(raw_keep) if raw_keep else None
    try:
        changed = local_login.change_password(
            ctx.user_id,
            body["current_password"],
            body["new_password"],
            keep_refresh_hash=keep_hash,
        )
    except local_login.WeakPasswordError:
        return _WEAK_PASSWORD
    audited(
        request,
        "auth.password_change",
        action="local",
        status="ok" if changed else "denied",
        user_id=ctx.user_id,
    )
    return {"success": True} if changed else _UNAUTHORIZED


@router.post("/mfa/enroll", response_model=None)
def mfa_enroll_route(body: _MfaEnrollBody, request: Request) -> dict[str, Any] | JSONResponse:
    """Provision a pending TOTP secret for the caller.

    Takes the account's CURRENT PASSWORD, and is throttled like the other
    code-bearing routes. Binding a second factor is a change of authority: a
    stolen session alone must not be enough to enrol the attacker's own
    authenticator, after which the real owner's password no longer gets them
    back in. Taking a password also makes this a guessing oracle, which is why
    it is rate limited rather than merely authenticated.

    This is the one and only response that ever contains the secret — it has
    to, because an authenticator app must receive it. The factor stays off
    until ``/mfa/confirm`` proves the app holds the same seed.
    """
    if _local_login_off():
        return _NOT_FOUND
    # The aggregate ceiling, keyed on the TCP peer and on this process — the
    # only two quantities on this route that a caller cannot choose. Ahead of
    # the body, the per-(email, IP) window, the account load and argon2.
    if local_login.flood_limited(_peer_ip(request)):
        return _THROTTLED
    if _caller_is_service(request):
        return _SERVICE_REFUSED
    ctx = _verified_caller(request)
    if ctx is None:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    if isinstance(body, BodyTooLarge):
        return _TOO_LARGE
    if body is None or not _credential_lengths_ok(body):
        return _BAD_REQUEST
    if local_login.throttled(f"mfa:{ctx.user_id}", _client_ip(request)):
        return _THROTTLED
    try:
        enrolled = local_login.begin_enrollment(ctx.user_id, body["password"])
    except local_login.MfaAlreadyEnabledError:
        audited(request, "auth.mfa_enroll", action="local", status="denied", user_id=ctx.user_id)
        return JSONResponse(
            {"error": "two-factor is already enabled; disable it first"}, status_code=409
        )
    except local_login.EnrollmentDeniedError:
        audited(request, "auth.mfa_enroll", action="local", status="denied", user_id=ctx.user_id)
        return _UNAUTHORIZED
    audited(request, "auth.mfa_enroll", action="local", user_id=ctx.user_id)
    return enrolled


@router.post("/mfa/confirm", response_model=None)
def mfa_confirm_route(body: _MfaCodeBody, request: Request) -> dict[str, Any] | JSONResponse:
    """Turn MFA on once the caller proves the pending secret arrived."""
    if _local_login_off():
        return _NOT_FOUND
    # The aggregate ceiling, keyed on the TCP peer and on this process — the
    # only two quantities on this route that a caller cannot choose. Ahead of
    # the body, the per-(email, IP) window, the account load and argon2.
    if local_login.flood_limited(_peer_ip(request)):
        return _THROTTLED
    if _caller_is_service(request):
        return _SERVICE_REFUSED
    ctx = _verified_caller(request)
    if ctx is None:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    if isinstance(body, BodyTooLarge):
        return _TOO_LARGE
    if body is None or not _credential_lengths_ok(body):
        return _BAD_REQUEST
    if local_login.throttled(f"mfa:{ctx.user_id}", _client_ip(request)):
        return _THROTTLED
    confirmed = local_login.confirm_enrollment(ctx.user_id, body["code"])
    audited(
        request,
        "auth.mfa_confirm",
        action="local",
        status="ok" if confirmed else "denied",
        user_id=ctx.user_id,
    )
    return {"success": True} if confirmed else _UNAUTHORIZED


@router.post("/mfa/disable", response_model=None)
def mfa_disable_route(body: _MfaDisableBody, request: Request) -> dict[str, Any] | JSONResponse:
    """Turn MFA off. Needs the password AND a live code, so a hijacked session
    on its own cannot strip the second factor."""
    if _local_login_off():
        return _NOT_FOUND
    # The aggregate ceiling, keyed on the TCP peer and on this process — the
    # only two quantities on this route that a caller cannot choose. Ahead of
    # the body, the per-(email, IP) window, the account load and argon2.
    if local_login.flood_limited(_peer_ip(request)):
        return _THROTTLED
    if _caller_is_service(request):
        return _SERVICE_REFUSED
    ctx = _verified_caller(request)
    if ctx is None:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    if isinstance(body, BodyTooLarge):
        return _TOO_LARGE
    if body is None or not _credential_lengths_ok(body):
        return _BAD_REQUEST
    if local_login.throttled(f"mfa:{ctx.user_id}", _client_ip(request)):
        return _THROTTLED
    disabled = local_login.disable_mfa(ctx.user_id, body["password"], body["code"])
    audited(
        request,
        "auth.mfa_disable",
        action="local",
        status="ok" if disabled else "denied",
        user_id=ctx.user_id,
    )
    return {"success": True} if disabled else _UNAUTHORIZED
