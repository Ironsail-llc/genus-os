"""First-run setup: the only routes that answer a caller with no account.

Everything else on this bridge requires a verified session. These cannot: on a
fresh install there is nobody to have one, no OIDC issuer, no Cloudflare Access
and no password — which is exactly the state that used to leave an operator
looking at a sign-in page with no buttons on it, reaching for
``GENUS_INSECURE_DEV_MODE``.

So this router is a narrow, temporary exception, and three walls make it safe.

**It exists only during first run** — and that is TWO questions, not one,
because the wizard itself changes the answer to the first halfway through.

``status``, ``claim`` and ``operator`` gate on :func:`_gate`, which asks
``setup_complete()`` — an ``owner`` row in the DATABASE, never a file — and
answers 404 the moment there is one. Those three must not run twice: a claimed
appliance has no first run to start, no link to redeem and no second owner to
create, and it should not confirm that this API ever existed here.

Everything after the operator step gates on :func:`_ceremony_gate`, the
``setup_completed_at`` marker the last step writes. It has to: the operator
step CREATES the owner row, so gating those routes on the database made the
wizard kill itself at step 2 — provider, channel, agent and complete all 404'd
to a browser holding a live claim, and the marker was never written on any real
install. The weaker predicate is safe only because it is additive: every route
under it needs a claim, and a claim can only be minted by the route that IS
database-gated, spending the single-use token ``genus init`` printed.

**Two credentials, not one.** The token ``genus init`` printed buys a
five-minute claim token; the claim is what every other route requires. A
bridge session is NOT a claim (different ``typ``, different audience) and a
claim is not a session, and both directions are tested. The dashboard's BFF
proxy denies this prefix outright, so the browser's session can never be
attached to one of these calls even by accident.

**Nothing that goes in comes back out.** A provider key, a bot token and a
password enter here and appear in no response, no audit row and no log line.
The audit trail records fingerprints, ids and outcomes.

The real work is never re-implemented. Provider keys go through the vault key
naming and the same engine reload the Settings page uses; the owner account is
created by ``accounts.bootstrap_owner_account`` from the ``owner.yaml`` this
step writes, so the file and the row cannot name different tenants; agents are
installed by ``robothor.cli.agent.install_preset``, in-process; the doctor
strip is the same offline report ``GET /api/doctor`` serves.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path  # noqa: TC003 - a runtime return annotation
from time import monotonic
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from robothor import setup_token
from robothor.auth import local_login, tokens
from robothor.auth.tokens import TokenError
from robothor.cli.agent import install_preset
from robothor.setup_token import setup_complete
from routers._audit import audited
from routers.auth import BodyTooLarge, credential_body

logger = logging.getLogger(__name__)

router = APIRouter(tags=["setup"])

# One message for every refusal on the claim route. "Wrong token", "expired",
# "already used" and "nobody ever ran init here" are four different facts about
# this box, and an unauthenticated caller is entitled to none of them.
#
# Built per call rather than held as module singletons: a Response is a mutable
# object, and handing the same instance to concurrent requests on the
# appliance's most-sprayed route is the kind of sharing that is harmless right
# up until something downstream sets a header on it.


def _claim_refused() -> JSONResponse:
    return JSONResponse({"error": "invalid or expired token"}, status_code=401)


def _throttled() -> JSONResponse:
    return JSONResponse({"error": "too many attempts"}, status_code=429)


def _bad_request() -> JSONResponse:
    return JSONResponse({"error": "invalid request"}, status_code=400)


#: The limiter bucket for claim attempts. Namespaced so it can never share a
#: window with a sign-in for the same address.
_CLAIM_BUCKET = "setup:claim"

#: Deliberately loose — an address is validated by the mail server, not here.
#: This only stops a value that is obviously not one from becoming the identity
#: of the appliance's only owner account.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

#: Serialises the operator step. The claim token is single-issue and the setup
#: token behind it is single-use, so two concurrent creations need a race to
#: happen at all — but the act creates the ONE account that owns the appliance,
#: and "unlikely" is not a control. One process, one bridge; a lock is enough.
#:
#: A THREADING lock, not an ``asyncio`` one, and held INSIDE the worker thread
#: rather than across an await. An ``asyncio.Lock`` binds to the first event
#: loop that has to wait on it and raises on any other, so a module-level one
#: is wrong for any host that serves requests from more than one loop — which
#: the test client does, one per request, and which would surface only under
#: the contention the lock exists for. The thing being serialised is blocking
#: work on a worker thread, so the wait belongs there.
_OPERATOR_LOCK = threading.Lock()


def workspace() -> Path:
    """This installation's workspace. A seam the suite replaces."""
    from robothor.settings.sources import workspace_path

    path = workspace_path()
    if path is None:
        raise HTTPException(status_code=503, detail="no workspace is configured")
    return path


