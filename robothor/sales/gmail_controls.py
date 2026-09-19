"""Gmail mailbox readiness and local cancellation independent of external campaigns."""

import asyncio
from datetime import UTC, datetime

from robothor.operations.store import Conflict, digest
from robothor.sales.gmail import Gmail
from robothor.sales.provider_status import revoke_mailbox
from robothor.sales.providers import ProviderError


def revoke_gmail(sales, sender):
    with sales.ops.transaction() as cur:
        revoke_mailbox(sales, sender, "Gmail connection or write requires operator review", cur)


class GmailStatusWorker:
    def __init__(self, sales, provider=None):
        self.sales, self.provider = sales, provider

    def plan(self):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.sales.tenant + ":gmail-status",),
            )
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s", (self.sales.tenant,)
            )
            row = cur.fetchone()
            config = row["config"] if row else {}
            if config.get("email_provider") != "gmail":
                return
            for sender in config.get("senders", []):
                cur.execute(
                    "SELECT status,updated_at>now()-interval '10 minutes' AS recent FROM operation_jobs WHERE tenant_id=%s AND kind='sales.gmail_status' AND payload->>'sender'=%s ORDER BY created_at DESC,id DESC LIMIT 1",
                    (self.sales.tenant, sender),
                )
                last = cur.fetchone()
                if last and (last["status"] != "completed" or last["recent"]):
                    continue
                self.sales.ops.enqueue(
                    "sales.gmail_status",
                    digest([sender, datetime.now(UTC).isoformat()]),
                    {"sender": sender},
                    cur=cur,
                )

    async def tick(self):
        if (await asyncio.to_thread(self.sales.settings)).get("email_provider") != "gmail":
            return False
        await asyncio.to_thread(self.plan)
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.gmail_status", lease_seconds=120)
        if not job:
            return False
        sender = job["payload"]["sender"]
        try:
            provider = self.provider or Gmail(self.sales.tenant)
            if provider.mailbox != sender:
                raise ProviderError("Gmail sender does not match the bound mailbox")
            await provider.profile()
            await asyncio.to_thread(
                self.sales.ops.complete,
                job["id"],
                job["lease_token"],
                {
                    "provider": "gmail",
                    "sender": sender,
                    "authenticated": True,
                    "readiness_restored": False,
                },
            )
        except ProviderError:
            await asyncio.to_thread(revoke_gmail, self.sales, sender)
            await asyncio.to_thread(
                self.sales.ops.defer,
                job["id"],
                job["lease_token"],
                "Gmail mailbox check failed; operator readiness review revoked",
                delay_seconds=300,
            )
        return True


class GmailStopWorker:
    """Local stops cannot recall an already submitted email or resolve an unknown write."""

    def __init__(self, sales):
        self.sales = sales

    async def tick(self):
        if (await asyncio.to_thread(self.sales.settings)).get("email_provider") != "gmail":
            return False
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.suppress")
        if not job:
            job = await asyncio.to_thread(self.sales.ops.claim, "sales.stop")
        if not job:
            return False
        await asyncio.to_thread(self.complete, job)
        return True

    def complete(self, job):
        p = job["payload"]
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE",
                (self.sales.tenant,),
            )
            settings = cur.fetchone()
            if not settings or settings["config"].get("email_provider") != "gmail":
                raise Conflict("Gmail provider selection changed during stop")
            if (
                not p
                or not p.keys()
                <= {"scope", "email", "sender", "prospect_id", "library_revision_before"}
                or ("scope" in p and p["scope"] != "all")
            ):
                raise Conflict("Gmail stop scope requires review")
            cur.execute(
                "SELECT id,status FROM operation_actions WHERE tenant_id=%s AND kind='sales.email' AND created_at<=%s "
                "AND (%s IS NULL OR payload->>'prospect_id'=%s) AND (%s IS NULL OR payload->>'sender'=%s) "
                "AND (%s IS NULL OR coalesce((payload->>'library_revision')::bigint,0)<=%s) "
                "AND (%s IS NULL OR payload->>'recipient'=%s OR '@'||split_part(payload->>'recipient','@',2)=%s) FOR UPDATE",
                (
                    self.sales.tenant,
                    job["created_at"],
                    p.get("prospect_id"),
                    p.get("prospect_id"),
                    p.get("sender"),
                    p.get("sender"),
                    p.get("library_revision_before"),
                    p.get("library_revision_before"),
                    p.get("email"),
                    p.get("email"),
                    p.get("email"),
                ),
            )
            rows = list(cur.fetchall())
            pending = [str(r["id"]) for r in rows if r["status"] in {"review", "approved"}]
            if pending:
                cur.execute(
                    "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s AND id::text=ANY(%s) AND status IN ('review','approved')",
                    (self.sales.tenant, pending),
                )
            self.sales.ops.complete(
                job["id"],
                job["lease_token"],
                {
                    "cancelled": len(pending),
                    "unresolved_sends": sum(r["status"] in {"unknown", "executing"} for r in rows),
                    "remote_recall_attempted": False,
                },
                cur=cur,
            )
