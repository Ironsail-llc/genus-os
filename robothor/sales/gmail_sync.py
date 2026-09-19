"""Bounded, replayable observations of sales-owned Gmail conversations.

Each scheduled job reads one complete thread. Domain writes and the read receipt
commit together; expired leases cannot advance either. A pre-send scan uses the
same canonicalization and suppression path before revalidating message approval.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

from psycopg2.extras import Json

from robothor.operations.store import Conflict, digest
from robothor.sales.gmail import Gmail, headers, participants, plain_text
from robothor.sales.models import Message
from robothor.sales.providers import ProviderError

_OPT_OUT = re.compile(
    r"\b(unsubscribe(?: me)?|remove me(?: (?:from|off) (?:your|the|this) (?:list|mailing list))?|stop (?:emailing|contacting|sending)|do not (?:email|contact)|don't (?:email|contact))\b",
    re.IGNORECASE,
)


def authorized(action):
    value = digest(action["payload"])
    if (
        action["payload_hash"] != value
        or action["approved_hash"] != value
        or not (action.get("decided_by") or "").startswith("operator:")
        or action["effect_hash"] != value
    ):
        raise Conflict("Gmail observation lacks an intact approved action")


def canonical(provider, root, record, actions):
    """Only this owned two-party thread may become this prospect's conversation."""
    draft = root["payload"]
    if provider.provider_id(record.get("threadId")) != root["provider_receipt"].get("thread_id"):
        raise ProviderError("Gmail observation thread identity changed")
    h = headers(record)
    outbound = "SENT" in record.get("labelIds", [])
    sender, recipient = (
        (draft["sender"], draft["recipient"]) if outbound else (draft["recipient"], draft["sender"])
    )
    participants(h, sender, recipient)
    text = plain_text(record)
    target = None
    if outbound:
        matches = [
            a
            for a in actions
            if h.get("message-id") == provider.message_id(str(a["id"]), a["payload"])
        ]
        if len(matches) > 1:
            raise Conflict("Ambiguous Gmail outbound authorization")
        if matches:
            target = matches[0]
            authorized(target)
            expected = target["payload"]["body"].replace("\r\n", "\n").replace("\r", "\n")
            if not expected.endswith("\n"):
                expected += "\n"
            if (
                any(
                    target["payload"].get(k) != draft.get(k)
                    for k in ("sender", "recipient", "prospect_id")
                )
                or h.get("subject") != target["payload"]["subject"]
                or text != expected
            ):
                raise ProviderError("Gmail outbound content differs from approved action")
            if target["effect_status"] == "completed" and target["provider_receipt"].get(
                "id"
            ) != provider.provider_id(record.get("id")):
                raise Conflict("Gmail message differs from completed effect receipt")
            # The stored canonical body is the reviewed text; MIME line endings
            # and the required terminal transport newline were checked above.
            text = target["payload"]["body"]
    try:
        occurred = datetime.fromtimestamp(int(record["internalDate"]) / 1000, UTC)
        if (occurred - datetime.now(UTC)).total_seconds() > 300:
            raise ValueError
        message = Message(
            provider_id=provider.provider_id(record["id"]),
            prospect_id=draft["prospect_id"],
            direction="outbound" if outbound else "inbound",
            occurred_at=occurred,
            sender=sender,
            recipient=recipient,
            subject=h.get("subject", ""),
            body=text,
            thread_id=provider.provider_id(record["threadId"]),
            auto_reply=not outbound
            and bool(
                h.get("auto-submitted", "no").lower() != "no"
                or h.get("x-autoreply")
                or h.get("x-autorespond")
            ),
        ).model_dump(mode="json")
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        raise ProviderError("Gmail message metadata requires review") from None
    # Only the new visible reply is scanned, not quoted thread history/footers.
    visible = re.split(r"(?m)^\s*>|^On .+wrote:\s*$|^[-_]{3,}", text, maxsplit=1)[0]
    opt_out = (
        not outbound
        and not message["auto_reply"]
        and bool(
            _OPT_OUT.search(visible)
            or re.fullmatch(
                r"(?:re:\s*)?(?:unsubscribe|remove me|stop)\s*[.!]?",
                h.get("subject", ""),
                re.IGNORECASE,
            )
        )
    )
    return {
        "message": message,
        "action_id": str(target["id"]) if target else None,
        "opt_out": opt_out,
    }


