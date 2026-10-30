"""What is waiting on a person, and how the Helm answers it.

Four kinds of question converge here, and the reason this is one router rather
than four endpoints is that the operator experiences them as one list — "things
that are stuck on me" — while the mechanics underneath are genuinely different:

``workflow``
    A ``workflow_approvals`` row. The answer is a verdict, and settling it lets
    the resume driver pick the run up on its next tick.
``question``
    An ``agent_questions`` row, written by the ``ask_user`` tool. The answer is
    free text.
``action``
    An ``operation_actions`` row in ``review``: an exact effect an agent
    proposed (a drafted email, say). The answer is a verdict, recorded by
    ``Operations.decide`` with its own ``operation_audit`` row; an executor
    claims it only once approved.
``escalation``
    An ``asyncio.Event`` inside the ENGINE process, with a coroutine parked on
    it. **The bridge cannot settle this by writing anything.** It is proxied to
    ``/api/admin/approvals/escalation/{id}`` through ``engine_request``, which is
    the one function in this repo allowed to mint an engine credential. A row
    written here instead would leave the agent waiting until its own timeout
    denied it, while the Helm showed a green tick.

The three durable kinds are tenant-local. Every route asks
``require_tenant_operator`` first, which admits an owner or admin of the tenant
the session acts in and returns that tenant; every store call then filters on it
AND runs inside ``tenant_scope`` so row-level security binds the same tenant. An
id that is not the acting tenant's is a 404 and settles nothing. An
``escalation`` lives in the one engine process, which is not tenant-scoped, so
it additionally needs the platform gate, ``require_operator``. Every *answer* — including one that
is refused — also writes exactly one ``audited`` event, identifiers only, so the
free-text answer never lands in the audit store that gets exported to a SIEM.
The listing does not: it changes nothing, and a read that writes a row per poll
would bury the decisions in the same table an auditor is reading them from.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from robothor.db.connection import tenant_scope
from robothor.engine.agent_questions import answer_question, get_question, list_pending_questions
from robothor.engine.approvals import (
    ApprovalDecision,
    decide_approval_by_id,
    get_approval_by_id,
    list_pending_approvals,
)
from robothor.operations.store import Conflict, Operations
from routers._audit import audited
from routers._engine_client import engine_request
from routers._operator import TenantOperator, require_operator, require_tenant_operator

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

KINDS = ("workflow", "question", "action", "escalation")

#: What may appear in the path segment this router hands to ``engine_request``.
#: That function builds a URL out of it, and ``_checked_path`` refuses anything
#: odd — but refusing here means the caller gets a 4xx instead of a 500, and
#: means the id is validated whether or not it is about to be proxied.
#:
#: Deliberately looser than a UUID, because an escalation id is
#: ``uuid4().hex`` — undashed — as the engine minted it. The two DURABLE kinds
#: get the stricter check below.
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

#: Kinds whose id is a Postgres ``UUID`` column value rather than a path
#: segment. Anything else reaches ``WHERE id = %s`` and raises
#: ``InvalidTextRepresentation`` — a 500 with no audit row, because the
#: exception escapes before :func:`audited` runs.
_UUID_KINDS = frozenset({"question", "workflow", "action"})

#: Envelope fields a proposed action's card names above its body, in order.
#: ``Approve`` authorizes the EXACT payload (``Operations.decide`` pins its
#: hash), so whoever approves must see who it goes to and what it says, the
#: same as in the Sales review. Strings are passed through; lists of ids are
#: joined.
_ENVELOPE_FIELDS = (
    ("sender", "sender"),
    ("recipient", "recipient"),
    ("purpose", "purpose"),
    ("subject", "subject"),
    ("claims", "claim_ids"),
    ("evidence", "evidence_ids"),
)


class AnswerRequest(BaseModel):
    """One decision. Which field matters depends on the kind."""

    answer: str | None = None
    approved: bool | None = None
    remember_session: bool = False
    note: str = ""


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _action_summary(kind: str, payload: Any) -> str:
    """One line naming what the proposal would do, from the payload if it says."""
    if isinstance(payload, dict):
        for key in ("summary", "subject", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return f"{kind}: {value.strip()}"
    return f"Proposed {kind}"


def _action_envelope(payload: Any) -> dict[str, str]:
    """Who and what a proposal addresses, from whichever fields it carries."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for label, key in _ENVELOPE_FIELDS:
        value = payload.get(key)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if isinstance(value, str) and value.strip():
            out[label] = value.strip()
    return out


#: Kinds whose payload, beyond the body and the envelope, holds only the
#: version pins and hashes their own review checks (``Sales.draft``): the card
#: shows the body alone. Every other kind shows the body and the rest of the
#: payload, because a plugin may address people in keys the envelope does not
#: name.
_BODY_ONLY_KINDS = frozenset({"sales.email"})


