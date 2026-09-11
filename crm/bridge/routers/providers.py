"""Providers, models and fleet defaults — the operator-facing front door.

Every route here is ``require_operator`` first line and audited, and the split
with the engine is deliberate: the engine owns the key pool, the model
registry and the process litellm reads credentials from, so it answers "is
this configured" and "does this key work". The bridge owns the vault write and
the manifest write, because those are acts an operator performs and the audit
trail has to name them.

The one property that outranks everything else: a credential travels IN only.
A ``PUT`` answers with a digest, a test connection answers with a verdict, and
nothing on this router logs a request body — the candidate key the wizard is
validating exists nowhere but that body, and a debug line would be a permanent
home for it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from robothor import vault
from robothor.engine.key_pool import MAX_KEY_SLOTS, key_fingerprint, provider_by_id
from robothor.vault.naming import provider_key
from routers._audit import audited
from routers._engine_client import engine_request
from routers._operator import require_operator

logger = logging.getLogger(__name__)

router = APIRouter(tags=["providers"])

#: A test connection makes a real completion upstream; the engine caps it at
#: 20s, so the proxy has to outlast that or it reports a timeout the engine
#: never saw.
_TEST_TIMEOUT_SECONDS = 30.0


class KeyWrite(BaseModel):
    """One provider credential on its way in, and only in.

    ``SecretStr``, not ``str``: this model is a frame local of the handler, and
    structlog's console renderer prints every local through ``repr()`` when it
    formats an exception. A plain string would put the key into the journal the
    first time anything below the parse raised — the same route by which a
    credential reached a log on this instance before.
    """

    api_key: SecretStr = Field(min_length=1)


class DefaultsWrite(BaseModel):
    model: str = Field(min_length=1)
    fallbacks: list[str] = Field(default_factory=list)


def _manifest_dir() -> Path:
    """Where this instance's agent manifests live.

    Same resolution the fleet router uses. Never a literal path: the manifest
    directory is instance data and differs per deployment.
    """
    override = os.environ.get("ROBOTHOR_AGENTS_DIR")
    if override:
        return Path(override)
    workspace = os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor"))
    return Path(workspace) / "docs" / "agents"


def _known_provider(provider_id: str) -> Any:
    spec = provider_by_id(provider_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown provider")
    return spec


def _validated_position(position: int) -> int:
    """Refuse a slot the engine would never read.

    The pool walks slots 1..MAX_KEY_SLOTS and stops at the first gap, so a key
    written to slot 40 is a credential the operator believes is configured and
    nothing will ever dial — the precise failure this whole surface exists to
    remove. Better a 422 now than a silent one later.
    """
    if position < 1 or position > MAX_KEY_SLOTS:
        raise HTTPException(
            status_code=422,
            detail=f"key position must be between 1 and {MAX_KEY_SLOTS}",
        )
    return position


def _proxied(status: int, body: Any) -> JSONResponse:
    return JSONResponse(content=body, status_code=status)


async def _reload_engine_secrets() -> None:
    """Tell the engine to pick the new credential up.

    Without this the vault row exists and the running engine keeps dialling
    the key it started with: "saved" and "in use" would be different states
    with nothing in the UI to tell them apart.
    """
    status, _ = await engine_request("POST", "/api/admin/secrets/reload")
    if status >= 400:
        logger.warning("Engine refused a secrets reload (status %s)", status)


async def _occupied_slots(provider_id: str) -> set[int]:
    """Which storage slots this provider already holds, per the engine.

    Asked of the engine rather than computed here: the engine is the process
    that resolves credentials, and the bridge's own view of the vault and the
    environment is not the one that decides what gets dialled.
    """
    status, body = await engine_request("GET", "/api/admin/providers")
    if status >= 400 or not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="could not read the provider state")
    for provider in body.get("providers", []):
        if provider.get("id") == provider_id:
            return {slot["position"] for slot in provider.get("slots", [])}
    return set()


async def _refuse_a_numbering_gap(provider_id: str, slot: int) -> None:
    """A spare must extend the run, not start a second one past a hole.

    The pool walks slots from 1 and stops at the first empty one, so writing
    slot 3 while slot 2 is empty stores a credential nothing will ever dial
    and reports success — the operator then has a key they believe is a spare
    and an outage the moment the primary caps. 409 rather than 422: the
    request is well-formed, it is the appliance's current state that makes it
    wrong, and the message has to say which slot to fill first.
    """
    if slot == 1:
        return
    occupied = await _occupied_slots(provider_id)
    missing = [n for n in range(1, slot) if n not in occupied]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"slot {missing[0]} is empty, so a key in slot {slot} would never be "
                f"used — fill slot {missing[0]} first"
            ),
        )


async def _reload_engine_defaults() -> bool:
    """Ask the engine to re-read ``_defaults.yaml``. True if it confirmed."""
    status, body = await engine_request("POST", "/api/admin/defaults/reload")
    if status >= 400:
        logger.warning("Engine refused a defaults reload (status %s)", status)
        return False
    return bool(isinstance(body, dict) and body.get("reloaded"))


@router.get("/api/providers")
async def list_providers(request: Request) -> JSONResponse:
    """Which providers hold a credential, from where, and in what state."""
    require_operator(request)
    status, body = await engine_request("GET", "/api/admin/providers")
    return _proxied(status, body)


@router.get("/api/models")
async def list_models(request: Request) -> JSONResponse:
    """Every model the engine can route to, curated registry plus plugins."""
    require_operator(request)
    status, body = await engine_request("GET", "/api/admin/models")
    return _proxied(status, body)


@router.put("/api/providers/{provider_id}/keys/{position}")
async def set_provider_key(
    provider_id: str, position: int, body: KeyWrite, request: Request
) -> dict[str, Any]:
    """Store one provider credential and put it into service."""
    actor = require_operator(request)
    spec = _known_provider(provider_id)
    slot = _validated_position(position)
    value = body.api_key.get_secret_value().strip()
    if not value:
        raise HTTPException(status_code=422, detail="api_key must not be empty")
    await _refuse_a_numbering_gap(spec.id, slot)

    # psycopg2 is synchronous; this handler is async because it awaits the
    # engine, so the database write goes to a worker thread rather than
    # stalling the event loop for every other request on the bridge.
    await asyncio.to_thread(vault.set, provider_key(spec.id, slot), value, category="credential")
    await _reload_engine_secrets()

    fingerprint = key_fingerprint(value)
    audited(
        request,
        "provider.key.set",
        action=f"{spec.id}:{slot}",
        provider=spec.id,
        position=slot,
        fingerprint=fingerprint,
        actor_hint=actor,
    )
    return {"configured": True, "fingerprint": fingerprint, "position": slot}


@router.delete("/api/providers/{provider_id}/keys/{position}")
async def delete_provider_key(provider_id: str, position: int, request: Request) -> dict[str, Any]:
    """Remove one provider credential and take it out of service."""
    actor = require_operator(request)
    spec = _known_provider(provider_id)
    slot = _validated_position(position)

    removed = bool(await asyncio.to_thread(vault.delete, provider_key(spec.id, slot)))
    await _reload_engine_secrets()

    audited(
        request,
        "provider.key.delete",
        action=f"{spec.id}:{slot}",
        provider=spec.id,
        position=slot,
        removed=removed,
        actor_hint=actor,
    )
    return {"configured": False, "position": slot, "removed": removed}


@router.post("/api/providers/{provider_id}/test")
async def test_provider(provider_id: str, body: dict[str, Any], request: Request) -> JSONResponse:
    """Ask the engine to dial the provider once and report what happened.

    The body is forwarded verbatim and never inspected, logged or audited: it
    may carry a key that has not been stored anywhere yet, and this route is
    the only place in the appliance where such a value exists.
    """
    require_operator(request)
    spec = _known_provider(provider_id)
    status, result = await engine_request(
        "POST",
        f"/api/admin/providers/{spec.id}/test",
        json=body,
        timeout=_TEST_TIMEOUT_SECONDS,
    )
    audited(
        request,
        "provider.test",
        action=spec.id,
        provider=spec.id,
        status="ok" if status < 400 else "error",
        result_ok=bool(isinstance(result, dict) and result.get("ok")),
    )
    return _proxied(status, result)


def _write_defaults_atomically(path: Path, data: dict[str, Any]) -> None:
    """Replace ``_defaults.yaml`` in one step.

    A torn write here is not a lost setting, it is an unparseable manifest
    layer that every agent on the instance inherits — the same shape as the
    YAML typo that deleted the main agent for nearly four hours. Temp file in
    the same directory (so ``os.replace`` stays atomic) and rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _merge_model_block(path: Path, primary: str, fallbacks: list[str]) -> None:
    """Set ``model.primary`` / ``model.fallbacks`` and change nothing else.

    Everything already in the file survives, including the other keys inside
    ``model:``: a hand-written ``temperature`` is not collateral for changing a
    model id. Comments do not survive — PyYAML is what the engine parses this
    file with, and a second YAML library on the write side would be a second
    opinion about what the file means.
    """
    existing: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise HTTPException(status_code=500, detail="_defaults.yaml is not a mapping")
        existing = loaded

    model_block = dict(existing.get("model") or {})
    model_block["primary"] = primary
    model_block["fallbacks"] = list(fallbacks)
    existing["model"] = model_block
    _write_defaults_atomically(path, existing)


