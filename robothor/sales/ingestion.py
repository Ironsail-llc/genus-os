"""Authenticated, tenant-bound provider intake and canonical message processing.

The custom webhook header authenticates delivery. Campaign ownership and the
provider's email resource establish message identity separately. Negative events
stop pending work in the intake transaction; no model or provider call delays it.
"""

from __future__ import annotations

import asyncio
import hmac
import re
from datetime import datetime
from email.utils import getaddresses

from fastapi import APIRouter, HTTPException, Request
from psycopg2.extras import Json

from robothor import vault
from robothor.operations.store import Conflict
from robothor.sales.inbound import AuthenticationError, instantly_event
from robothor.sales.models import Contact, Message, Outcome
from robothor.sales.providers import Instantly, ProviderError, RateLimited
from robothor.sales.service import Sales

router = APIRouter()
_PATH = re.compile(r"/api/integrations/instantly/[A-Za-z0-9_-]{1,128}/webhook")
_REPLIES = {"reply_received", "auto_reply_received"}
_NEGATIVE = {"lead_unsubscribed", "lead_not_interested", "lead_wrong_person", "email_bounced"}


def is_instantly_webhook(method, path):
    """Only this POST authenticates with a provider secret instead of a JWT."""
    return method == "POST" and _PATH.fullmatch(path) is not None


def _owned(sales, event, cur):
    if not event["campaign_id"]:
        return None
    cur.execute(
        "SELECT a.* FROM operation_effects e JOIN operation_actions a "
        "ON a.tenant_id=e.tenant_id AND a.id::text=e.dedup_key "
        "WHERE e.tenant_id=%s AND e.kind='instantly.campaign' AND e.status='completed' "
        "AND e.receipt->>'id'=%s AND a.kind='sales.email'",
        (sales.tenant, event["campaign_id"]),
    )
    rows = cur.fetchall()
    if len(rows) != 1:
        return None
    action = dict(rows[0])
    draft = action["payload"]
    inbound = event["kind"] in _REPLIES
    lead = event.get("sender" if inbound else "recipient")
    mailbox = event.get("recipient" if inbound else "sender")
    # These fields are documented as optional. The unique owned campaign is
    # single-recipient; supplied identities must still agree with its draft.
    if lead not in (None, draft["recipient"]) or mailbox not in (None, draft["sender"]):
        return None
    sales.require(draft["prospect_id"], cur)
    return action


def receive_instantly(sales, raw, authorization, secret, workspace):
    event = instantly_event(raw, authorization, secret, workspace)
    with sales.ops.transaction() as cur:
        if not sales.ops.receive("instantly", event["event_id"], event, cur=cur):
            return False
        if event["kind"] == "account_error" and event.get("sender"):
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE", (sales.tenant,)
            )
            row = cur.fetchone()
            config = row["config"] if row else {}
            sender = event["sender"]
            if sender in config.get("senders", []) or sender in config.get(
                "mailbox_approved_until", {}
            ):
                config.setdefault("mailbox_approved_until", {}).pop(sender, None)
                cur.execute(
                    "UPDATE sales_settings SET config=%s WHERE tenant_id=%s",
                    (Json(config), sales.tenant),
                )
                cur.execute(
                    "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s "
                    "AND kind='sales.email' AND status IN ('review','approved') AND payload->>'sender'=%s",
                    (sales.tenant, sender),
                )
                sales.ops.enqueue("sales.stop", event["event_id"], {"sender": sender}, cur=cur)
                cur.execute(
                    "UPDATE operation_inbox SET processed_at=now() WHERE tenant_id=%s AND provider='instantly' AND event_id=%s",
                    (sales.tenant, event["event_id"]),
                )
                sales.ops.audit(cur, event["event_id"], "mailbox.readiness_revoked")
                return True
        action = _owned(sales, event, cur)
        if action:
            prospect = action["payload"]["prospect_id"]
            if event["kind"] in _NEGATIVE:
                sales.suppress(action["payload"]["recipient"], event["kind"], "instantly", cur=cur)
            if event["kind"] in _REPLIES | _NEGATIVE:
                cur.execute(
                    "UPDATE sales_prospects SET conversation_version=conversation_version+1,updated_at=now() "
                    "WHERE tenant_id=%s AND id=%s",
                    (sales.tenant, prospect),
                )
                cur.execute(
                    "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s "
                    "AND kind='sales.email' AND status IN ('review','approved') "
                    "AND payload->>'prospect_id'=%s",
                    (sales.tenant, prospect),
                )
                sales.ops.enqueue(
                    "sales.stop", event["event_id"], {"prospect_id": prospect}, cur=cur
                )
        sales.ops.enqueue(
            "sales.inbound", event["event_id"], {"event_id": event["event_id"]}, cur=cur
        )
        sales.ops.audit(
            cur, event["event_id"], "provider.event_received", detail={"kind": event["kind"]}
        )
        return True