# ── the gate ─────────────────────────────────────────────────────────


def _gate() -> None:
    """404 ``status``, ``claim`` and ``operator`` once this instance is claimed.

    These three are the routes that must not run a SECOND time: an instance
    with an owner has no first-run to start, no link to redeem and no second
    owner to create. The predicate is the database — an owner row — so deleting
    a file cannot re-open them.

    404 rather than 403: a claimed appliance should not confirm that a first-run
    API ever existed at this path. ``setup_complete`` fails CLOSED on an
    unreachable database for the same reason.

    NOT used on the routes after the operator step. That was the original
    version and it killed the wizard at step 2: the operator step creates the
    owner row, so from the next request on, ``detect``, ``provider``,
    ``channel``, ``agent`` and ``complete`` all answered 404 to a browser
    holding a live claim, the provider step (which has no Skip) showed "that key
    did not work" forever, and ``setup_completed_at`` was never written on any
    real install. See :func:`_ceremony_gate`.
    """
    if setup_complete():
        raise HTTPException(status_code=404, detail="not found")


def _ceremony_gate() -> None:
    """404 the post-operator routes once the wizard has RECORDED that it
    finished.

    The distinction from :func:`_gate` is the whole of the C1 fix. "Has an
    owner" turns true halfway through the ceremony; "has recorded completion"
    turns true at the end of it, written by ``POST /api/setup/complete``.

    This is a weaker predicate on its own, and it is only safe because it is
    additive. Every route below requires a setup CLAIM, and a claim can only be
    minted by ``POST /api/setup/claim`` — which IS gated on the database, and
    which spends the single-use token ``genus init`` printed. So on an instance
    with an owner, nobody can obtain a new claim at all; the only claim that can
    reach these routes is one minted moments earlier by the operator running the
    wizard, and it expires five minutes later. Deleting ``config.yaml`` to clear
    the marker therefore re-opens nothing: there is no way to get the credential
    these routes require.
    """
    if setup_token.setup_recorded(workspace()):
        raise HTTPException(status_code=404, detail="not found")


def _claim_context(request: Request) -> dict[str, Any]:
    """Verify the ``Authorization: Bearer`` claim token, or 401.

    The middleware lets this prefix past without a session, so this is the only
    thing standing in front of the routes below. It accepts exactly one kind of
    credential: a token minted by :func:`claim_setup` with ``typ: "setup"`` and
    the ``genus-setup`` audience. An operator's own session token fails both
    checks, which is the requirement — these routes must never be reachable
    with a browser session, even the right person's.
    """
    from robothor.auth.deps import bearer_from_header

    token = bearer_from_header(request.headers.get("authorization"))
    if not token:
        raise HTTPException(status_code=401, detail="setup claim required")
    try:
        claims = tokens.decode_setup_claim_token(token)
    except TokenError:
        # Never the library's reason: it distinguishes expiry from a bad
        # signature, which is a probe an unauthenticated caller can run.
        raise HTTPException(status_code=401, detail="setup claim required") from None
    # Audited as "setup" rather than "anonymous": the acts below are real
    # changes to the appliance and the trail has to name the ceremony.
    request.state.actor_id = "setup"
    return claims


#: The database gate: "this instance has an owner, so first run is over".
#: ``status``, ``claim`` and ``operator`` only.
Gate = Annotated[None, Depends(_gate)]
#: The ceremony gate: "the wizard recorded that it finished". Everything after
#: the operator step, each of which also requires a claim.
Ceremony = Annotated[None, Depends(_ceremony_gate)]
Claim = Annotated["dict[str, Any]", Depends(_claim_context)]


# ── status ───────────────────────────────────────────────────────────


@router.get("/api/setup/status")
async def setup_status(request: Request, _gated: Gate) -> dict[str, Any]:
    """What the wizard still has to do. Public, and booleans only.

    Public because ``proxy.ts`` polls it before anyone holds anything, and
    because a browser that followed the printed link has to be told whether the
    wizard is still live. Booleans only for the same reason it is public: an
    email, a provider id or a hostname here would be a map of the appliance for
    whoever finds the port first.

    Once an owner exists this 404s with everything else, and the dashboard
    reads that as complete.
    """
    steps = await asyncio.to_thread(_step_state)
    return {"complete": False, "steps": steps}