@router.patch("/api/providers/defaults")
async def set_default_models(body: DefaultsWrite, request: Request) -> dict[str, Any]:
    """Point the fleet at a different primary model and fallback chain.

    Manifests stay the source of truth (design decision D5), so this writes
    the ``model:`` block of ``_defaults.yaml`` and nothing else — every other
    key in that file, and every other key inside ``model:``, survives
    untouched. A hand-written ``temperature`` is not collateral for changing a
    model id.
    """
    actor = require_operator(request)

    status, catalog = await engine_request("GET", "/api/admin/models")
    if status >= 400 or not isinstance(catalog, dict):
        raise HTTPException(status_code=502, detail="could not read the model catalog")
    known = {entry["id"] for entry in catalog.get("models", [])}

    unknown = [name for name in [body.model, *body.fallbacks] if name not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown model id(s): {', '.join(sorted(unknown))}",
        )

    # Read-modify-write plus an fsync: filesystem work, off the event loop for
    # the same reason the vault write is.
    await asyncio.to_thread(
        _merge_model_block,
        _manifest_dir() / "_defaults.yaml",
        body.model,
        list(body.fallbacks),
    )

    # Manifests are the source of truth, so the write is only half the act:
    # the engine parsed the model block at boot and caches it, and until it is
    # told, the fleet keeps running on the model the operator just replaced.
    # This is design decision D5's "write validated YAML, then call engine
    # reconcile".
    in_effect = await _reload_engine_defaults()

    audited(
        request,
        "provider.defaults.set",
        action=body.model,
        primary=body.model,
        fallback_count=len(body.fallbacks),
        applied=in_effect,
        actor_hint=actor,
    )
    return {
        "model": body.model,
        "fallbacks": list(body.fallbacks),
        # Whether the ENGINE is running on it, not merely whether the file says
        # so. "Saved" and "in use" have been two different states on this
        # appliance often enough to be worth one boolean.
        "applied": in_effect,
    }