@router.post("/api/integrations/instantly/{tenant_id}/webhook", status_code=202)
async def webhook(tenant_id: str, request: Request):
    # The path is a selector, never proof of authority. Authenticate before body
    # parsing, persistence, or looking at the prospect's records.
    if not is_instantly_webhook(request.method, request.url.path):
        raise HTTPException(404)
    secret = await asyncio.to_thread(
        vault.get, "providers/instantly/webhook_secret", tenant_id=tenant_id
    )
    authorization = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(authorization.encode(), ("Bearer " + secret).encode()):
        raise HTTPException(401, "Webhook authentication required")
    workspace = await asyncio.to_thread(
        vault.get, "providers/instantly/workspace_id", tenant_id=tenant_id
    )
    body = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 262144:
                    raise HTTPException(413, "Webhook payload too large")
    except TimeoutError:
        raise HTTPException(408, "Webhook body timeout") from None
    try:
        await asyncio.to_thread(
            receive_instantly, Sales(tenant_id), bytes(body), authorization, secret, workspace
        )
    except AuthenticationError:
        raise HTTPException(401, "Webhook authentication required") from None
    except (ValueError, KeyError, TypeError, OverflowError):
        raise HTTPException(422, "Invalid webhook event") from None
    return {"accepted": True}


def _addresses(value):
    if not isinstance(value, str):
        raise Conflict("Provider message address format requires review")
    return [Contact.email_address(address) for _, address in getaddresses([value])]


def _canonical(record, event, action):
    """Minimize an independently fetched email; reject ambiguous associations."""
    draft = action["payload"]
    inbound = event["kind"] in _REPLIES
    sender, recipient = (
        (draft["recipient"], draft["sender"]) if inbound else (draft["sender"], draft["recipient"])
    )
    if (
        not isinstance(record, dict)
        or record.get("organization_id") != event["workspace"]
        or record.get("campaign_id") != event["campaign_id"]
        or record.get("eaccount") != draft["sender"]
        or record.get("lead") not in (None, draft["recipient"])
        or record.get("ue_type") not in ({2} if inbound else {1, 3})
        or not isinstance(record.get("id"), str)
        or not record["id"]
        or (event["provider_id"] and record["id"] != event["provider_id"])
        or record.get("cc_address_email_list")
        or record.get("bcc_address_email_list")
    ):
        raise Conflict("Provider message identity requires reconciliation")
    if _addresses(record.get("from_address_email")) != [sender] or _addresses(
        record.get("to_address_email_list")
    ) != [recipient]:
        raise Conflict("Provider message participants require reconciliation")
    body = record.get("body")
    body = body.get("text") if isinstance(body, dict) else None
    if not isinstance(body, str) or not isinstance(record.get("subject"), str):
        # HTML-only mail stays in review until a supported plain-text extraction
        # contract exists. Never substitute unverified webhook text.
        raise Conflict("Provider message text requires review")
    return Message.model_validate(
        {
            "provider_id": record["id"],
            "prospect_id": draft["prospect_id"],
            "direction": "inbound" if inbound else "outbound",
            "occurred_at": record["timestamp_created"],
            "sender": sender,
            "recipient": recipient,
            "subject": record["subject"],
            "body": body,
            "campaign_id": event["campaign_id"],
            "auto_reply": inbound
            and (event["kind"] == "auto_reply_received" or record.get("is_auto_reply") == 1),
            "thread_id": record.get("thread_id"),
        }
    ).model_dump(mode="json")


