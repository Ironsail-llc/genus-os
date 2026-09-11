"""Provider credentials, models, and a test connection — the engine half.

Before this, the only way to give an appliance an LLM credential was to edit a
file on the box over ssh, and the only way to find out whether the credential
worked was to run an agent and read the failure. That is the single largest
step between "installed" and "running", and it is why the dashboard shipped
with no provider configuration at all.

The real work lives here rather than in the bridge for one reason: the engine
is the process that makes LLM calls. It owns the key pool, the model registry,
and the environment litellm resolves credentials from, so it is the only place
that can answer "does this key work" with a completion rather than a guess.
The bridge proxies; it does not re-implement.

Three rules this module is built around:

* **A credential is write-only.** Every response carries a ``sha256:`` digest
  and a source, never key material — including inside an upstream provider's
  error text, which does echo the key back on a 401.
* **A test connection is a real completion.** ``max_tokens=1`` against a fixed
  ``ping``. A check that the key is non-empty would be the inert control this
  codebase has shipped six times.
* **A candidate key is used once and left nowhere.** The wizard tests a key
  before storing it, so it reaches litellm as a per-call keyword and never
  touches ``os.environ`` (shared with every thread and subprocess) or the
  vault.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, SecretStr

from robothor.engine import key_pool

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: A test connection is an interactive act — an operator is watching a spinner
#: in a browser. Long enough for a cold provider, short enough that a dead
#: endpoint reports rather than hangs.
TEST_TIMEOUT_SECONDS = 20

#: The prompt every test connection sends. Fixed so the call is the cheapest
#: possible proof that the credential authenticates and the model exists.
TEST_PROMPT = "ping"

_REDACTED = "[redacted]"

#: Upstream error text can be a whole HTML page. The operator needs the first
#: line, not the provider's stylesheet.
_MAX_MESSAGE_CHARS = 400


class TestConnectionRequest(BaseModel):
    """What the wizard or the Settings page asks us to dial.

    ``api_key`` is the key the operator has just typed and not yet saved. It is
    accepted so a bad key can be caught before it is stored, and it is used for
    exactly one call.
    """

    model: str | None = Field(default=None)
    # SecretStr, not str: this model is a frame local of the handler, and
    # structlog's console renderer prints every local through repr() when it
    # formats an exception. A plain str would put an unstored credential into
    # the journal the first time anything below this line raised.
    api_key: SecretStr | None = Field(default=None)


def _timestamps_for_vault_slots() -> dict[str, str]:
    """Refresh the vault snapshot and read every provider slot's write time.

    One function so the whole listing costs two database connections — one for
    the values, one for the timestamps — regardless of how many providers and
    slots exist. It blocks, so the route reaches it through a thread.

    Best-effort on the timestamps alone: the listing is a status page and must
    render on a box whose vault is not initialised at all. The values are not
    best-effort; ``vault_snapshot`` already degrades to "nothing configured".
    """
    from robothor.vault.naming import provider_key

    key_pool.refresh_vault_snapshot()

    wanted: list[str] = []
    for spec in key_pool.PROVIDERS:
        wanted.extend(
            provider_key(spec.id, slot.position)
            for slot in key_pool.provider_slots(spec.id)
            if slot.source == "vault"
        )
    if not wanted:
        return {}
    try:
        from psycopg2 import Error as PsycopgError

        from robothor.vault.dal import get_secrets_updated_at

        stamps = get_secrets_updated_at(wanted)
    except (PsycopgError, OSError, FileNotFoundError) as exc:
        # Narrow on purpose: a database that is down or a vault that was never
        # initialised are expected states for a status page. Anything else is a
        # bug and must not be swallowed into a blank column.
        logger.warning(
            "Vault timestamps unavailable (%s: %s); slots report updated_at=null",
            type(exc).__name__,
            exc,
        )
        return {}
    return {key: stamp.isoformat() for key, stamp in stamps.items()}


def _provider_payload(spec: key_pool.ProviderSpec, timestamps: dict[str, str]) -> dict[str, Any]:
    from robothor.vault.naming import provider_key

    slots = key_pool.provider_slots(spec.id)
    return {
        "id": spec.id,
        "label": spec.label,
        "configured": bool(slots),
        "slots": [
            {
                # The STORAGE slot, not a place in this list: it is what
                # DELETE .../keys/{position} addresses and what updated_at was
                # looked up by. A dense index silently named the wrong row.
                "position": slot.position,
                "source": slot.source,
                "fingerprint": slot.fingerprint,
                "state": slot.state,
                "updated_at": (
                    timestamps.get(provider_key(spec.id, slot.position))
                    if slot.source == "vault"
                    else None
                ),
            }
            for slot in slots
        ],
        "env_var": spec.env_var,
        "default_model": spec.default_model,
    }


def _model_provider(model_id: str) -> str:
    """Which provider a model id belongs to.

    Prefers the configured catalog so ``openrouter/anthropic/…`` is reported
    as OpenRouter (who bills it) rather than Anthropic (who trained it); falls
    back to the litellm route prefix for everything else — ``codex/``,
    ``ollama_chat/``, a plugin's own namespace.
    """
    for spec in key_pool.PROVIDERS:
        if model_id.startswith(spec.model_prefix):
            return spec.id
    head, _, tail = model_id.partition("/")
    return head if tail else "unknown"


def _supports_tools(model_id: str) -> bool | None:
    """Whether the model can call tools, when anything actually knows.

    ``ModelLimits`` does not carry this, and inventing a field that defaults to
    ``True`` would have the UI promise tool use for the 14B local models that
    demonstrably cannot do it. ``None`` means "unknown", which is the honest
    answer for a model no catalog covers.
    """
    try:
        import litellm

        return bool(litellm.supports_function_calling(model=model_id))
    except Exception:  # noqa: BLE001 - an unknown model is not an error
        return None


def _model_payload(model_id: str, limits: Any, source: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "provider": _model_provider(model_id),
        "context_window": limits.max_input_tokens,
        "supports_thinking": bool(limits.supports_thinking),
        "supports_tools": _supports_tools(model_id),
        "source": source,
    }


def _manifest_dir() -> Path:
    """Where this instance's agent manifests live. Never a literal path."""
    override = os.environ.get("ROBOTHOR_AGENTS_DIR")
    if override:
        return Path(override)
    workspace = os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor"))
    return Path(workspace) / "docs" / "agents"