#: How long a computed step state is reused. This route is PUBLIC and polled,
#: and answering it honestly costs vault reads (`provider_slots`) and a
#: directory glob — cheap per call, free amplification in bulk, on the one
#: unauthenticated surface a fresh box exposes. The dashboard memoises for 5 s
#: on its side, but the bridge port is reachable directly, so the ceiling
#: belongs here too. Two seconds is well inside a wizard step.
_STEP_STATE_TTL_S = 2.0
_step_state_cache: tuple[float, dict[str, bool]] | None = None
_step_state_lock = threading.Lock()


def reset_step_state_cache() -> None:
    """Forget the memoised step state. For tests."""
    global _step_state_cache  # noqa: PLW0603
    with _step_state_lock:
        _step_state_cache = None


def _step_state() -> dict[str, bool]:
    """Which wizard steps are already satisfied on this box. Booleans only.

    Memoised behind a lock for the same two reasons the doctor route is: the
    memo bounds how often the work happens, the lock bounds how many happen at
    once, and this is the route an unauthenticated caller can poll.
    """
    global _step_state_cache  # noqa: PLW0603

    with _step_state_lock:
        cached = _step_state_cache
        if cached is not None and monotonic() - cached[0] < _STEP_STATE_TTL_S:
            return cached[1]
        state = {
            "operator": False,  # reaching this route at all means there is no owner
            "provider": _any_provider_configured(),
            "channel": _telegram_configured(),
            "agents": _any_agent_installed(),
        }
        _step_state_cache = (monotonic(), state)
        return state


# ── claim ────────────────────────────────────────────────────────────


_ClaimBody = Annotated[Any, Depends(credential_body(("token",)))]


@router.post("/api/setup/claim", response_model=None)
def claim_setup(body: _ClaimBody, request: Request, _gated: Gate) -> Any:
    """Exchange the token ``genus init`` printed for a five-minute claim.

    A ``def`` handler on purpose: the verification and the file read are
    synchronous, and FastAPI runs these in its worker threadpool rather than on
    the event loop — the same reason the credential routes in ``auth.py`` are.

    Two limiters in front, both keyed on quantities the caller cannot choose:
    the process-wide flood ceiling (shared with sign-in, because both spend
    work for an unauthenticated caller), then five attempts a minute per
    address. The attempt is recorded BEFORE the token is checked, so guessing
    right on the sixth try is still refused.

    The setup token is deliberately NOT consumed here. A browser that reloads
    the page, or an operator who closes the tab before typing a password, must
    be able to try again; the operator step is what spends it.
    """
    from routers.auth import _client_ip, _peer_ip

    if local_login.flood_limited(_peer_ip(request)):
        return _throttled()
    if local_login.throttled(_CLAIM_BUCKET, _client_ip(request)):
        return _throttled()
    if isinstance(body, BodyTooLarge) or not isinstance(body, dict):
        return _bad_request()

    token = body.get("token") or ""
    if not setup_token.verify_setup_token(workspace(), token):
        # Not audited per refusal: this route is public, so a row per failure
        # turns a spray into a log amplifier. The limiter above bounds it.
        return _claim_refused()

    try:
        claim = tokens.issue_setup_claim_token()
    except TokenError as exc:
        # No signing key. This is a REAL state on the box this route exists to
        # serve: GENUS_AUTH_SIGNING_KEY unset and the vault not yet initialised
        # is exactly where a fresh install sits before `genus vault init`, and
        # `signing_key()` refuses rather than generating one it cannot store.
        # Unhandled it was a 500 and a traceback on the one route that has to
        # work on a fresh box, with nothing telling the operator what to do.
        #
        # 503 and the remedy, never a fallback key: an ephemeral one would mint
        # claims that stop verifying at the next restart, under a key nobody
        # kept.
        logger.error("setup: cannot mint a claim, no signing key (%s)", type(exc).__name__)
        return JSONResponse(
            {
                "error": "no signing key",
                "detail": (
                    "This instance cannot issue credentials yet. On the server, run "
                    "`genus vault init` (or set GENUS_AUTH_SIGNING_KEY), restart the "
                    "bridge, then open the setup link again."
                ),
            },
            status_code=503,
        )

    request.state.actor_id = "setup"
    audited(request, "setup.claim", action="claim")
    return {"claim_token": claim, "expires_in": tokens.SETUP_CLAIM_TTL_SECONDS}


# ── detect ───────────────────────────────────────────────────────────