class InstantlyInboxWorker:
    def __init__(self, sales, provider=None):
        self.sales = sales
        self.provider = provider or Instantly(sales.tenant)

    def _load(self, job):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT payload FROM operation_inbox WHERE tenant_id=%s AND provider='instantly' AND event_id=%s",
                (self.sales.tenant, job["payload"]["event_id"]),
            )
            row = cur.fetchone()
            if not row:
                raise Conflict("Provider event missing")
            event = row["payload"]
            action = _owned(self.sales, event, cur)
            if not action:
                raise Conflict("Provider event campaign association requires reconciliation")
            return event, action

    async def tick(self):
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.inbound", lease_seconds=120)
        if not job:
            return False
        try:
            event, action = await asyncio.to_thread(self._load, job)
            if await self.provider.secret("providers/instantly/workspace_id") != event["workspace"]:
                raise Conflict("Provider workspace changed; reconciliation required")
            message = None
            if event["kind"] in _REPLIES | {"email_sent"}:
                if event["provider_id"]:
                    record = await self.provider.get_email(event["provider_id"])
                    message = _canonical(record, event, action)
                else:
                    # One bounded page. Pagination or multiple matches is held,
                    # never resolved by choosing the newest message arbitrarily.
                    page = await self.provider.emails(
                        campaign_id=event["campaign_id"], workspace_id=event["workspace"]
                    )
                    if page.get("next_starting_after"):
                        raise Conflict("Provider message lookup requires paginated reconciliation")
                    matches = []
                    for record in page.get("items", []):
                        try:
                            candidate = _canonical(record, event, action)
                        except (ValueError, KeyError, TypeError):
                            continue
                        delta = abs(
                            (
                                Outcome.aware(datetime.fromisoformat(candidate["occurred_at"]))
                                - Outcome.aware(datetime.fromisoformat(event["occurred_at"]))
                            ).total_seconds()
                        )
                        if (
                            candidate["subject"] == event["subject"]
                            and candidate["body"] == event["body"]
                            and delta <= 300
                        ):
                            matches.append(candidate)
                    if len(matches) != 1:
                        raise Conflict("Provider message lookup did not have one exact match")
                    message = matches[0]
            elif event["kind"] not in _NEGATIVE | {"campaign_completed"}:
                raise Conflict("Provider event requires operator reconciliation")
            await asyncio.to_thread(self._commit, job, event, action, message)
        except RateLimited as exc:
            await asyncio.to_thread(
                self.sales.ops.defer,
                job["id"],
                job["lease_token"],
                "Provider read rate limited",
                delay_seconds=exc.retry_after,
                busy=True,
            )
        except (ValueError, KeyError, TypeError, ProviderError):
            # No provider bodies, message content, tokens, or addresses in errors.
            await asyncio.to_thread(
                self.sales.ops.defer,
                job["id"],
                job["lease_token"],
                "Provider event requires reconciliation",
                delay_seconds=300,
            )
        return True

    def _commit(self, job, event, action, message):
        with self.sales.ops.transaction() as cur:
            current = _owned(self.sales, event, cur)
            if not current or current["id"] != action["id"]:
                raise Conflict("Provider campaign association changed")
            if message:
                record_provider_message(self.sales, action, message, cur)
            cur.execute(
                "UPDATE operation_inbox SET processed_at=now() WHERE tenant_id=%s AND provider='instantly' AND event_id=%s",
                (self.sales.tenant, event["event_id"]),
            )
            self.sales.ops.complete(
                job["id"], job["lease_token"], {"event_id": event["event_id"]}, cur=cur
            )


def record_provider_message(sales, action, message, cur):
    """One verified observation and its matching action receipt, in one transaction."""
    created = sales.record_message(message, cur=cur)
    if message["direction"] == "outbound":
        # Replies have their own approved action; do not replace the initial
        # campaign's receipt with the UUID of a subsequent manual reply.
        cur.execute(
            "SELECT a.* FROM operation_effects e JOIN operation_actions a "
            "ON a.tenant_id=e.tenant_id AND a.id::text=e.dedup_key "
            "WHERE e.tenant_id=%s AND e.kind='instantly.reply' AND e.status='completed' "
            "AND e.receipt->>'id'=%s AND a.kind='sales.email'",
            (sales.tenant, message["provider_id"]),
        )
        replies = cur.fetchall()
        target = dict(replies[0]) if len(replies) == 1 else action
        draft = target["payload"]
        if len(replies) > 1 or any(
            message[field] != draft[field]
            for field in ("sender", "recipient", "subject", "body", "prospect_id")
        ):
            if created:
                sales.escalate(
                    message["prospect_id"],
                    "Observed outbound message differs from its recorded draft",
                    cur=cur,
                )
        else:
            cur.execute(
                "UPDATE operation_actions SET receipt=COALESCE(receipt,'{}'::jsonb)||%s "
                "WHERE tenant_id=%s AND id=%s AND status='completed'",
                (
                    Json(
                        {"delivery_status": "sent", "provider_message_id": message["provider_id"]}
                    ),
                    sales.tenant,
                    target["id"],
                ),
            )
    return created