def known_models() -> list[dict[str, Any]]:
    """Every model the engine can route to: curated registry plus plugins."""
    from robothor.engine import model_registry

    models = [
        _model_payload(model_id, limits, "registry")
        for model_id, limits in model_registry._MODEL_REGISTRY.items()
    ]
    known = {entry["id"] for entry in models}
    try:
        plugin_models = model_registry._plugin_model_limits()
    except Exception:  # noqa: BLE001 - a plugin never breaks a model listing
        logger.warning("Plugin model registry unavailable for the models listing")
        plugin_models = {}
    models.extend(
        _model_payload(model_id, limits, "plugin")
        for model_id, limits in plugin_models.items()
        if model_id not in known
    )
    return models


def _validate_test_model(spec: key_pool.ProviderSpec, model: str) -> None:
    """Refuse a model this provider's key has no business dialling.

    Two separate checks, because they stop two different things. The prefix
    check stops one provider's credential being sent to another provider's
    endpoint — the only part of this that is a security property. The catalog
    check is the same validation ``PATCH /defaults`` applies, so a typo is a
    422 naming the field rather than a 30-second wait for an upstream 404.

    The catalog is the model registry plus the providers' own defaults: three
    of the five defaults are not registry entries (nothing has pinned pricing
    for them), and rejecting the button the wizard itself offers would be
    worse than not validating at all.
    """
    from fastapi import HTTPException

    if not model.startswith(spec.model_prefix):
        raise HTTPException(
            status_code=422,
            detail=f"model {model!r} does not belong to provider {spec.id!r}",
        )
    catalog = {entry["id"] for entry in known_models()}
    catalog.update(provider.default_model for provider in key_pool.PROVIDERS)
    if model not in catalog:
        raise HTTPException(status_code=422, detail=f"unknown model id: {model}")


def _classify(exc: BaseException) -> str:
    """Fold a provider failure into the one word the UI acts on.

    The classes are chosen by what the operator does next: ``auth`` means fix
    the key, ``rate_limit`` means wait or add a spare, ``model_not_found``
    means pick another model, ``network`` means look at the box. ``unknown`` is
    reported as unknown rather than guessed at — a mislabelled cause sends the
    operator to the wrong fix, which is worse than no label.
    """
    if isinstance(exc, TimeoutError | ConnectionError | OSError):
        return "network"

    text = f"{type(exc).__name__}: {exc}".lower()
    if "429" in text or "rate limit" in text or "ratelimit" in text or "quota" in text:
        return "rate_limit"
    if (
        "404" in text
        or "model not found" in text
        or "not found" in text
        or "no such model" in text
        or "is not a valid model" in text
    ):
        return "model_not_found"
    if (
        "401" in text
        or "403" in text
        or "unauthorized" in text
        or "authentication" in text
        or "invalid api key" in text
        or "no auth credentials" in text
        or "api key" in text
    ):
        return "auth"
    if (
        "timeout" in text
        or "timed out" in text
        or "connection" in text
        or "network" in text
        or "unreachable" in text
        or "name resolution" in text
    ):
        return "network"
    return "unknown"


def _redact(message: str, secrets: set[str]) -> str:
    """Strip every credential we know of out of text bound for a response.

    Providers put the rejected key in the body of their own 401s, and that is
    the exact route by which an OpenRouter key reached a log on this instance.
    Classification happens on the raw text; only the redacted form leaves.
    """
    cleaned = message
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, _REDACTED)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > _MAX_MESSAGE_CHARS:
        cleaned = cleaned[:_MAX_MESSAGE_CHARS] + "…"
    return cleaned


