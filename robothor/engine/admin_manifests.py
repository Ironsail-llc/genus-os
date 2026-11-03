"""Agent manifests over HTTP — the engine half of the Helm's agent builder.

The engine owns the workspace, the tool registry and the job set, so the
builder's reads, writes, verdicts and runs happen here, and the bridge's
``/api/agent-manifests`` routes proxy to these. The bridge keeps what is about
the OPERATOR: the session gate and the audit trail that names them.

Under ``/api/admin``, which ``engine/auth.py`` requires the ``engine:control``
scope for — reads included, because the listing enumerates the fleet, its
models and its delivery targets.

A write is only half the act: the job set changes only once the scheduler has
re-derived it. Here that happens IN-PROCESS after every successful write, and
its outcome is reported beside the write as ``reconcile`` — ``applied: true``
with the counts, or ``applied: false`` with a reason. A save that reports
success and changes nothing is the failure mode this surface exists to remove,
so "saved" and "in effect" are two different fields, and a reconcile that
cannot happen never rolls back a write that did: the file is the source of
truth and the watchdog reconciles it within five minutes anyway.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

# Module scope, not inside ``register``: under ``from __future__ import
# annotations`` FastAPI resolves a handler's ``Request`` annotation from the
# module's globals, and a function-local import answered 422 to every caller.
from fastapi import APIRouter, HTTPException, Request

from robothor.engine import manifest_store as store
from robothor.engine import manifest_validation as validation
from robothor.engine.manifest_rows import _broken, _chat_fields, _summary
from robothor.engine.manifest_store import (
    CreateRequest,
    DeleteRequest,
    ManifestError,
    PatchRequest,
    ValidateRequest,
)
from robothor.engine.sanitize import sanitize_log

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


# ─── What only this process can do ───────────────────────────────────


async def reconcile_schedules(scheduler: Any) -> dict[str, Any]:
    """Re-derive the live job set, and report what happened. Never raises."""
    from robothor.engine.admin_scheduler import resolve_scheduler

    live = resolve_scheduler(scheduler)
    if live is None:
        return {"applied": False, "error": "scheduler not running"}
    try:
        outcome = await live.reconcile()
    except Exception as error:  # noqa: BLE001 — reported, not raised
        # The TYPE only: an OSError's text carries the manifest directory.
        logger.warning("Reconcile after a manifest write failed: %s", type(error).__name__)
        return {"applied": False, "error": "the engine did not reconcile"}
    return {"applied": True, **outcome.as_dict()}


async def _registered_tools() -> tuple[set[str], list[dict[str, str]]]:
    """The registry's tool names, and a warning if it would not say.

    A registry that cannot be built downgrades the tool check to a warning
    rather than failing the save: the operator would otherwise be unable to
    edit an agent while a plugin is broken, which is exactly when they most
    want to.
    """
    from robothor.engine import admin_scheduler

    try:
        names = await asyncio.to_thread(admin_scheduler.registered_tool_names)
    except Exception as error:  # noqa: BLE001 — a warning, not a refusal
        logger.warning("Tool registry unavailable for validation: %s", type(error).__name__)
        return set(), [
            validation.issue(
                "tools_allowed",
                "tools_unverified",
                "the tool registry did not answer, so tool names were not checked",
            )
        ]
    return {str(name) for name in names}, []


async def _validation_context() -> validation.ValidationContext:
    """One view of the world for every verdict in this request.

    Its own function so an edit can validate the BEFORE and AFTER documents
    against the same fleet and the same tool list — see ValidationContext.
    """
    scan = await asyncio.to_thread(store.scan)
    fleet = {str(m["id"]): m for m in scan.manifests if isinstance(m.get("id"), str)}
    tools, tool_warnings = await _registered_tools()
    return validation.ValidationContext(
        fleet=fleet,
        tools=tools,
        manifest_dir=store.manifest_dir(),
        repo_root=store.workspace(),
        tool_warnings=tool_warnings,
    )


async def _validate(document: dict[str, Any]) -> dict[str, Any]:
    return await validation.validate(document, await _validation_context())


def _refuse(verdict: dict[str, Any]) -> None:
    """A failed validation, as a 422 carrying the validator's own words.

    The messages are the ones ``manifest_schema`` and ``manifest_checks``
    produce, verbatim, with their stable codes. "Invalid manifest" would leave
    the operator guessing which of forty fields to look at.
    """
    if not verdict["ok"]:
        raise ManifestError(422, verdict)


def _unparseable(error: ManifestError) -> dict[str, Any]:
    return {
        "ok": False,
        "errors": [validation.issue("", "unparseable", str(error.detail))],
        "warnings": [],
    }


# ─── The operations ──────────────────────────────────────────────────


async def list_fleet() -> dict[str, Any]:
    """Every agent this appliance has, including the ones that will not load.

    Per manifest rather than one generator over all of them: ``_block`` handles
    the shapes known to break ``_summary`` today; this handles the field nobody
    has added yet. One unreadable file belongs in ``broken``, where the operator
    can see which one to fix — not in a 500 that takes the other nineteen agents
    off the page with it.
    """
    scan = await asyncio.to_thread(store.scan)
    main_agent_id = store.main_agent_id()
    agents: list[dict[str, Any]] = []
    undescribable: list[dict[str, str]] = []
    for manifest in scan.manifests:
        agent_id = str(manifest.get("id") or "")
        try:
            agents.append({**_summary(manifest), **_chat_fields(manifest, main_agent_id)})
        except Exception as error:  # noqa: BLE001 — one manifest, not the page
            logger.warning(
                "Could not summarise %s: %s", sanitize_log(agent_id), type(error).__name__
            )
            undescribable.append(
                {"id": agent_id, "filename": "", "error_type": type(error).__name__}
            )
    agents.sort(key=lambda row: row["id"])
    return {
        "agents": agents,
        "broken": [*_broken(scan), *undescribable],
        "count": len(agents),
        # Named rather than inferred. The chat sends NO session_key for this
        # agent — that omission is what keeps the operator's shared main
        # session exactly as it was — and a browser that had to guess which id
        # that is would guess "main".
        "default_agent": main_agent_id,
    }


async def get_one(agent_id: str) -> dict[str, Any]:
    """One agent: its document, its raw YAML, its instructions, its verdict.

    A broken manifest answers 200 with the verdict filled in, not 404. "Absent"
    and "unreadable" are different problems with different fixes.
    """
    _, path = await asyncio.to_thread(store.require_manifest, agent_id)
    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    try:
        document = store.parse(text)
    except ManifestError as unreadable:
        return {
            "manifest": None,
            "yaml": text,
            "instructions": "",
            "validation": _unparseable(unreadable),
        }
    return {
        "manifest": document,
        "yaml": text,
        "instructions": await asyncio.to_thread(store.read_instructions, document),
        "validation": await _validate(document),
    }


async def validate_body(body: ValidateRequest) -> dict[str, Any]:
    """Always a verdict. A 422 would make "invalid" and "malformed" one answer."""
    if body.manifest is not None:
        return await _validate(body.manifest)
    if body.yaml is None:
        raise ManifestError(422, "send either 'manifest' or 'yaml'")
    try:
        document = store.parse(body.yaml)
    except ManifestError as unreadable:
        return _unparseable(unreadable)
    return await _validate(document)


async def create(body: CreateRequest, scheduler: Any) -> dict[str, Any]:
    """Scaffold an agent, validate it, write it, and make it fire.

    409 rather than an overwrite on a colliding id: the ids are derived from a
    display name, so a collision is far more likely to be an operator who
    forgot they already built this agent than one who meant to replace it.
    """
    agent_id = store.safe_id(body.id if body.id is not None else store.kebab(body.name))
    if (await asyncio.to_thread(store.manifest_path, agent_id)).exists():
        raise ManifestError(409, "an agent with that id already exists")
    document, relative, instructions = await asyncio.to_thread(store.scaffold, agent_id, body)
    verdict = await _validate(document)
    _refuse(verdict)
    await asyncio.to_thread(store.write_pair, agent_id, document, relative, instructions)
    return {
        "id": agent_id,
        "manifest": document,
        "warnings": verdict["warnings"],
        "reconcile": await reconcile_schedules(scheduler),
    }


async def apply_patch(
    agent_id: str, changes: dict[str, Any], change: str, scheduler: Any
) -> dict[str, Any]:
    """The one write path every edit goes through — PATCH, enable, disable.

    Enable and disable are the same act as any other field change, so they take
    the same route through validation, the history ring and the reconcile. A
    shortcut that wrote the flag directly would be a second way to change a
    manifest, and the second way is always the one that forgets to reconcile.

    The document is judged twice — as it was and as the edit leaves it — and
    refused only on what the edit INTRODUCED. See
    :func:`manifest_validation.introduced` for why a single verdict on the
    merged document locked the operator out of exactly the manifests they
    needed to fix or silence.
    """
    agent_id, path = await asyncio.to_thread(store.require_manifest, agent_id)
    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    original = store.parse(text)
    document = store.parse(text)
    store.merge_owned(document, changes)
    store.bump_version(document, change)
    # ONE context for both verdicts: two reads of the tool list across a single
    # save could disagree about which tools exist, and the difference between
    # the two answers is what decides the refusal.
    context = await _validation_context()
    verdict = validation.introduced(
        await validation.validate(original, context),
        await validation.validate(document, context),
    )
    _refuse(verdict)
    await asyncio.to_thread(store.snapshot, agent_id)
    await asyncio.to_thread(store.replace_manifest, path, document)
    return {
        "id": agent_id,
        "manifest": document,
        "warnings": verdict["warnings"],
        # The faults this edit did not introduce and did not fix. Also present
        # in `warnings`; named separately so a UI can say "saved — still broken
        # for these reasons" without diffing two lists. Empty when healthy.
        "pre_existing": verdict["pre_existing"],
        "reconcile": await reconcile_schedules(scheduler),
    }


async def retire(agent_id: str, body: DeleteRequest, scheduler: Any) -> dict[str, Any]:
    """Move the manifest to ``retired/``. Nothing is unlinked.

    The confirmation has to be the agent's own id, typed: a boolean
    ``confirm: true`` is a field a misdirected form fills in for you. The
    instruction file stays — it is the operator's writing, and re-creating the
    agent with the same id picks it back up.
    """
    agent_id = store.safe_id(agent_id)
    if body.confirm != agent_id:
        raise ManifestError(422, "confirm must be the agent's id")
    agent_id, path = await asyncio.to_thread(store.require_manifest, agent_id)
    await asyncio.to_thread(store.snapshot, agent_id)
    await asyncio.to_thread(store.retire, agent_id, path)
    return {"id": agent_id, "retired": True, "reconcile": await reconcile_schedules(scheduler)}


async def run(agent_id: str, runner: Any, auth: Any) -> dict[str, Any]:
    """Fire the agent once, now, through the same function the trigger route uses.

    ``triggered`` is that function's answer verbatim, including its ``error``
    for an agent it cannot load — the caller decides what a refusal looks like.
    """
    from robothor.engine import manual_trigger

    agent_id, _ = await asyncio.to_thread(store.require_manifest, agent_id)
    triggered = await manual_trigger.trigger_agent(runner, store.manifest_dir(), agent_id, auth)
    return {"id": agent_id, "triggered": triggered}


# ─── Routes ──────────────────────────────────────────────────────────


def register(app: FastAPI, scheduler: Any = None, runner: Any = None) -> None:
    """Mount the agent-manifest routes on the engine app."""
    from robothor.engine.auth import request_context

    router = APIRouter(prefix="/api/admin/agent-manifests", tags=["admin"])

    async def _answer(operation: Any) -> dict[str, Any]:
        try:
            result: dict[str, Any] = await operation
        except ManifestError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error
        return result

    @router.get("")
    async def list_manifests() -> dict[str, Any]:
        return await _answer(list_fleet())

    # Declared before ``/{agent_id}`` so the literal path is matched as itself.
    @router.post("/validate")
    async def validate_manifest(body: ValidateRequest) -> dict[str, Any]:
        return await _answer(validate_body(body))

    @router.get("/{agent_id}")
    async def get_manifest(agent_id: str) -> dict[str, Any]:
        return await _answer(get_one(agent_id))

    @router.post("", status_code=201)
    async def create_manifest(body: CreateRequest) -> dict[str, Any]:
        return await _answer(create(body, scheduler))

    @router.patch("/{agent_id}")
    async def patch_manifest(agent_id: str, body: PatchRequest) -> dict[str, Any]:
        return await _answer(apply_patch(agent_id, body.owned_paths(), body.change, scheduler))

    @router.post("/{agent_id}/enable")
    async def enable_manifest(agent_id: str) -> dict[str, Any]:
        # Writes ``schedule.enabled: true`` even when the key is absent: absent
        # means enabled, and a route that skipped the write would report success
        # without producing a document that says so.
        return await _answer(
            apply_patch(agent_id, {"schedule.enabled": True}, "Enabled via the Helm", scheduler)
        )

    @router.post("/{agent_id}/disable")
    async def disable_manifest(agent_id: str) -> dict[str, Any]:
        return await _answer(
            apply_patch(agent_id, {"schedule.enabled": False}, "Disabled via the Helm", scheduler)
        )

    @router.delete("/{agent_id}")
    async def retire_manifest(agent_id: str, body: DeleteRequest) -> dict[str, Any]:
        return await _answer(retire(agent_id, body, scheduler))

    @router.post("/{agent_id}/run")
    async def run_manifest(agent_id: str, request: Request) -> dict[str, Any]:
        return await _answer(run(agent_id, runner, lambda: request_context(request)))

    # Outside the builder's prefix, so no agent id can shadow it: the raw scan
    # the bridge's read-only fleet and automations views read for a tenant cell.
    @app.get("/api/admin/manifests", tags=["admin"])
    async def scan_manifests() -> dict[str, Any]:
        scan = await asyncio.to_thread(store.scan)
        return {"manifests": list(scan.manifests), "broken": _broken(scan)}

    app.include_router(router)