@router.get("/api/setup/detect")
async def detect(request: Request, _gated: Ceremony, _claim: Claim) -> dict[str, Any]:
    """What this box already has, so the wizard can skip what is done.

    Fingerprints, model ids, booleans and check statuses. Never a credential,
    and never the doctor's ``detail`` strings — the full report enumerates what
    is wrong with the appliance, which services it runs and which migrations it
    is missing, and that is a map for anyone who should not have it. The strip
    at the top of the wizard needs an id and a word.
    """
    providers, ollama, telegram, checks, models = await asyncio.gather(
        asyncio.to_thread(_provider_state),
        asyncio.to_thread(_ollama_state),
        asyncio.to_thread(_telegram_configured),
        asyncio.to_thread(_required_check_rows),
        _model_catalog(),
    )
    return {
        "providers": providers,
        "models": models,
        "ollama": ollama,
        "telegram": {"configured": telegram},
        "doctor": {"checks": checks},
    }


# ── operator ─────────────────────────────────────────────────────────


_OperatorBody = Annotated[
    Any, Depends(credential_body(("name", "email", "password"), ("tenant_id",)))
]


@router.post("/api/setup/operator", response_model=None)
async def create_operator(
    body: _OperatorBody,
    request: Request,
    _gated: Gate,
    _finished: Ceremony,
    _claim: Claim,
) -> Any:
    """Create the owner account and hand the browser a signed-in session.

    The order is load-bearing. ``owner.yaml`` is written FIRST, then
    ``bootstrap_owner_account()`` derives the tenant, the email and the display
    name from it. That is the whole reason this does not run its own INSERT:
    the live doctor found an instance whose ``owner.yaml`` named one tenant
    while the account lived under another, and deriving the row from the file
    makes that class impossible rather than merely unlikely.

    Then: the password (argon2, through ``local_login.set_password``),
    ``GENUS_LOCAL_LOGIN`` turned on in config.yaml through the same writer
    ``genus config set`` uses, the setup token consumed, and one real
    ``authenticate()`` so the response carries the same token pair a sign-in
    would. The browser is in without a second form, and
    ``mfa_setup_required`` is true because an owner behind a password with no
    second factor is exactly the state #499's rule exists to end.

    Refusals, all before anything is written: an owner already exists (409 —
    the router's own lock, independent of the 404 gate, which is computed a
    moment earlier), an ``owner.yaml`` naming someone else (409), a password
    the policy rejects (422), an address that is not one (422). None of them
    echo the body.
    """
    if isinstance(body, BodyTooLarge) or not isinstance(body, dict):
        return _bad_request()

    name = str(body.get("name") or "").strip()
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    tenant_id = str(body.get("tenant_id") or "").strip() or _default_tenant()

    if not name or not _EMAIL.match(email):
        raise HTTPException(status_code=422, detail="a name and a valid email address are required")
    try:
        local_login.validate_password(password)
    except local_login.WeakPasswordError as exc:
        # The message names the policy, never the value.
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return await asyncio.to_thread(
        _create_owner_blocking, request, name, email, password, tenant_id
    )


def _create_owner_blocking(
    request: Request, name: str, email: str, password: str, tenant_id: str
) -> dict[str, Any]:
    """The synchronous half: owner.yaml, the account, the password, the session.

    Runs on a worker thread — argon2 and psycopg2 belong there — and holds
    ``_OPERATOR_LOCK`` for the whole of it, so two callers cannot both pass the
    "no owner yet" check and then both create one.
    """
    with _OPERATOR_LOCK:
        return _create_owner_locked(request, name, email, password, tenant_id)