class GmailThreadWorker:
    def __init__(self, sales, provider=None):
        self.sales = sales
        self.provider = provider or Gmail(sales.tenant)

    def _actions(self, cur, prospect_id):
        cur.execute(
            "SELECT a.*,e.payload_hash AS effect_hash,e.status AS effect_status,e.receipt AS provider_receipt "
            "FROM operation_actions a JOIN operation_effects e ON e.tenant_id=a.tenant_id AND e.dedup_key=a.id::text AND e.kind='gmail.send' "
            "WHERE a.tenant_id=%s AND a.kind='sales.email' AND a.payload->>'prospect_id'=%s ORDER BY a.created_at,a.id LIMIT 201",
            (self.sales.tenant, str(prospect_id)),
        )
        actions = [dict(r) for r in cur.fetchall()]
        if len(actions) > 200:
            raise Conflict("Gmail conversation exceeds the bounded action inventory")
        return actions

    def _load(self, action_id, *, cur=None):
        if cur is None:
            with self.sales.ops.transaction() as cursor:
                return self._load(action_id, cur=cursor)
        cur.execute(
            "SELECT payload->>'prospect_id' AS prospect_id FROM operation_actions WHERE tenant_id=%s AND id=%s AND kind='sales.email'",
            (self.sales.tenant, str(action_id)),
        )
        row = cur.fetchone()
        if not row:
            raise Conflict("Gmail action does not belong to this tenant")
        actions = self._actions(cur, row["prospect_id"])
        matches = [a for a in actions if str(a["id"]) == str(action_id)]
        if len(matches) != 1:
            raise Conflict("Gmail effect missing")
        root = matches[0]
        authorized(root)
        receipt = root["provider_receipt"] or {}
        if (
            root["effect_status"] != "completed"
            or receipt.get("mailbox") != self.provider.mailbox
            or root["payload"]["sender"] != self.provider.mailbox
        ):
            raise Conflict("Gmail thread requires a completed account-bound effect")
        self.provider.raw_id(receipt.get("thread_id"))
        self.provider.raw_id(receipt.get("id"))
        if self.provider.provider_id(receipt.get("gmail_message_id")) != receipt["id"]:
            raise Conflict("Gmail receipt resource identity mismatch")
        return root, actions

    def plan(self):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.sales.tenant + ":gmail-sync-plan",),
            )
            cur.execute(
                "SELECT roots.*,j.status AS last_status FROM ("
                "SELECT DISTINCT ON (e.receipt->>'thread_id') a.id AS action_id,e.receipt->>'thread_id' AS thread_id "
                "FROM operation_actions a JOIN operation_effects e ON e.tenant_id=a.tenant_id AND e.dedup_key=a.id::text "
                "WHERE a.tenant_id=%s AND a.kind='sales.email' AND e.kind='gmail.send' AND e.status='completed' "
                "AND e.receipt->>'mailbox'=%s ORDER BY e.receipt->>'thread_id',a.created_at,a.id) roots "
                "LEFT JOIN LATERAL(SELECT status,updated_at FROM operation_jobs WHERE tenant_id=%s AND kind='sales.gmail_sync' "
                "AND payload->>'thread_id'=roots.thread_id ORDER BY created_at DESC,id DESC LIMIT 1) j ON true "
                "WHERE j.status IS NULL OR (j.status='completed' AND j.updated_at<now()-interval '1 minute') "
                "ORDER BY j.updated_at NULLS FIRST,roots.action_id LIMIT 25",
                (self.sales.tenant, self.provider.mailbox, self.sales.tenant),
            )
            roots = list(cur.fetchall())
            now = datetime.now(UTC).isoformat()
            for root in roots:
                payload = {
                    "action_id": str(root["action_id"]),
                    "thread_id": root["thread_id"],
                    "mailbox": self.provider.mailbox,
                }
                self.sales.ops.enqueue("sales.gmail_sync", digest([payload, now]), payload, cur=cur)
            return len(roots)

    async def inspect(self, action_id):
        root, actions = await asyncio.to_thread(self._load, action_id)
        await self.provider.profile()
        thread = await self.provider.get_thread(root["provider_receipt"]["thread_id"])
        records = thread.get("messages")
        if (
            thread.get("id") != self.provider.raw_id(root["provider_receipt"]["thread_id"])
            or not isinstance(records, list)
            or not 1 <= len(records) <= 100
        ):
            raise ProviderError("Gmail thread incomplete or outside the 100-message scan bound")
        ids = [r.get("id") for r in records if isinstance(r, dict)]
        if (
            len(ids) != len(records)
            or not all(isinstance(i, str) for i in ids)
            or len(set(ids)) != len(ids)
            or root["provider_receipt"].get("gmail_message_id") not in ids
        ):
            raise ProviderError("Gmail thread has duplicate messages or lacks its owned anchor")
        events = [canonical(self.provider, root, record, actions) for record in records]
        anchor = next(
            e for e in events if e["message"]["provider_id"] == root["provider_receipt"]["id"]
        )
        if anchor["action_id"] != str(root["id"]):
            raise Conflict("Gmail owned anchor lacks canonical authorization")
        events.sort(key=lambda e: (e["message"]["occurred_at"], e["message"]["provider_id"]))
        return root, events

    def commit(self, action_id, events, *, job=None):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE",
                (self.sales.tenant,),
            )
            root, actions = self._load(action_id, cur=cur)
            self.sales.require(root["payload"]["prospect_id"], cur)
            mapped = {str(a["id"]): a for a in actions}
            for event in events:
                m = event["message"]
                if (
                    m["prospect_id"] != root["payload"]["prospect_id"]
                    or m["thread_id"] != root["provider_receipt"]["thread_id"]
                ):
                    raise Conflict("Gmail association changed during observation")
                created = self.sales.record_message(m, cur=cur)
                if event["opt_out"]:
                    if created:
                        self.sales.suppress(m["sender"], "email_opt_out", "gmail", cur=cur)
                elif created and m["direction"] == "outbound" and not event["action_id"]:
                    self.sales.escalate(
                        m["prospect_id"],
                        "Unapproved outbound Gmail message observed; human review required",
                        cur=cur,
                    )
                if event["action_id"]:
                    target = mapped.get(event["action_id"])
                    if not target:
                        raise Conflict("Gmail observed action disappeared")
                    authorized(target)
                    cur.execute(
                        "UPDATE operation_actions SET receipt=COALESCE(receipt,'{}'::jsonb)||%s||jsonb_build_object('delivery_status',CASE WHEN receipt->'delivery_failure'->>'action'='failed' THEN 'bounced' WHEN receipt->'delivery_failure'->>'action'='delayed' THEN 'delivery_delayed' ELSE 'sent_copy_verified' END) WHERE tenant_id=%s AND id=%s AND status='completed'",
                        (
                            Json(
                                {
                                    "delivery_status": "sent_copy_verified",
                                    "provider_message_id": m["provider_id"],
                                }
                            ),
                            self.sales.tenant,
                            event["action_id"],
                        ),
                    )
            result = {
                "thread_id": root["provider_receipt"]["thread_id"],
                "mailbox": self.provider.mailbox,
                "messages": len(events),
                "complete": True,
                "observed_at": datetime.now(UTC).isoformat(),
            }
            if job:
                if (
                    job["payload"]["thread_id"] != result["thread_id"]
                    or job["payload"]["mailbox"] != self.provider.mailbox
                ):
                    raise Conflict("Gmail job account association changed")
                self.sales.ops.complete(job["id"], job["lease_token"], result, cur=cur)
            self.sales.ops.audit(cur, action_id, "gmail.thread_observed", detail=result)
            return result

    async def tick(self):
        if (await asyncio.to_thread(self.sales.settings)).get("email_provider") != "gmail":
            return False
        await asyncio.to_thread(self.plan)
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.gmail_sync", lease_seconds=120)
        if not job:
            return False
        try:
            allowed = await asyncio.to_thread(
                self.sales.ops.admit_request, "gmail:thread", limit=30, window_seconds=60
            )
            if not allowed:
                await asyncio.to_thread(
                    self.sales.ops.defer,
                    job["id"],
                    job["lease_token"],
                    "Gmail read limit reached",
                    delay_seconds=60,
                    busy=True,
                )
                return True
            root, events = await self.inspect(job["payload"]["action_id"])
            await asyncio.to_thread(self.commit, str(root["id"]), events, job=job)
        except (Conflict, ProviderError, ValueError, TypeError, KeyError):
            await asyncio.to_thread(
                self.sales.ops.defer,
                job["id"],
                job["lease_token"],
                "Gmail conversation read requires review",
                delay_seconds=300,
            )
        return True

    def _prospect_actions(self, prospect_id):
        with self.sales.ops.transaction() as cur:
            return self._actions(cur, prospect_id)

    async def sync_prospect(self, prospect_id, *, exclude_action=None):
        actions = await asyncio.to_thread(self._prospect_actions, prospect_id)
        if any(
            a["effect_status"] != "completed" and str(a["id"]) != str(exclude_action)
            for a in actions
        ):
            raise Conflict("Resolve earlier Gmail writes before another send")
        roots = {}
        for a in actions:
            receipt = a["provider_receipt"] or {}
            if a["effect_status"] == "completed":
                if receipt.get("mailbox") != self.provider.mailbox:
                    raise Conflict("Gmail conversation account changed")
                roots.setdefault(receipt.get("thread_id"), a)
        if len(roots) > 10:
            raise Conflict("Gmail preflight exceeds the ten-thread review bound")
        for root in roots.values():
            admitted = await asyncio.to_thread(
                self.sales.ops.admit_request, "gmail:thread", limit=30, window_seconds=60
            )
            if not admitted:
                raise Conflict("Gmail preflight read limit reached")
            current, events = await self.inspect(str(root["id"]))
            await asyncio.to_thread(self.commit, str(current["id"]), events)
