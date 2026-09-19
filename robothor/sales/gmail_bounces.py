"""Finite metadata scans discover delivery reports outside owned Gmail threads.

Only multipart delivery-status reports have their raw content fetched. Plain mail
bodies are not collected. Fixed scan bounds, page receipts and overlap recover
missed/indexed-late notices; incomplete scans never supply preflight coverage.
"""

from __future__ import annotations

import asyncio
import base64
import re
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import Message as EmailHeaders
from email.parser import BytesParser
from email.utils import getaddresses

from psycopg2.extras import Json

from robothor.operations.store import Conflict, digest
from robothor.sales.gmail import Gmail, GmailMailboxError
from robothor.sales.gmail_controls import revoke_gmail
from robothor.sales.gmail_sync import authorized
from robothor.sales.models import Contact
from robothor.sales.providers import ProviderError


def one(part, name, *, required=True):
    values = part.get_all(name, [])
    if len(values) > 1 or (required and not values):
        raise ValueError("Ambiguous report field")
    return str(values[0]).strip() if values else None


def parse_report(raw, mailbox):
    """Minimize RFC 3464/6533 reports; human diagnostic prose is never interpreted."""
    try:
        if not isinstance(raw, bytes) or len(raw) > 2_000_000:
            raise ValueError
        mail = BytesParser(policy=policy.default).parsebytes(raw)
        if (
            mail.defects
            or mail.get_content_type() != "multipart/report"
            or mail.get_param("report-type") not in {"delivery-status", "global-delivery-status"}
        ):
            raise ValueError
        if [Contact.email_address(a) for _, a in getaddresses([one(mail, "To")])] != [mailbox]:
            raise ValueError
        parts = list(mail.walk())
        if len(parts) > 100 or any(p.defects for p in parts):
            raise ValueError
        ids, recipients = set(), []
        status_parts = 0
        for part in parts:
            kind = part.get_content_type()
            if kind in {"text/rfc822-headers", "message/global-headers"}:
                original = BytesParser(policy=policy.default).parsebytes(
                    part.get_payload(decode=True) or b""
                )
                ids.add(one(original, "Message-ID"))
            elif kind in {"message/rfc822", "message/global"}:
                children = part.get_payload()
                if not isinstance(children, list) or len(children) != 1:
                    raise ValueError
                ids.add(one(children[0], "Message-ID"))
            elif kind in {"message/delivery-status", "message/global-delivery-status"}:
                status_parts += 1
                blocks = part.get_payload()
                if kind == "message/global-delivery-status":
                    content = (
                        blocks[0].as_bytes()
                        if isinstance(blocks, list) and len(blocks) == 1
                        else (part.get_payload(decode=True) or b"")
                    )
                    blocks = [
                        BytesParser(policy=policy.default).parsebytes(block)
                        for block in re.split(rb"\r?\n\r?\n", content.strip())
                    ]
                if not isinstance(blocks, list) or not 2 <= len(blocks) <= 101:
                    raise ValueError
                one(blocks[0], "Reporting-MTA")
                for block in blocks[1:]:
                    final = one(block, "Final-Recipient")
                    address = one(block, "Original-Recipient", required=False) or final
                    address_type, separator, email = address.partition(";")
                    if separator != ";" or address_type.lower() not in {"rfc822", "utf-8"}:
                        raise ValueError
                    recipient = Contact.email_address(email)
                    outcome, status = one(block, "Action").lower(), one(block, "Status")
                    if not re.fullmatch(r"[245]\.\d{1,3}\.\d{1,3}", status):
                        raise ValueError
                    expected = {
                        "failed": "5",
                        "delayed": "4",
                        "delivered": "2",
                        "relayed": "2",
                        "expanded": "2",
                    }.get(outcome)
                    if expected != status[0]:
                        raise ValueError
                    if outcome in {"failed", "delayed"}:
                        recipients.append(
                            {"recipient": recipient, "action": outcome, "status": status}
                        )
        if len(ids) != 1 or status_parts != 1:
            raise ValueError
        original_id = next(iter(ids))
        if not re.fullmatch(r"<[^<>\s@]+@[^<>\s@]+>", original_id):
            raise ValueError
        return [{"original_id": original_id, **r} for r in recipients]
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
        raise ProviderError("Gmail delivery report requires operator review") from None