async def _run_test_connection(
    spec: key_pool.ProviderSpec, request: TestConnectionRequest
) -> dict[str, Any]:
    configured = key_pool.resolve_keys(spec.id)
    candidate = request.api_key.get_secret_value().strip() if request.api_key is not None else ""
    api_key = candidate or (configured[0].key if configured else "")
    model = (request.model or "").strip() or spec.default_model
    # Every credential this provider could be dialling with, so an upstream
    # error that echoes any of them is scrubbed and not just the one we sent.
    secrets = {candidate, *(item.key for item in configured)}

    if not api_key:
        return {
            "ok": False,
            "model": model,
            "latency_ms": 0,
            "error_class": "auth",
            "message": f"No credential configured for {spec.label} and none supplied.",
        }

    started = time.monotonic()
    try:
        # Imported inside the try so an ImportError is classified and reported
        # like any other failure. Above it, the same exception escapes as a 500
        # whose traceback renders this frame's locals — and one of them is the
        # request model holding the operator's key.
        from robothor.engine import llm_client

        await llm_client.llm_call(
            [{"role": "user", "content": TEST_PROMPT}],
            model=model,
            max_tokens=1,
            temperature=0.0,
            timeout=TEST_TIMEOUT_SECONDS,
            max_retries=1,
            # SecretKey, not str: this value is bound into the kwargs dict that
            # litellm raises through, and structlog's console renderer prints
            # every frame local via repr(). That is the exact route by which an
            # OpenRouter key reached a log on this instance.
            api_key=key_pool.SecretKey(api_key),
        )
    except Exception as exc:  # noqa: BLE001 - every failure is a reportable result
        elapsed = int((time.monotonic() - started) * 1000)
        error_class = _classify(exc)
        # Deliberately not logger.exception: a traceback renders frame locals,
        # and the credential is a local of the frame that raised.
        logger.info(
            "Provider test connection for %s failed (%s / %s) in %dms",
            spec.id,
            type(exc).__name__,
            error_class,
            elapsed,
        )
        return {
            "ok": False,
            "model": model,
            "latency_ms": elapsed,
            "error_class": error_class,
            "message": _redact(f"{type(exc).__name__}: {exc}", secrets),
        }

    elapsed = int((time.monotonic() - started) * 1000)
    return {
        "ok": True,
        "model": model,
        "latency_ms": elapsed,
        "error_class": None,
        "message": f"{spec.label} answered in {elapsed}ms.",
    }


def register(app: FastAPI) -> None:
    """Mount the admin provider routes on the engine app.

    A separate module with one ``register`` call rather than four more closures
    in ``health.py``: that file is already the engine's largest, and the
    credential surface is the last thing that should be hard to find in it.
    Every path sits under ``/api/admin``, which ``engine/auth.py`` requires
    ``engine:control`` for.
    """
    from fastapi import APIRouter, HTTPException

    from robothor.credential_errors import install_credential_safe_validation

    # The test-connection body carries a key. FastAPI's default 422 would
    # reflect it back, so a mistyped field name would bounce the operator's
    # credential out of the appliance.
    install_credential_safe_validation(app)

    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.get("/providers")
    async def list_providers() -> dict[str, Any]:
        """Which providers are configured, from where, and in what state.

        The vault read runs in a thread: psycopg2 is synchronous, and blocking
        the engine's event loop on a status page stalls every agent turn and
        every webhook in flight.
        """
        timestamps = await asyncio.to_thread(_timestamps_for_vault_slots)
        return {"providers": [_provider_payload(spec, timestamps) for spec in key_pool.PROVIDERS]}

    @router.post("/providers/{provider_id}/test")
    async def test_provider(provider_id: str, body: TestConnectionRequest) -> dict[str, Any]:
        """One real completion, so "configured" and "working" stay different words."""
        spec = key_pool.provider_by_id(provider_id)
        if spec is None:
            raise HTTPException(status_code=404, detail="unknown provider")
        if body.model:
            _validate_test_model(spec, body.model.strip())
        return await _run_test_connection(spec, body)

    @router.get("/models")
    async def list_models() -> dict[str, Any]:
        """Every model the engine knows about, curated registry plus plugins."""
        return {"models": known_models()}

    @router.post("/secrets/reload")
    async def reload_secrets() -> dict[str, Any]:
        """Pick up credentials written to the vault without an engine restart."""
        result = await asyncio.to_thread(key_pool.reload_provider_keys)
        return {"reloaded": result.reloaded, "slots": result.slots}

    @router.post("/defaults/reload")
    async def reload_defaults() -> dict[str, Any]:
        """Re-read ``_defaults.yaml`` after the UI has rewritten it.

        Manifests stay the source of truth (design decision D5), so a UI that
        writes YAML has to tell the engine — otherwise the fleet keeps running
        on the model block it parsed at boot. ``_load_defaults`` caches on
        mtime, which usually notices a rename; this makes "usually" into
        "always", and gives the writer something to await so the response can
        honestly say the change is live.
        """
        from robothor.engine import config as engine_config

        engine_config._defaults_cache = (0.0, {})
        manifest_dir = _manifest_dir()
        defaults = engine_config._load_defaults(manifest_dir)
        model_block = defaults.get("model") or {}
        return {
            "reloaded": True,
            "primary": model_block.get("primary"),
            "fallbacks": list(model_block.get("fallbacks") or []),
        }

    app.include_router(router)