def _action_detail(kind: str, payload: Any) -> str:
    """The proposal's whole content: its body, then every field the envelope
    does not actually show (``_BODY_ONLY_KINDS`` excepted); the whole payload
    when there is no body.

    Never truncated. What is approved is the full payload, so a card that
    showed a prefix, or the body alone of a payload that also says who it goes
    to, would ask for a verdict on text nobody read.
    """
    if isinstance(payload, dict) and isinstance(payload.get("body"), str) and payload["body"]:
        named = _action_envelope(payload)
        shown = {"body", *(key for label, key in _ENVELOPE_FIELDS if label in named)}
        rest = {key: value for key, value in payload.items() if key not in shown}
        if not rest or kind in _BODY_ONLY_KINDS:
            return str(payload["body"])
        return (
            str(payload["body"]) + "\n\n" + json.dumps(rest, sort_keys=True, indent=2, default=str)
        )
    return json.dumps(payload, sort_keys=True, indent=2, default=str)


def _pending(tenant_id: str) -> list[dict]:
    """Everything waiting on a person in ``tenant_id``, newest deadline last.

    Composed rather than unioned in SQL: the two tables have different columns
    for good reasons (``crm/migrations/117`` explains at length) and a view that
    flattened them would have to invent a shared vocabulary that neither side
    actually uses. The one field that IS shared is the one the answer endpoint
    needs — ``kind`` plus ``id``.
    """
    out: list[dict] = [
        {
            "kind": "workflow",
            "id": str(row.id),
            "run_id": str(row.run_id),
            "agent_id": "",
            "question": row.prompt,
            "detail": row.detail,
            "options": [],
            "expires_at": _iso(row.expires_at),
            "created_at": _iso(row.created_at),
        }
        for row in list_pending_approvals(tenant_id=tenant_id)
    ]
    out.extend(
        {
            "kind": "question",
            "id": str(q.id),
            "run_id": str(q.run_id),
            "agent_id": q.agent_id,
            "question": q.question,
            "detail": "",
            "options": list(q.options or []),
            "expires_at": _iso(q.expires_at),
            "created_at": _iso(q.created_at),
        }
        for q in list_pending_questions(tenant_id=tenant_id)
    )
    out.extend(
        {
            "kind": "action",
            "id": str(row["id"]),
            "run_id": "",
            "agent_id": "",
            "action_kind": row["kind"],
            "question": _action_summary(row["kind"], row["payload"]),
            "detail": _action_detail(str(row["kind"]), row["payload"]),
            "envelope": _action_envelope(row["payload"]),
            "options": [],
            "expires_at": _iso(row["expires_at"]),
            "created_at": _iso(row["created_at"]),
        }
        for row in Operations(tenant_id).list_review()
    )
    out.sort(key=lambda item: str(item["expires_at"] or ""))
    return out


@router.get("")
def list_approvals(request: Request) -> dict:
    """Everything waiting on a human decision, of either durable kind.

    In-RAM escalations are deliberately absent: they live in the engine and are
    gone on restart, so listing them from here would require a second proxy
    whose answer is stale the moment it is rendered. The engine announces those
    over the run's own status stream (``approval_required``), which is where a
    prompt with a sub-minute life belongs.
    """
    who = require_tenant_operator(request)
    with tenant_scope(who.tenant_id):
        pending = _pending(who.tenant_id)
    return {"count": len(pending), "pending": pending}


@router.post("/{kind}/{approval_id}")
async def answer_approval(kind: str, approval_id: str, request: Request) -> Any:
    """Answer one waiting question. ``kind`` decides where the answer goes."""
    who = require_tenant_operator(request)
    if kind == "escalation":
        # The engine holds escalations in RAM, in one process, with no tenant.
        require_operator(request)

    if kind not in KINDS:
        return _refuse(request, kind, approval_id, 400, f"kind must be one of {KINDS}")
    if not _SAFE_ID.fullmatch(approval_id):
        return _refuse(request, kind, approval_id, 400, "malformed id")
    if kind in _UUID_KINDS and not _is_uuid(approval_id):
        # 422 rather than 400: the value is well-formed for a path segment and
        # wrong for this field, which is what 422 means. "refused" rather than
        # "denied" in the trail, because nothing was denied to anybody — a
        # verified operator sent something this route cannot act on.
        return _refuse(request, kind, approval_id, 422, f"{kind} ids are UUIDs", status="refused")

    try:
        body = AnswerRequest(**(await request.json()))
    except Exception:  # noqa: BLE001 — a malformed body is the caller's error
        return _refuse(request, kind, approval_id, 400, "invalid request body")

    if kind == "escalation":
        return await _resolve_escalation(request, approval_id, body)
    with tenant_scope(who.tenant_id):
        if kind == "question":
            return await _answer_question(request, approval_id, body, who)
        if kind == "workflow":
            return await _decide_workflow(request, approval_id, body, who)
        return await _decide_action(request, approval_id, body, who)