class GmailBounceWorker:
    def __init__(self, sales, provider=None):
        self.sales = sales
        self.provider = provider or Gmail(sales.tenant)

    def _actions(self, cur):
        cur.execute(
            "SELECT a.*,e.payload_hash AS effect_hash,e.status AS effect_status,e.receipt AS provider_receipt "
            "FROM operation_actions a JOIN operation_effects e ON e.tenant_id=a.tenant_id AND e.dedup_key=a.id::text AND e.kind='gmail.send' "
            "WHERE a.tenant_id=%s AND a.kind='sales.email' AND a.payload->>'sender'=%s ORDER BY a.created_at,a.id LIMIT 10001",
            (self.sales.tenant, self.provider.mailbox),
        )
        rows = [dict(r) for r in cur.fetchall()]
        if len(rows) > 10000:
            raise Conflict("Gmail delivery-report action inventory exceeds its bound")
        return rows

    def _last(self, cur):
        cur.execute(
            "SELECT * FROM operation_jobs WHERE tenant_id=%s AND kind='sales.gmail_bounces' AND payload->>'mailbox'=%s ORDER BY created_at DESC,id DESC LIMIT 1",
            (self.sales.tenant, self.provider.mailbox),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def plan(self):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.sales.tenant + ":gmail-bounces-plan",),
            )
            actions = self._actions(cur)
            if not actions:
                return
            last = self._last(cur)
            now = datetime.now(UTC)
            if last and (last["status"] != "completed" or not (last["result"] or {}).get("final")):
                return
            if last and last["updated_at"] > now - timedelta(minutes=1):
                return
            first = actions[0]["created_at"] - timedelta(minutes=5)
            prior = last["result"] if last else {}
            full_at = datetime.fromisoformat(prior["full_at"]) if prior.get("full_at") else None
            full = full_at is None or full_at < now - timedelta(days=1)
            after = (
                first
                if full
                else max(first, datetime.fromisoformat(prior["through"]) - timedelta(days=1))
            )
            payload = {
                "mailbox": self.provider.mailbox,
                "after": after.isoformat(),
                "through": now.isoformat(),
                "full_at": (now if full else full_at).isoformat(),
                "cursor": None,
                "seen_cursors": [],
                "page": 1,
            }
            self.sales.ops.enqueue("sales.gmail_bounces", digest(payload), payload, cur=cur)

    async def _read(self, payload):
        if payload["mailbox"] != self.provider.mailbox:
            raise Conflict("Gmail delivery-report scan account changed")
        await self.provider.profile()
        start, end = (
            datetime.fromisoformat(payload["after"]),
            datetime.fromisoformat(payload["through"]),
        )
        page = await self.provider.list_messages(
            q=f"to:{self.provider.mailbox} after:{int(start.timestamp())} before:{int(end.timestamp()) + 1}",
            cursor=payload["cursor"],
            limit=25,
        )
        items, cursor = page.get("messages", []), page.get("nextPageToken")
        if (
            not isinstance(items, list)
            or len(items) > 25
            or (
                cursor
                and (
                    not isinstance(cursor, str)
                    or len(cursor) > 4096
                    or cursor in payload["seen_cursors"]
                    or not items
                    or payload["page"] >= 100
                )
            )
        ):
            raise ProviderError("Gmail delivery-report page incomplete or cursor repeated")
        ids = [i.get("id") for i in items if isinstance(i, dict)]
        if (
            len(ids) != len(items)
            or not all(isinstance(i, str) for i in ids)
            or len(ids) != len(set(ids))
        ):
            raise ProviderError("Gmail delivery-report page identity invalid")
        observations = []
        for raw_id in ids:
            meta = await self.provider.get_metadata(raw_id)
            if meta.get("id") != raw_id:
                raise ProviderError("Gmail delivery report metadata identity changed")
            fields = meta.get("payload", {}).get("headers", [])
            if not isinstance(fields, list) or any(not isinstance(h, dict) for h in fields):
                raise ProviderError("Gmail message headers require review")
            values = [h["value"] for h in fields if h.get("name", "").lower() == "content-type"]
            if not values:
                continue
            if len(values) != 1:
                raise ProviderError("Gmail message MIME identity requires review")
            mime = EmailHeaders()
            mime["Content-Type"] = values[0]
            if mime.get_content_type() != "multipart/report" or mime.get_param(
                "report-type"
            ) not in {"delivery-status", "global-delivery-status"}:
                continue
            raw = await self.provider.get_raw(raw_id)
            if raw.get("id") != raw_id or raw.get("threadId") != meta.get("threadId"):
                raise ProviderError("Gmail delivery report resource identity changed")
            encoded = raw.get("raw", "")
            if not isinstance(encoded, str) or len(encoded) > 2_700_000:
                raise ProviderError("Gmail delivery report exceeds size bound")
            decoded = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
            reports = await asyncio.to_thread(parse_report, decoded, self.provider.mailbox)
            occurred = datetime.fromtimestamp(int(meta["internalDate"]) / 1000, UTC)
            if occurred < start or occurred > end + timedelta(seconds=1):
                raise ProviderError("Gmail delivery report lies outside the scan window")
            observations.extend(
                {
                    "provider_id": self.provider.provider_id(raw_id),
                    "occurred_at": occurred.isoformat(),
                    **report,
                }
                for report in reports
            )
        return observations, cursor

    def commit(self, job, observations, cursor):
        p = job["payload"]
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE",
                (self.sales.tenant,),
            )
            actions = self._actions(cur)
            matched = 0
            for event in observations:
                candidates = [
                    a
                    for a in actions
                    if event["original_id"] == self.provider.message_id(str(a["id"]), a["payload"])
                ]
                if len(candidates) > 1:
                    raise Conflict("Gmail delivery report matches multiple actions")
                if not candidates:
                    continue
                a = candidates[0]
                authorized(a)
                key = digest([event["provider_id"], event["original_id"], event["recipient"]])
                if not self.sales.ops.receive("gmail", key, event, cur=cur):
                    continue
                if event["recipient"] != a["payload"]["recipient"]:
                    self.sales.escalate(
                        a["payload"]["prospect_id"],
                        "Delivery report recipient differs from the approved message",
                        cur=cur,
                    )
                else:
                    matched += 1
                    if event["action"] == "failed":
                        self.sales.suppress(
                            event["recipient"], "gmail_delivery_failed", "gmail", cur=cur
                        )
                    else:
                        self.sales.escalate(
                            a["payload"]["prospect_id"],
                            "Gmail reported delayed delivery; review before more outreach",
                            cur=cur,
                        )
                    previous = (a["receipt"] or {}).get("delivery_failure", {})
                    if previous.get("action") != "failed":
                        receipt = {
                            "delivery_status": "bounced"
                            if event["action"] == "failed"
                            else "delivery_delayed",
                            "delivery_failure": event,
                        }
                        cur.execute(
                            "UPDATE operation_actions SET receipt=COALESCE(receipt,'{}'::jsonb)||%s WHERE tenant_id=%s AND id=%s",
                            (Json(receipt), self.sales.tenant, a["id"]),
                        )
                        a["receipt"] = {**(a["receipt"] or {}), **receipt}
                cur.execute(
                    "UPDATE operation_inbox SET processed_at=now() WHERE tenant_id=%s AND provider='gmail' AND event_id=%s",
                    (self.sales.tenant, key),
                )
                self.sales.ops.audit(
                    cur,
                    a["id"],
                    "gmail.delivery_report_observed",
                    detail={
                        "provider_id": event["provider_id"],
                        "action": event["action"],
                        "status": event["status"],
                    },
                )
            result = {
                "mailbox": self.provider.mailbox,
                "through": p["through"],
                "full_at": p["full_at"],
                "final": cursor is None,
                "matched": matched,
                "page": p["page"],
            }
            self.sales.ops.complete(job["id"], job["lease_token"], result, cur=cur)
            if cursor:
                payload = {
                    **p,
                    "cursor": cursor,
                    "seen_cursors": [*p["seen_cursors"], cursor],
                    "page": p["page"] + 1,
                }
                self.sales.ops.enqueue("sales.gmail_bounces", digest(payload), payload, cur=cur)

    async def tick(self):
        if (await asyncio.to_thread(self.sales.settings)).get("email_provider") != "gmail":
            return False
        await asyncio.to_thread(self.plan)
        job = await asyncio.to_thread(
            self.sales.ops.claim, "sales.gmail_bounces", lease_seconds=120
        )
        if not job:
            return False
        try:
            allowed = await asyncio.to_thread(
                self.sales.ops.admit_request, "gmail:delivery-reports", limit=10, window_seconds=60
            )
            if not allowed:
                await asyncio.to_thread(
                    self.sales.ops.defer,
                    job["id"],
                    job["lease_token"],
                    "Gmail report scan rate limited",
                    delay_seconds=60,
                    busy=True,
                )
                return True
            async with asyncio.timeout(75):
                events, cursor = await self._read(job["payload"])
            await asyncio.to_thread(self.commit, job, events, cursor)
        except (ProviderError, Conflict, ValueError, TypeError, KeyError, TimeoutError) as exc:
            if isinstance(exc, GmailMailboxError):
                await asyncio.to_thread(revoke_gmail, self.sales, self.provider.mailbox)
            await asyncio.to_thread(
                self.sales.ops.defer,
                job["id"],
                job["lease_token"],
                "Gmail delivery-report read requires review",
                delay_seconds=300,
            )
        return True

    def fresh(self):
        with self.sales.ops.transaction() as cur:
            if not self._actions(cur):
                return True
            last = self._last(cur)
            if not last or last["status"] != "completed" or not (last["result"] or {}).get("final"):
                return False
            through = datetime.fromisoformat(last["result"]["through"])
            return datetime.now(UTC) - timedelta(minutes=2) <= through <= datetime.now(UTC)

    async def ensure_fresh(self):
        for _ in range(5):
            if await asyncio.to_thread(self.fresh):
                return
            if not await self.tick():
                break
        if not await asyncio.to_thread(self.fresh):
            raise Conflict("Complete fresh Gmail delivery-report coverage before another send")
