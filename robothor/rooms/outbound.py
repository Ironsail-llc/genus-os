"""Email a member approved, from their task workspace. Sync; async callers use
``asyncio.to_thread``.

The flow, on the operations ledger (``operation_actions``):

1. **Propose** (``propose_email``, the ``room_propose_email`` tool). Only in the
   member's own interactive turn in a task workspace whose open task is
   assigned to them (``approvals.member_turn``, the predicate deciding uses
   too). Nothing is sent: a ``room.email`` action is written holding the exact
   email (``payload``), hashed. Every recipient goes through the
   do-not-contact check.
2. **Decide.** The member under the member approval rule, or an operator.
3. **Deliver** (``deliver``, run by the engine on the approval event and by
   ``sweep``). The action is claimed by id, which re-checks the approved hash
   and leases it, so concurrent executors and a restart send it at most once
   (a lease that runs out becomes ``unknown`` and is never sent again). The
   sending mailbox and the do-not-contact list are checked again, the email
   goes out through the workspace mail provider, and ``finish_action``
   records the provider's receipt. A failed send is recorded and not retried.
4. **Receipt.** One assistant turn in the workspace conversation (``kind:
   "room_email_receipt"``) says what happened, and ``room.message`` is
   published. Rejection, expiry, refusal and an unconfirmed send get one too.

**Who it is sent as** is decided in exactly one place, ``sender_for``: today
the company's assistant mailbox (the one the ``gws_*`` tools send from), with
Reply-To the member's own address.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection, tenant_scope
from robothor.operations.proposer import Proposer
from robothor.operations.store import Conflict, NotPermitted, Operations
from robothor.rooms import approvals, dal
from robothor.rooms.keys import canonical_uuid
from robothor.rooms.tasks import TaskPerson
from robothor.sanitize import sanitize_log

if TYPE_CHECKING:
    from robothor.rooms.access import Actor

logger = logging.getLogger(__name__)

__all__ = [
    "KIND",
    "EmailRefused",
    "ProposedEmail",
    "Sender",
    "deliver",
    "propose_email",
    "send_raw",
    "sender_for",
    "sweep",
]

KIND = "room.email"
_VERSION = 1
_NOTED = "room.email.noted"
_TOOL = "room_propose_email"
_CHANGED = "the email was changed after it was approved"
_EXPIRED = "the approval expired before the email could be sent"
#: One plain mailbox address: no display name, no whitespace, no header breaks.
_ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


class EmailRefused(ValueError):  # noqa: N818 - named like the rooms errors
    """The draft cannot be proposed or sent as it stands; the message says why."""


@dataclass(frozen=True)
class Sender:
    """Who an approved workspace email is sent as."""

    address: str
    reply_to: str
    on_behalf_of: TaskPerson


@dataclass(frozen=True)
class ProposedEmail:
    action_id: str
    payload: dict[str, Any]
    expires_at: Any


# ─── who it is sent as: the one seam ─────────────────────────────────────


def _assistant_mailbox(tenant_id: str) -> str:
    """The mailbox the ``gws_*`` tools send from for this tenant."""
    from robothor.settings import get_settings

    settings = get_settings().workspace
    if settings.workspace_provider == "microsoft365":
        return (settings.m365_assistant_mailbox or "").strip().lower()
    from robothor.engine.tools.handlers.gws import _resolve_robothor_email

    return _resolve_robothor_email()


def _member(tenant_id: str, user_id: str) -> tuple[str, str] | None:
    """``(email, display_name)`` of an account of the tenant."""
    user = canonical_uuid(user_id)
    if user is None:
        return None
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT email, display_name FROM user_accounts WHERE tenant_id = %s AND id = %s",
            (tenant_id, user),
        )
        row = cur.fetchone()
    return (str(row["email"] or "").strip().lower(), row["display_name"] or "") if row else None


def sender_for(tenant_id: str, user_id: str) -> Sender:
    """Who ``user_id``'s approved email in ``tenant_id`` is sent as.

    The ONE place this is decided; the proposal, the approval card (which shows
    the proposal) and the executor all read it. Today: the company's assistant
    mailbox, Reply-To the member's own address. To send from the member's own
    mailbox later, change this function (and the transport) only."""
    mailbox = _assistant_mailbox(tenant_id)
    if not _ADDRESS.match(mailbox or ""):
        raise EmailRefused(
            "no assistant mailbox is configured for this company, so no email can be sent; "
            "ask an operator to connect one"
        )
    member = _member(tenant_id, user_id)
    if member is None or not _ADDRESS.match(member[0]):
        raise EmailRefused("your account has no email address for replies; ask an operator")
    return Sender(
        address=mailbox,
        reply_to=member[0],
        on_behalf_of=TaskPerson(user_id=str(canonical_uuid(user_id)), display_name=member[1]),
    )


# ─── the transport ───────────────────────────────────────────────────────


def send_raw(tenant_id: str, raw: str) -> dict[str, Any]:
    """Send a base64url RFC 5322 message through the tenant's workspace mail
    provider (the ``gws_*`` tools' provider). Returns its ``SendResult``."""
    from robothor.workspace import get_workspace
    from robothor.workspace.bridge import blocking

    result: dict[str, Any] = blocking(get_workspace(tenant_id).mail).send(raw)
    return result


def _mime(payload: dict[str, Any]) -> str:
    msg = EmailMessage(policy=SMTP)
    msg["From"] = payload["sender"]
    msg["Reply-To"] = payload["reply_to"]
    msg["To"] = ", ".join(payload["recipient"])
    if payload["cc"]:
        msg["Cc"] = ", ".join(payload["cc"])
    msg["Subject"] = payload["subject"]
    msg.set_content(payload["body"])
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


# ─── propose ─────────────────────────────────────────────────────────────


def _addresses(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise EmailRefused(f"{what} must be a list of email addresses")
    out = [v.strip() for v in value]
    bad = [v for v in out if not _ADDRESS.match(v)]
    if bad:
        raise EmailRefused(f"not a plain email address in {what}: {bad[0]!r}")
    return out


def _draft(to: Any, cc: Any, subject: Any, body: Any, attachments: Any) -> dict[str, Any]:
    if attachments:
        raise EmailRefused("attachments are not supported yet; send the email without them")
    recipients = _addresses(to, "to")
    if not recipients:
        raise EmailRefused("an email needs at least one recipient")
    copies = _addresses(cc, "cc")
    if not isinstance(subject, str) or not subject.strip():
        raise EmailRefused("an email needs a subject")
    if "\r" in subject or "\n" in subject:
        raise EmailRefused("the subject must be one line")
    if not isinstance(body, str) or not body.strip():
        raise EmailRefused("an email needs a body")
    return {"recipient": recipients, "cc": copies, "subject": subject.strip(), "body": body}


def _opt_out(tenant_id: str, run_id: str, addresses: list[str]) -> str | None:
    """The do-not-contact refusal for these recipients, or None (the ``gws_*`` check)."""
    from robothor.engine.tools.handlers.gws import _dnc_refusal

    refusal = _dnc_refusal(_TOOL, *addresses, run_id=run_id, tenant_id=tenant_id)
    return str(refusal.get("error") or "a recipient has opted out") if refusal else None


def propose_email(
    actor: Actor,
    room_id: str,
    run_id: str,
    *,
    to: Any,
    cc: Any = None,
    subject: Any,
    body: Any,
    attachments: Any = None,
) -> ProposedEmail:
    """Propose an email from the member's own turn in their task workspace.

    Raises ``NotPermitted`` outside that turn, ``EmailRefused`` for a draft
    that cannot be sent. Sends nothing."""
    turn = approvals.member_turn(actor, room_id, run_id)
    if turn is None:
        raise NotPermitted(
            "an email can be proposed only in your own conversation in a task workspace "
            "whose open task is assigned to you"
        )
    draft = _draft(to, cc, subject, body, attachments)
    refusal = _opt_out(actor.tenant_id, run_id, draft["recipient"] + draft["cc"])
    if refusal:
        raise EmailRefused(refusal)
    sender = sender_for(actor.tenant_id, actor.user_id)
    payload = {
        "version": _VERSION,
        "sender": sender.address,
        "reply_to": sender.reply_to,
        **draft,
        "on_behalf_of": {
            "user_id": sender.on_behalf_of.user_id,
            "display_name": sender.on_behalf_of.display_name,
        },
        "room_id": turn["room_id"],
        "task_id": turn["task_id"],
    }
    ops = Operations(actor.tenant_id)
    action = ops.propose(
        KIND,
        f"room-email:{uuid4()}",
        payload,
        proposer=Proposer(
            run_id=str(canonical_uuid(run_id)),
            user_id=sender.on_behalf_of.user_id,
            room_id=turn["room_id"],
        ),  # fmt: skip
    )
    row = ops.get_action(action) or {}
    return ProposedEmail(action_id=action, payload=payload, expires_at=row.get("expires_at"))


# ─── deliver ─────────────────────────────────────────────────────────────


def _finish(ops: Operations, row: dict[str, Any], status: str, receipt: dict[str, Any]) -> None:
    try:
        ops.finish_action(str(row["id"]), row["lease_token"], status, receipt)
    except Conflict:
        logger.warning("room email %s: lease lost before it could be finished", row["id"])


def deliver(tenant_id: str, action_id: str) -> str:
    """Send one approved workspace email, once. ``sent``, ``failed``,
    ``refused`` (not sent: altered, opted out) or ``skipped`` (not approved,
    not a workspace email, or another executor has it). Every connection it
    takes is bound to ``tenant_id`` (row-level security)."""
    with tenant_scope(tenant_id):
        return _deliver(tenant_id, action_id)


def _deliver(tenant_id: str, action_id: str) -> str:
    action = canonical_uuid(action_id)
    if action is None:
        return "skipped"
    ops = Operations(tenant_id)
    current = ops.get_action(action)
    if current is None or current["kind"] != KIND:
        return "skipped"
    try:
        row = ops.claim_action_by_id(action, lease_seconds=300)
    except Conflict:
        ops.refuse_changed_approval(action, _CHANGED)
        _note(tenant_id, action)
        return "refused"
    if row is None:
        _note(tenant_id, action)
        return "skipped"
    outcome = _send_claimed(tenant_id, ops, row)
    _note(tenant_id, action)
    return outcome


def _send_claimed(tenant_id: str, ops: Operations, row: dict[str, Any]) -> str:
    payload = row["payload"]
    try:
        sender = sender_for(tenant_id, payload["on_behalf_of"]["user_id"])
    except EmailRefused as exc:
        _finish(ops, row, "failed", {"error": str(exc)})
        return "failed"
    if sender.address != payload["sender"]:
        _finish(ops, row, "failed", {"error": "the sending mailbox changed since approval"})
        return "failed"
    refusal = _opt_out(
        tenant_id, str(row.get("proposed_by_run_id") or ""), payload["recipient"] + payload["cc"]
    )
    if refusal:
        _finish(ops, row, "cancelled", {"error": refusal})
        return "refused"
    try:
        result = send_raw(tenant_id, _mime(payload))
    except Exception as exc:  # noqa: BLE001 - recorded on the action, never retried
        _finish(ops, row, "failed", {"error": str(exc)[:500] or type(exc).__name__})
        return "failed"
    if not isinstance(result, dict) or result.get("error") or not result.get("id"):
        error = result.get("error") if isinstance(result, dict) else None
        _finish(ops, row, "failed", {"error": str(error or "the mail provider gave no message id")})
        return "failed"
    _finish(
        ops,
        row,
        "completed",
        {
            "id": str(result["id"]),
            "thread_id": result.get("threadId"),
            "sent_at": datetime.now(UTC).isoformat(),
        },
    )
    return "sent"


def sweep(tenant_id: str) -> int:
    """For ``tenant_id`` (the engine's own): deliver every approved workspace
    email the event missed, settle lost leases as ``unknown``, settle
    approvals that expired undelivered, and write every receipt still owed.
    Returns how many emails were sent."""
    with tenant_scope(tenant_id):
        ops = Operations(tenant_id)
        ops.expire_leases()
        ops.cancel_expired_approvals(KIND, _EXPIRED)
        sent = 0
        for row in ops.list_actions(kinds=[KIND], status="approved", limit=1000):
            sent += _deliver(tenant_id, str(row["id"])) == "sent"
        with ops.transaction() as cur:
            cur.execute(
                "SELECT a.id FROM operation_actions a WHERE a.tenant_id = %s AND a.kind = %s "
                "AND (a.status IN ('completed', 'failed', 'unknown', 'cancelled', 'rejected') "
                "OR (a.status = 'review' AND a.expires_at <= now())) "
                "AND NOT EXISTS (SELECT 1 FROM operation_audit o WHERE o.tenant_id = a.tenant_id "
                "AND o.entity_id = a.id::text AND o.event = %s)",
                (tenant_id, KIND, _NOTED),
            )
            owed = [str(r["id"]) for r in cur.fetchall()]
        for action in owed:
            _note(tenant_id, action)
        return sent


# ─── receipts ────────────────────────────────────────────────────────────


def _person(tenant_id: str, actor: str | None) -> dict[str, str] | None:
    if not actor or ":" not in actor:
        return None
    user = actor.partition(":")[2]
    member = _member(tenant_id, user)
    return {"user_id": user, "display_name": member[1] if member else ""}


def _receipt(tenant_id: str, row: dict[str, Any], error: str | None) -> dict[str, Any] | None:
    """The receipt for the action's state, or None while it is still pending."""
    status = row["status"]
    result = row.get("receipt") or {}
    expired = status == "review" and row["expires_at"] <= datetime.now(UTC)
    shown = {
        "completed": "sent",
        "failed": "failed",
        "unknown": "uncertain",
        "cancelled": "not_sent",
        "rejected": "rejected",
    }.get(status)
    if error is not None:
        shown = "not_sent"
    elif expired:
        shown = "expired"
    if shown is None:
        return None
    decided = _person(tenant_id, row.get("decided_by"))
    payload = row["payload"]
    return {
        "action_id": str(row["id"]),
        "status": shown,
        "from": payload.get("sender"),
        "reply_to": payload.get("reply_to"),
        "to": list(payload.get("recipient") or []),
        "cc": list(payload.get("cc") or []),
        "subject": payload.get("subject"),
        "approved_by": decided if status != "rejected" and decided else None,
        "decided_by": decided,
        "decided_at": row["decided_at"].isoformat() if row.get("decided_at") else None,
        "sent_at": result.get("sent_at") if shown == "sent" else None,
        "message_id": result.get("id") if shown == "sent" else None,
        "error": error or (result.get("error") if shown in ("failed", "not_sent") else None),
        "room_id": payload.get("room_id"),
        "task_id": payload.get("task_id"),
    }


def _text(r: dict[str, Any]) -> str:
    who = (r["approved_by"] or r["decided_by"] or {}).get("display_name") or "someone"
    to = ", ".join(r["to"]) + (f" (cc {', '.join(r['cc'])})" if r["cc"] else "")
    subject = f'"{r["subject"]}"'
    return {
        "sent": (
            f"Email sent from {r['from']} to {to}, subject {subject}, at {r['sent_at']}, "
            f"approved by {who}. Replies go to {r['reply_to']}. "
            f"Provider message id {r['message_id']}."
        ),
        "failed": (
            f"Email {subject} to {to} was approved by {who} but NOT sent: {r['error']}. "
            "It will not be retried; propose it again to try once more."
        ),
        "uncertain": (
            f"Email {subject} to {to} may or may not have been sent: sending stopped before "
            "the mail provider confirmed it. It will not be sent again; ask an operator to "
            f"check the {r['from']} mailbox."
        ),
        "not_sent": f"Email {subject} to {to} was not sent: {r['error']}.",
        "rejected": f"Email {subject} to {to} was rejected by {who}; nothing was sent.",
        "expired": f"Email {subject} to {to} expired before it was approved; nothing was sent.",
    }[r["status"]]


def _note(tenant_id: str, action_id: str, *, error: str | None = None) -> None:
    """Write the action's receipt into its workspace, once. Never raises."""
    try:
        with tenant_scope(tenant_id):
            _write_note(tenant_id, action_id, error)
    except Exception:
        logger.exception("could not write the receipt for room email %s", sanitize_log(action_id))


def _write_note(tenant_id: str, action_id: str, error: str | None) -> None:
    from robothor.engine import chat_store
    from robothor.events import bus

    ops = Operations(tenant_id)
    with ops.transaction() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{_NOTED}:{action_id}",)
        )
        cur.execute(
            "SELECT 1 FROM operation_audit WHERE tenant_id = %s AND entity_id = %s AND event = %s",
            (tenant_id, action_id, _NOTED),
        )
        if cur.fetchone():
            return
        cur.execute(
            "SELECT * FROM operation_actions WHERE tenant_id = %s AND id = %s AND kind = %s",
            (tenant_id, action_id, KIND),
        )
        row = cur.fetchone()
        receipt = _receipt(tenant_id, dict(row), error) if row else None
        if receipt is None:
            return
        room = dal.get_room(tenant_id, str(receipt["room_id"] or ""))
        if room is None:
            # Never recorded as written when it was not: the sweep tries again.
            logger.warning("room email %s: its workspace was not found", sanitize_log(action_id))
            return
        message_id = chat_store.save_message(
            room.session_key,
            "assistant",
            _text(receipt),
            channel="webchat",
            tenant_id=tenant_id,
            extras={"kind": "room_email_receipt", "receipt": receipt},
        )
        if message_id is None:
            return
        ops.audit(cur, action_id, _NOTED, "system", {"message_id": message_id})
    if message_id is not None:
        dal.record_activity(tenant_id, room.id, message_id)
        try:
            bus.publish(
                "agent",
                "room.message",
                {"room_id": room.id, "tenant_id": tenant_id, "message_id": message_id},
                source="rooms",
                tenant_id=tenant_id,
            )
        except Exception:
            logger.warning("could not publish room.message for %s", room.id, exc_info=True)
