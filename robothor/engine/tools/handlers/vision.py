"""Vision tool handlers — look, who_is_here, enroll/unenroll, mode.

Face enrollment linkage (Task 7, Unified Identity Context)
------------------------------------------------------------
The vision service (robothor/vision/service.py, port 8600, hand-rolled HTTP)
recognizes faces by a plain string label and has NO database access, by
design — it stays that way. This module is the engine-side join: it proxies
enroll/unenroll calls to the vision service over HTTP (unchanged) and
separately reads/writes ``face_identities`` (migration 089) to link a label
to a ``crm_people`` row. See ``robothor/identity/resolvers.py::_resolve_vision``
for the read side of this table.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from robothor.engine.tools.dispatch import ToolContext, _cfg

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _get_conn() -> Any:
    """Get a database connection. Use: with _get_conn() as conn:"""
    from robothor.engine.tools.dispatch import get_db

    return get_db()


@_handler("look")
async def _look(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    prompt = args.get("prompt", "Describe what you see in this image in detail.")
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(f"{_cfg().vision_url}/look", json={"prompt": prompt})
        resp.raise_for_status()
        return dict(resp.json())


def _join_face_identities(ctx: ToolContext, labels: list[str]) -> list[dict[str, Any]]:
    """Best-effort post-join of vision labels against face_identities +
    tenant_users (for role).

    Never raises: a DB error here must not break who_is_here's result —
    vision answers degrade to unjoined identifications (label only), not
    fail. Every entry gets verified=False regardless of match: presence (a
    probabilistic face match) is never authentication. ``role`` is included
    only when a linked, active tenant_users row exists — omitted otherwise
    rather than set to null, so callers can use plain key presence checks.
    """
    if not labels:
        return []

    rows_by_label: dict[str, tuple[Any, str | None, str | None]] = {}
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT fi.face_label, fi.person_id, fi.display_name, tu.role
                    FROM face_identities fi
                    LEFT JOIN tenant_users tu
                        ON tu.person_id = fi.person_id
                       AND tu.tenant_id = fi.tenant_id
                       AND tu.is_active = TRUE
                    WHERE fi.tenant_id = %s AND fi.face_label = ANY(%s)
                    """,
                    (ctx.tenant_id, list(labels)),
                )
                for face_label, person_id, display_name, role in cur.fetchall():
                    rows_by_label[face_label] = (person_id, display_name, role)
    except Exception:
        logger.exception("who_is_here: face_identities join failed, returning unjoined labels")
        rows_by_label = {}

    identifications: list[dict[str, Any]] = []
    for label in labels:
        entry: dict[str, Any] = {"label": label, "verified": False}
        match = rows_by_label.get(label)
        if match is not None:
            person_id, display_name, role = match
            entry["person_id"] = str(person_id) if person_id else None
            entry["display_name"] = display_name or ""
            if role:
                entry["role"] = role
        identifications.append(entry)
    return identifications


@_handler("who_is_here")
async def _who_is_here(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_cfg().vision_url}/health")
        resp.raise_for_status()
        data = resp.json()
    labels = data.get("people_present", [])
    return {
        "people_present": labels,
        "identifications": _join_face_identities(ctx, labels),
        "running": data.get("running", False),
        "mode": data.get("mode"),
        "last_detection": data.get("last_detection"),
    }


def _resolve_enrollment_identity(
    ctx: ToolContext, person_id: str, person_name: str
) -> tuple[str | None, str]:
    """Resolve enroll_face's optional person_id/person_name args to a
    ``(person_id, display_name)`` pair to write into face_identities.

    ``person_id``, when given, is looked up in crm_people (tenant-scoped): a
    person_id that doesn't resolve there is dropped rather than written —
    the face_identities.person_id FK would reject a dangling id anyway, and
    a caller-supplied id from the wrong tenant must never silently link
    across tenants. ``person_name`` alone (no person_id) is stored as
    free-text display_name with no person_id link: resolving a bare name to
    a specific crm_people row is ambiguous (multiple people can share a
    name) and out of scope for a one-shot enrollment call — use
    ``link_identity``/``robothor user link-face`` for a deliberate link.
    """
    if person_id:
        try:
            from robothor.crm import dal as crm_dal

            person = crm_dal.get_person(person_id, tenant_id=ctx.tenant_id)
        except Exception:
            logger.exception("enroll_face: crm_people lookup failed for person_id %r", person_id)
            person = None
        if person:
            name = person.get("name") or {}
            full = f"{name.get('firstName', '')} {name.get('lastName', '')}".strip()
            return person_id, full or person_name or ""
        logger.warning(
            "enroll_face: person_id %r not found in tenant %r — enrolling unlinked",
            person_id,
            ctx.tenant_id,
        )
        return None, person_name or ""
    return None, person_name or ""