def _audit_decision(
    request: Request, who: TenantOperator, kind: str, item_id: str, settled: bool, **extra: Any
) -> None:
    """One ``approval.answer`` event naming who decided, in which tenant."""
    audited(
        request,
        "approval.answer",
        action=item_id,
        status="ok" if settled else "error",
        kind=kind,
        settled=settled,
        decided_by=who.actor,
        tenant_id=who.tenant_id,
        **extra,
    )


def _not_found(request: Request, kind: str, item_id: str) -> JSONResponse:
    """Not the acting tenant's (or nobody's). Indistinguishable on purpose."""
    return _refuse(request, kind, item_id, 404, f"no such {kind} in this tenant")


async def _answer_question(
    request: Request, question_id: str, body: AnswerRequest, who: TenantOperator
) -> Any:
    answer = (body.answer or "").strip()
    if not answer:
        return _refuse(request, "question", question_id, 400, "answer is required")

    settled = await asyncio.to_thread(
        answer_question, question_id, answer, answered_by=who.actor, tenant_id=who.tenant_id
    )
    if not settled and (
        await asyncio.to_thread(get_question, question_id, tenant_id=who.tenant_id) is None
    ):
        return _not_found(request, "question", question_id)
    _audit_decision(request, who, "question", question_id, settled)
    if not settled:
        return {"settled": False, "message": "Already answered — nothing changed."}
    return {"settled": True, "kind": "question", "id": question_id}


async def _decide_workflow(
    request: Request, approval_id: str, body: AnswerRequest, who: TenantOperator
) -> Any:
    if body.approved is None:
        return _refuse(request, "workflow", approval_id, 400, "approved is required")

    decision = ApprovalDecision.APPROVED if body.approved else ApprovalDecision.REJECTED
    settled = await asyncio.to_thread(
        decide_approval_by_id,
        approval_id,
        decision,
        decided_by=who.actor,
        note=body.note or "",
        tenant_id=who.tenant_id,
    )
    if not settled and (
        await asyncio.to_thread(get_approval_by_id, approval_id, tenant_id=who.tenant_id) is None
    ):
        return _not_found(request, "workflow", approval_id)
    _audit_decision(
        request,
        who,
        "workflow",
        approval_id,
        settled,
        decision=decision.value,
    )
    if not settled:
        return {"settled": False, "message": "Already decided — nothing changed."}
    # The run resumes on the engine's next approval-driver tick (≤1 min).
    return {"settled": True, "kind": "workflow", "id": approval_id, "decision": decision.value}


async def _decide_action(
    request: Request, action_id: str, body: AnswerRequest, who: TenantOperator
) -> Any:
    """Approve or reject a proposed action through the tenant's own ledger.

    ``Operations.decide`` writes the verdict and an ``operation_audit`` row with
    the actor and tenant in one transaction; this adds the bridge audit event.
    """
    if body.approved is None:
        return _refuse(request, "action", action_id, 400, "approved is required")

    ops = Operations(who.tenant_id)
    try:
        await asyncio.to_thread(ops.decide, action_id, bool(body.approved), who.actor)
        settled = True
    except Conflict:
        settled = False
        if await asyncio.to_thread(ops.get_action, action_id) is None:
            return _not_found(request, "action", action_id)
    decision = "approved" if body.approved else "rejected"
    _audit_decision(
        request,
        who,
        "action",
        action_id,
        settled,
        decision=decision,
    )
    if not settled:
        return {"settled": False, "message": "Already decided or expired — nothing changed."}
    return {"settled": True, "kind": "action", "id": action_id, "decision": decision}


async def _resolve_escalation(request: Request, request_id: str, body: AnswerRequest) -> Any:
    """Proxy to the engine, because the pending request lives in that process."""
    if body.approved is None:
        return _refuse(request, "escalation", request_id, 400, "approved is required")

    status, payload = await engine_request(
        "POST",
        f"/api/admin/approvals/escalation/{request_id}",
        json={"approved": bool(body.approved), "remember_session": bool(body.remember_session)},
    )
    ok = status == 200
    audited(
        request,
        "approval.answer",
        action=request_id,
        status="ok" if ok else "error",
        kind="escalation",
        approved=bool(body.approved),
        engine_status=status,
    )
    if ok:
        return {"settled": True, "kind": "escalation", "id": request_id}
    return JSONResponse(
        {
            "settled": False,
            "message": (payload or {}).get("error") or "the engine no longer holds that escalation",
        },
        status_code=status if status in (404, 503) else 502,
    )


def _is_uuid(value: str) -> bool:
    """Whether ``value`` is something Postgres will accept for a ``UUID`` column."""
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _refuse(
    request: Request,
    kind: str,
    approval_id: str,
    code: int,
    message: str,
    *,
    status: str = "denied",
) -> JSONResponse:
    """Answer and record the refusal. A refused write is a fact an auditor wants."""
    audited(
        request,
        "approval.answer",
        action=approval_id if _SAFE_ID.fullmatch(approval_id) else "malformed",
        status=status,
        kind=kind if kind in KINDS else "unknown",
        reason=message,
    )
    return JSONResponse({"settled": False, "message": message}, status_code=code)