def _create_owner_locked(
    request: Request, name: str, email: str, password: str, tenant_id: str
) -> dict[str, Any]:
    """The body of :func:`_create_owner_blocking`, with the lock held."""
    from robothor.auth import accounts
    from robothor.constants import owner_config_path
    from robothor.owner_config import load_owner_config, write_owner_config
    from robothor.settings.sources import owner_config_override_path

    # The router's OWN refusal, and it is not the 404 gate: the gate was
    # computed before this request's body was parsed, and the act below creates
    # the single account that owns the appliance.
    if accounts.owner_account_exists():
        raise HTTPException(status_code=409, detail="an owner account already exists")

    target = owner_config_override_path() or owner_config_path()
    # The FILE only. ``load_owner_config`` falls back to the deprecated
    # ROBOTHOR_OWNER_EMAIL / ROBOTHOR_OWNER_NAME pair when no file exists, and
    # a stale pair in the service environment would otherwise refuse the
    # operator standing at the wizard — or, worse, let
    # ``bootstrap_owner_account`` claim the appliance for whoever that variable
    # names. The file we write below is authoritative over both, which is why
    # writing it before the bootstrap is the whole design.
    existing = load_owner_config(target) if target.exists() else None
    if existing is not None and (existing.email or "").strip().lower() != email:
        # A half-finished earlier run, or another operator's identity. Creating
        # the account from it would silently claim the appliance for someone
        # the person at the keyboard did not name.
        raise HTTPException(
            status_code=409,
            detail=(
                "an operator identity already exists at this instance's owner.yaml "
                "under a different address; remove or correct it and try again"
            ),
        )
    if existing is None and not write_owner_config(name, email, tenant_id=tenant_id, path=target):
        raise HTTPException(status_code=500, detail="the operator identity could not be written")

    # Derived FROM owner.yaml, never from the request body: this is what makes
    # "the file names one tenant and the account lives in another" impossible
    # rather than merely unlikely.
    account = accounts.bootstrap_owner_account()
    if account is None:  # pragma: no cover - we just wrote the file it reads
        raise HTTPException(status_code=500, detail="the owner account could not be created")

    local_login.set_password(str(account["id"]), password)
    enabled = _enable_local_login()
    setup_token.consume_setup_token(workspace())

    audited(
        request,
        "setup.operator",
        action=str(account["id"]),
        tenant_id=str(account["tenant_id"]),
        local_login=enabled,
    )

    payload: dict[str, Any] = {
        "user": {
            "id": str(account["id"]),
            "email": str(account["email"]),
            "display_name": account.get("display_name") or name,
            "role": account["role"],
            "tenant_id": account["tenant_id"],
        },
        # Mandatory for the owner while local login is the only method (#499).
        "mfa_setup_required": True,
        "signed_in": False,
    }
    if not enabled:
        # Honest rather than convenient: the account exists and the password
        # works, but something outside config.yaml (an environment variable, a
        # unit drop-in) is keeping the sign-in route closed, so there is no
        # session to hand back and the operator has to be told which.
        payload["local_login_pending_restart"] = True
        return payload

    from routers.auth import _client_ip

    result = local_login.authenticate(
        str(account["tenant_id"]),
        email,
        password,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if not result.ok or not result.tokens:  # pragma: no cover - we set this password
        payload["local_login_pending_restart"] = True
        return payload

    payload.update(result.tokens)
    # Deliberately a CONSTANT, not `result.mfa_setup_required`. That flag is
    # advisory and goes false when `GENUS_OWNER_MFA_REQUIRED=false`; the wizard
    # still puts enrolment in front of the operator, because the account it has
    # just created is the owner of the appliance and a password is currently the
    # whole of what protects it. The earlier `bool(...) or True` was the same
    # value written as if it were a computation.
    payload["mfa_setup_required"] = True
    payload["signed_in"] = True
    return payload


def _enable_local_login() -> bool:
    """Turn ``GENUS_LOCAL_LOGIN`` on for this instance and make it live here.

    Written through the same writer ``genus config set`` uses, so config.yaml
    keeps its comments and its mode. Then the settings cache is dropped so that
    ``authenticate()`` below — in THIS process — sees the change; without that
    the wizard would create an account and the very next call would refuse it
    because the flag was read at boot.

    Returns whether local login is actually on afterwards. It can be false: an
    environment variable outranks config.yaml, and reporting "enabled" while
    the sign-in route stays closed is the failure this platform has shipped
    before.
    """
    from robothor.settings import reset_settings
    from robothor.settings.config_file import write_setting
    from robothor.settings.sources import config_yaml_path

    path = config_yaml_path()
    if path is not None:
        try:
            write_setting("auth", "local_login", True, names=("GENUS_LOCAL_LOGIN",), path=path)
        except OSError:
            logger.warning("setup: could not write config.yaml to enable local login")
    reset_settings()
    return local_login.local_login_enabled()


def _default_tenant() -> str:
    from robothor.constants import DEFAULT_TENANT

    return DEFAULT_TENANT


# ── provider ─────────────────────────────────────────────────────────


@router.post("/api/setup/provider")
async def configure_provider(request: Request, _gated: Ceremony, _claim: Claim) -> JSONResponse:
    """Store one provider credential, point the fleet at a model, and dial it.

    The verdict is what gates the step in the UI, and a provider that refuses
    the key is a 200 carrying ``ok: false`` rather than an error status: the
    request worked, the credential did not, and the browser has to be able to
    tell those apart to say something useful.

    The body is never logged or audited — it holds a key that exists nowhere
    else yet, so a debug line would be its first permanent home.
    """
    body = await _json_body(request)
    provider_id = str(body.get("provider_id") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    default_model = str(body.get("default_model") or "").strip()
    if not provider_id or not api_key or not default_model:
        raise HTTPException(
            status_code=422, detail="provider_id, api_key and default_model are required"
        )

    await _store_provider_key(provider_id, api_key)
    await _store_default_model(default_model)
    status, result = await _test_provider(provider_id, default_model)

    from robothor.engine.key_pool import key_fingerprint

    audited(
        request,
        "setup.provider",
        action=provider_id,
        provider=provider_id,
        fingerprint=key_fingerprint(api_key),
        model=default_model,
        result_ok=bool(isinstance(result, dict) and result.get("ok")),
    )
    return JSONResponse(result, status_code=status)


# ── channel ──────────────────────────────────────────────────────────


@router.post("/api/setup/channel")
async def configure_channel(request: Request, _gated: Ceremony, _claim: Claim) -> dict[str, Any]:
    """Verify a Telegram bot token with ``getMe`` and store it. Optional.

    An empty token is Skip, not an error: a channel-free instance is a normal,
    supported deployment. A token Telegram rejects is reported and NOT stored —
    a stored token nothing will accept is the "configured but not working"
    state the whole providers/channels surface exists to remove.
    """
    body = await _json_body(request)
    token = str(body.get("telegram_bot_token") or "").strip()
    if not token:
        audited(request, "setup.channel", action="telegram", skipped=True)
        return {"ok": True, "bot": "", "skipped": True}

    ok, bot = await _verify_telegram_token(token)
    if ok:
        await _store_telegram_token(token)
    audited(request, "setup.channel", action="telegram", verified=ok, bot=bot or None)
    return {"ok": ok, "bot": bot, "skipped": False}


# ── agents ───────────────────────────────────────────────────────────


@router.post("/api/setup/agent")
async def install_agents(request: Request, _gated: Ceremony, _claim: Claim) -> dict[str, Any]:
    """Install one catalogue preset, in this process.

    ``robothor.cli.agent.install_preset`` is the same function
    ``genus agent install --preset`` calls. Shelling out to the CLI would run a
    different interpreter against a possibly different workspace and report
    through parsed stdout; a partial install would be indistinguishable from a
    complete one.
    """
    body = await _json_body(request)
    preset = str(body.get("preset") or "").strip()
    if not preset:
        raise HTTPException(status_code=422, detail="a preset is required")

    outcome = await asyncio.to_thread(install_preset, preset, auto_yes=True)
    if outcome["unknown_preset"]:
        raise HTTPException(
            status_code=422,
            detail=f"unknown preset; the presets are {', '.join(outcome['available'])}",
        )

    audited(
        request,
        "setup.agents",
        action=preset,
        preset=preset,
        installed=len(outcome["installed"]),
        failed=len(outcome["failed"]) or None,
    )
    return {
        "preset": preset,
        "installed": outcome["installed"],
        # Ids only. The collected messages are exception strings and typically
        # carry an absolute path under `templates/agents/...`; the rest of this
        # router is scrupulous about not describing the box, and the operator's
        # next move is the same either way. The detail is in the bridge log.
        "failed": sorted(outcome["failed"]),
        "missing": outcome["missing"],
    }


# ── complete ─────────────────────────────────────────────────────────


@router.post("/api/setup/complete")
async def complete(request: Request, _gated: Ceremony, _claim: Claim) -> dict[str, Any]:
    """Close first-run setup and point the browser at the chat.

    Refuses (409) unless an owner account actually exists. Recording completion
    on a box with nobody in it would close the wizard over an instance that has
    no way in at all — and the marker this writes is the gate for every route
    above, so it must not be reachable before there is something to protect.

    The write is what closes the ceremony: from here on
    :func:`_ceremony_gate` answers 404 for ``detect``, ``provider``,
    ``channel``, ``agent`` and this route, and ``status``/``claim``/``operator``
    were already closed by the owner row. It goes at the TOP LEVEL of
    config.yaml rather than inside ``settings:``, because that block is
    validated against the registry and an undeclared key in it is rejected
    under strict mode: a marker there would stop a freshly completed instance
    from starting.
    """
    from robothor.auth import accounts

    if not await asyncio.to_thread(accounts.owner_account_exists):
        raise HTTPException(status_code=409, detail="no owner account exists yet")

    checks = await asyncio.to_thread(_required_check_rows)
    await asyncio.to_thread(_record_completion)
    audited(request, "setup.complete", action="complete", checks=len(checks))
    return {"next": "/?v=chat", "doctor": {"checks": checks}}


# ── helpers the suite replaces ───────────────────────────────────────


async def _json_body(request: Request) -> dict[str, Any]:
    """Parse a JSON object body, refusing anything else without echoing it.

    The read is BOUNDED, through the same ``_bounded_body`` the credential
    routes in ``auth.py`` use — imported, not copied, so there is one answer to
    "how much of a body will this bridge hold". ``await request.body()``
    concatenates the entire stream first and applies the ceiling afterwards,
    which is no ceiling at all: a caller holding a claim could make the bridge
    allocate an arbitrary body, and a chunked request declares no
    ``Content-Length`` for any front to refuse it by. That is the precise lever
    ``_bounded_body`` was written to close, and this router had reimplemented
    the unbounded version next to it.
    """
    import json as _json

    from routers.auth import _bounded_body

    raw = await _bounded_body(request)
    if raw is None:
        raise HTTPException(status_code=413, detail="request body too large")
    try:
        parsed = _json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid request") from None
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="invalid request")
    return parsed


def _provider_state() -> list[dict[str, Any]]:
    """Which providers hold a credential, by fingerprint. Never a key."""
    from robothor.engine.key_pool import PROVIDERS, provider_slots

    rows: list[dict[str, Any]] = []
    for spec in PROVIDERS:
        try:
            slots = provider_slots(spec.id)
        except Exception:  # noqa: BLE001 - one unreadable provider is not all of them
            logger.debug("setup: provider_slots(%s) failed", spec.id, exc_info=True)
            continue
        rows.append(
            {
                "id": spec.id,
                "label": getattr(spec, "label", spec.id),
                "configured": bool(slots),
                "slots": [
                    {
                        "position": slot.position,
                        "source": slot.source,
                        "fingerprint": slot.fingerprint,
                    }
                    for slot in slots
                ],
            }
        )
    return rows


def _ollama_state() -> dict[str, Any]:
    """Local models that can actually call a tool.

    "Installed" is not the question: a 14B model that cannot emit a tool call
    runs every agent on this platform into a wall, and the operator finds out
    when the first scheduled run does nothing. ``/api/show`` reports
    ``capabilities``, so the list is what the box can really do.
    """
    import json as _json
    import urllib.error
    import urllib.request

    from robothor.settings import get_settings

    try:
        ollama = get_settings().ollama
        base = (ollama.url or f"http://{ollama.host}:{ollama.port}").rstrip("/")
    except Exception:  # noqa: BLE001 - a broken config is "unreachable", not a 500
        return {"reachable": False, "tool_models": []}

    def _fetch(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = _json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(  # noqa: S310 - the URL is built above
            url, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310
            return dict(_json.loads(response.read(1_000_000)))

    try:
        tags = _fetch(f"{base}/api/tags")
    except Exception:  # noqa: BLE001 - no Ollama is a supported deployment
        return {"reachable": False, "tool_models": []}

    tool_models: list[str] = []
    for model in tags.get("models") or []:
        name = str(model.get("name") or "")
        if not name:
            continue
        try:
            shown = _fetch(f"{base}/api/show", {"model": name})
        except Exception:  # noqa: BLE001 - one model that will not describe itself
            continue
        if "tools" in (shown.get("capabilities") or []):
            tool_models.append(name)
    return {"reachable": True, "tool_models": tool_models}


async def _model_catalog() -> list[dict[str, str]]:
    """Model ids the engine can route to, so the wizard can offer a list.

    ``GET /api/models`` is operator-gated, and the caller here has no session
    by definition, so the catalogue comes through this route instead. Nothing
    in it is a secret: ids and their provider, which is what a ``<select>``
    needs.

    Best effort. An engine that is down or still starting on a fresh box is the
    normal case at this point in an install, and it must degrade to a free-text
    model field rather than blocking the whole detect call.
    """
    from routers._engine_client import engine_request

    try:
        status, body = await engine_request("GET", "/api/admin/models")
    except Exception:  # noqa: BLE001 - no engine yet is the common first-run case
        return []
    if status >= 400 or not isinstance(body, dict):
        return []
    return [
        {"id": str(entry.get("id")), "provider": str(entry.get("provider") or "")}
        for entry in body.get("models", [])
        if entry.get("id")
    ]


def _telegram_configured() -> bool:
    """Whether a bot token is resolvable. A boolean — never the token."""
    try:
        from robothor.settings import get_settings

        return bool(get_settings().channels.telegram_bot_token.strip())
    except Exception:  # noqa: BLE001
        return False


def _any_provider_configured() -> bool:
    return any(row["configured"] for row in _provider_state())


def _any_agent_installed() -> bool:
    """Whether this instance has any agent manifest at all."""
    try:
        from robothor.engine.config import EngineConfig

        directory = EngineConfig.from_env().manifest_dir
        return bool(directory and any(p.name != "_defaults.yaml" for p in directory.glob("*.yaml")))
    except Exception:  # noqa: BLE001
        return False


def _required_check_rows() -> list[dict[str, str]]:
    """The doctor's REQUIRED checks, as ids and statuses.

    Reuses the bridge's memoised offline report, so the wizard's strip and the
    Health view are the same run rather than two: the wizard polls, and a fresh
    26-check doctor per poll would put load on the database it is reporting on.
    """
    from routers.health import _cached_or_run

    try:
        payload = _cached_or_run()
    except Exception:  # noqa: BLE001 - a broken doctor is an empty strip, not a 500
        logger.warning("setup: the doctor could not run", exc_info=True)
        return []
    return [
        {"id": str(row.get("id")), "status": str(row.get("status"))}
        for row in payload.get("checks", [])
        if row.get("severity") == "required"
    ]


async def _store_provider_key(provider_id: str, api_key: str) -> None:
    """Write the credential where the ENGINE reads it, then make it live.

    The same two acts the Settings page performs — the vault row under the one
    naming module, and the engine's secrets reload. Without the reload the row
    exists and the running engine keeps dialling the key it started with.
    """
    from robothor import vault
    from robothor.vault.naming import provider_key
    from routers.providers import _known_provider, _reload_engine_secrets

    spec = _known_provider(provider_id)
    await asyncio.to_thread(vault.set, provider_key(spec.id, 1), api_key, category="credential")
    await _reload_engine_secrets()


async def _store_default_model(model: str) -> None:
    """Point the fleet's manifest defaults at the chosen model, then reload."""
    from routers.providers import _manifest_dir, _merge_model_block, _reload_engine_defaults

    await asyncio.to_thread(_merge_model_block, _manifest_dir() / "_defaults.yaml", model, [])
    await _reload_engine_defaults()


async def _test_provider(provider_id: str, model: str) -> tuple[int, dict[str, Any]]:
    """One real completion through the engine, the providers API's own path."""
    from routers._engine_client import engine_request
    from routers.providers import _TEST_TIMEOUT_SECONDS, _known_provider

    spec = _known_provider(provider_id)
    status, result = await engine_request(
        "POST",
        f"/api/admin/providers/{spec.id}/test",
        json={"model": model},
        timeout=_TEST_TIMEOUT_SECONDS,
    )
    if not isinstance(result, dict):
        result = {"ok": False, "model": model, "latency_ms": 0, "error_class": "BadGateway"}
    return status, result


def _telegram_getme_url(token: str) -> str:
    """The Bot API URL for ``getMe``.

    Percent-encoded into the path. The host is a literal so there is no SSRF
    here, but a bot token is operator-supplied text and a stray ``/``, ``?`` or
    ``#`` would silently change which Bot API method is called. The colon in
    ``<digits>:<secret>`` is a legal path character and the API expects it raw:
    encoding it made every verification fail while blaming the token.
    """
    return f"https://api.telegram.org/bot{quote(token, safe=':')}/getMe"


async def _verify_telegram_token(token: str) -> tuple[bool, str]:
    """``getMe``: the only Bot API call that costs nothing and proves the token
    is real, unrevoked, and belongs to the bot the operator thinks it does."""
    import httpx

    url = _telegram_getme_url(token)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
    except Exception:  # noqa: BLE001 - unreachable is a verdict, not a 500
        return False, ""
    if response.status_code != 200:
        return False, ""
    try:
        payload = response.json()
    except ValueError:
        return False, ""
    # A 200 whose body is a list, a string or null is not an answer this
    # understands. `.get` on one of those is an AttributeError and a 500 on an
    # operator-supplied value, which is the wrong way to fail a verification.
    if not isinstance(payload, dict):
        return False, ""
    result = payload.get("result")
    if not isinstance(result, dict):
        return False, ""
    return True, str(result.get("username") or "")


async def _store_telegram_token(token: str) -> None:
    """Store the bot token under the one channel naming the platform uses."""
    from robothor import vault
    from robothor.vault.naming import channel_field

    await asyncio.to_thread(
        vault.set, channel_field("telegram", "bot_token"), token, category="credential"
    )


def _record_completion() -> str:
    """Write ``setup_completed_at`` — the act that closes the wizard.

    Not a note: this IS the gate for every route after the operator step (see
    :func:`_ceremony_gate`), which is why it must go through the same function
    the gate reads, against the same workspace. Two spellings of that path would
    be a wizard that records completion somewhere nothing looks.
    """
    return setup_token.record_setup_completed(workspace())