def _link_face_enrollment(
    ctx: ToolContext, face_label: str, person_id_arg: str, person_name_arg: str
) -> dict[str, Any]:
    """Resolve + upsert the face_identities row for a just-enrolled label.

    Always upserts a row — even with person_id=NULL and display_name='' —
    so who_is_here's join has the label registered whether or not this
    enrollment named a person; a later `robothor user link-face` or
    re-enroll can fill in the link. Best-effort: never raises. An upsert
    failure (e.g. face_identities not migrated yet) degrades to
    {"linked": False} rather than failing the enrollment, which already
    succeeded on the vision service by the time this runs.
    """
    resolved_person_id, display_name = _resolve_enrollment_identity(
        ctx, person_id_arg, person_name_arg
    )
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO face_identities (tenant_id, face_label, person_id, display_name)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id, face_label) DO UPDATE
                        SET person_id = EXCLUDED.person_id,
                            display_name = EXCLUDED.display_name
                    """,
                    (ctx.tenant_id, face_label, resolved_person_id, display_name),
                )
                conn.commit()
    except Exception:
        logger.exception("enroll_face: face_identities upsert failed for label %r", face_label)
        return {"linked": False}
    return {
        "linked": resolved_person_id is not None,
        "person_id": resolved_person_id,
        "display_name": display_name or None,
    }


@_handler("enroll_face")
async def _enroll_face(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    face_name = args.get("name", "")
    if not face_name:
        return {"error": "Name is required for face enrollment"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{_cfg().vision_url}/enroll", json={"name": face_name})
        resp.raise_for_status()
        result = dict(resp.json())
    if result.get("success"):
        result["identity"] = _link_face_enrollment(
            ctx, face_name, args.get("person_id", ""), args.get("person_name", "")
        )
    return result


@_handler("enroll_face_from_image")
async def _enroll_face_from_image(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    face_name = args.get("name", "")
    image_paths = args.get("image_paths", [])
    if not face_name:
        return {"error": "Name is required"}
    if not image_paths:
        return {"error": "image_paths is required"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{_cfg().vision_url}/enroll-from-image",
            json={"name": face_name, "image_paths": image_paths},
        )
        resp.raise_for_status()
        result = dict(resp.json())
    if result.get("success"):
        result["identity"] = _link_face_enrollment(
            ctx, face_name, args.get("person_id", ""), args.get("person_name", "")
        )
    return result


@_handler("list_enrolled_faces")
async def _list_enrolled_faces(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_cfg().vision_url}/enrolled")
        resp.raise_for_status()
        return dict(resp.json())


def _delete_face_identity(ctx: ToolContext, face_label: str) -> None:
    """Best-effort delete of the face_identities row for an unenrolled
    label. Never raises: the vision-service unenrollment already succeeded
    by the time this runs, and a DB error here must not surface as a tool
    failure for an operation that already completed."""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM face_identities WHERE tenant_id = %s AND face_label = %s",
                    (ctx.tenant_id, face_label),
                )
                conn.commit()
    except Exception:
        logger.exception("unenroll_face: face_identities delete failed for label %r", face_label)


@_handler("unenroll_face")
async def _unenroll_face(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    face_name = args.get("name", "")
    if not face_name:
        return {"error": "Name is required"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{_cfg().vision_url}/unenroll", json={"name": face_name})
        resp.raise_for_status()
        result = dict(resp.json())
    _delete_face_identity(ctx, face_name)
    return result


# Keep in lockstep with robothor/vision/service.py VALID_MODES
# (pinned by test_vision_tools.py::TestSetVisionMode).
_SET_MODE_VALID = ("disarmed", "basic", "armed", "disabled")


@_handler("set_vision_mode")
async def _set_vision_mode(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    mode = args.get("mode", "")
    if mode not in _SET_MODE_VALID:
        return {"error": f"Invalid mode: {mode}. Valid: {', '.join(_SET_MODE_VALID)}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{_cfg().vision_url}/mode", json={"mode": mode})
        resp.raise_for_status()
        return dict(resp.json())


@_handler("log_interaction")
async def _log_interaction(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{_cfg().bridge_url}/log-interaction",
            json={
                k: args.get(k, "")
                for k in [
                    "contact_name",
                    "channel",
                    "direction",
                    "content_summary",
                    "channel_identifier",
                ]
            },
        )
        resp.raise_for_status()
        return dict(resp.json())
