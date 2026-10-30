"""One way to reach a RUNNING engine's control API from outside it.

The engine holds state no other process can see. Credential pools live in its
memory: which key is in rotation, which is retired, why, and for how long. A
CLI or a doctor check that answers "is the key spent?" from its own process is
answering about a process that has never made an LLM call — it will say
"active" while the fleet is stalled.

That mattered on 2026-09-16. The operator raised the cap at the provider, and
the engine kept the key retired for its six-hour cooldown because nothing had
told it otherwise. The only way to bring it back without restarting the daemon
was ``POST /api/admin/secrets/reload``, which needs a service token in the
engine's own audience with the ``engine:control`` scope — the raw control
token an operator has to hand is rejected with 401, correctly.

So the token is minted here, per call, exactly as the bridge mints its own
(``crm/bridge/routers/_engine_client.py``, which now shares this module's base
URL and minting rather than keeping a second copy). It lives seconds and is
never printed: a credential in a terminal scrollback is a credential in a
backup.

Deliberately synchronous and ``urllib``-based. The callers are a CLI process
and a doctor check running on a daemon thread, neither of which has an event
loop, and the payloads are two small JSON objects.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: How long a minted engine credential is good for. Long enough for one
#: request, short enough that a token captured from a process listing is
#: worthless by the time it is read.
TOKEN_TTL_SECONDS = 120

#: Seconds to wait on the engine. A control endpoint that has not answered in
#: this long is not busy, it is wedged.
DEFAULT_TIMEOUT_S = 15.0

#: The only schemes an engine may be reached over. The URL comes from settings,
#: so it is exactly as trustworthy as whatever wrote the unit file — and a
#: ``file://`` there would turn every control call into something urllib
#: resolves somewhere nobody intended.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class EngineUnreachableError(RuntimeError):
    """The engine did not answer. Its state is unknown, not "fine"."""


def _checked_url(value: str, setting: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.netloc:
        # Names the SETTING, not the value: the operator has to go and edit
        # something, and "the engine URL" is not a thing they can find.
        raise ValueError(f"{setting} must be an http(s) URL with a host, got {value!r}")
    return value.rstrip("/")


def _single_url() -> str:
    from robothor.settings import get_settings

    engine = get_settings().engine
    explicit = (engine.url or "").strip()
    if explicit:
        return _checked_url(explicit, "ROBOTHOR_ENGINE_URL")
    return f"http://{engine.host}:{engine.port}"


def _platform_tenant() -> str:
    from robothor.constants import DEFAULT_TENANT
    from robothor.settings import get_settings

    return (get_settings().database.platform_tenant or "").strip() or DEFAULT_TENANT


#: A tenant id that may be substituted into a URL template. Tenant ids are
#: kebab-case slugs; anything else (a dot, a slash, a colon) would change which
#: HOST the template names, and the template is the operator's, not the caller's.
_TEMPLATE_SAFE_TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")

_warned_fallback = False


def reset_tenant_url_warning() -> None:
    """Forget that the single-URL fallback was warned about (tests)."""
    global _warned_fallback  # noqa: PLW0603
    _warned_fallback = False


def _tenant_engine_map(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ValueError("GENUS_TENANT_ENGINE_URLS must be a JSON object of {tenant: url}")
    return parsed


def engine_base_url(tenant: str | None = None) -> str:
    """Where the engine for *tenant* answers. Loopback unless deployment says otherwise.

    One engine per tenant: ``engine/auth.py`` refuses a token minted for any
    tenant but its own. The platform tenant (and ``None``) is the single
    ``ROBOTHOR_ENGINE_URL``. Any other tenant resolves, in order, through the
    explicit ``GENUS_TENANT_ENGINE_URLS`` map (local development, where every
    engine is loopback on its own port), the ``GENUS_TENANT_ENGINE_URL_TEMPLATE``
    with its literal ``{tenant}`` placeholder, and finally the single URL —
    with one warning per process, because that engine will refuse the call and
    the operator needs to know why.
    """
    global _warned_fallback  # noqa: PLW0603
    if tenant is None or tenant == _platform_tenant():
        return _single_url()

    from robothor.settings import get_settings

    auth = get_settings().auth
    mapped = _tenant_engine_map(auth.tenant_engine_urls).get(tenant)
    if mapped:
        return _checked_url(mapped.strip(), "GENUS_TENANT_ENGINE_URLS")
    template = (auth.tenant_engine_url_template or "").strip()
    if template:
        if not _TEMPLATE_SAFE_TENANT.fullmatch(tenant):
            raise ValueError("tenant id cannot be substituted into an engine URL")
        return _checked_url(
            template.replace("{tenant}", tenant), "GENUS_TENANT_ENGINE_URL_TEMPLATE"
        )
    if not _warned_fallback:
        _warned_fallback = True
        logger.warning(
            "No engine URL for a non-root tenant: set GENUS_TENANT_ENGINE_URLS or "
            "GENUS_TENANT_ENGINE_URL_TEMPLATE. Falling back to the single engine, "
            "which refuses another tenant's token."
        )
    return _single_url()


def control_token(
    service_id: str = "genus-cli",
    tenant_id: str | None = None,
    *,
    require_existing_key: bool = True,
) -> str:
    """A short-lived service token in the ENGINE's audience.

    Never returned to a caller that prints it, never logged. ``engine/auth.py``
    refuses a bridge-audience token outright, which is why an operator cannot
    simply paste the control token they already have.
    """
    from robothor.auth import tokens
    from robothor.auth.tokens import issue_service_token
    from robothor.constants import DEFAULT_TENANT
    from robothor.engine.auth import ENGINE_AUDIENCE
    from robothor.secrets import secret_source

    # Minting resolves the signing key, and ``signing_key()`` GENERATES and
    # upserts one when it reads as absent. A vault whose read fails while its
    # write works would therefore have its live signing key replaced by a
    # doctor run — invalidating every session and every stored MFA secret
    # (``doctor/checks/secrets._signing_key`` documents that hazard; this is
    # the caller that would have walked into it). The names come from
    # ``auth.tokens`` rather than a third copy of the same two strings.
    if require_existing_key and secret_source(
        tokens._ENV_NAME, vault_key=tokens._VAULT_KEY, live=True
    ) not in ("env", "vault"):
        raise EngineUnreachableError(
            "no JWT signing key resolves, so no engine credential can be minted from here"
        )
    if tenant_id is None:
        from robothor.settings import get_settings

        tenant_id = get_settings().database.tenant_id or DEFAULT_TENANT
    return issue_service_token(
        service_id,
        tenant_id,
        audience=ENGINE_AUDIENCE,
        scopes=("engine:control",),
        ttl_seconds=TOKEN_TTL_SECONDS,
    )


def control_request(
    method: str, path: str, *, timeout: float = DEFAULT_TIMEOUT_S
) -> dict[str, Any]:
    """Call one engine control route and return its decoded body.

    Raises :class:`EngineUnreachableError` for anything that is not a 2xx, with a
    message naming what an operator does about it. The body is never logged:
    these routes are about credentials.
    """
    url = f"{engine_base_url()}{path}"
    request = urllib.request.Request(  # noqa: S310 — scheme checked above
        url, method=method, headers={"Authorization": f"Bearer {control_token()}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return dict(json.loads(response.read() or b"{}"))
    except urllib.error.HTTPError as exc:
        raise EngineUnreachableError(
            f"the engine answered {exc.code} for {method} {path} — "
            "is this the same instance the daemon is running?"
        ) from None
    except Exception as exc:  # noqa: BLE001
        raise EngineUnreachableError(
            f"the engine did not answer at {engine_base_url()} "
            f"({type(exc).__name__}) — is genus-engine running?"
        ) from None
