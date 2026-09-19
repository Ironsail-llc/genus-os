"""Recover missing campaign messages through finite, restartable scans.

Each native tick reads one page. The durable page chain fixes its upper bound
and advances only in the transaction that records every verified message.
A failed page holds that campaign for review rather than skipping its data.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from robothor.operations.store import Conflict, digest
from robothor.sales.ingestion import _canonical, _owned, record_provider_message
from robothor.sales.providers import Instantly, ProviderError, RateLimited


class ReconciliationWorker:
    def __init__(self, sales, provider=None):
        self.sales = sales
        self.provider = provider or Instantly(sales.tenant)

    def _plan(self):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.sales.tenant + ":reconcile-plan",),
            )
            cur.execute(
                "SELECT a.id,a.created_at,a.payload,e.receipt->>'id' AS campaign_id "
                "FROM operation_effects e JOIN operation_actions a ON a.tenant_id=e.tenant_id AND a.id::text=e.dedup_key "
                "WHERE e.tenant_id=%s AND e.kind='instantly.campaign' AND e.status='completed' "
                "AND a.kind='sales.email' ORDER BY a.created_at,a.id",
                (self.sales.tenant,),
            )
            campaigns = list(cur.fetchall())
            now = datetime.now(UTC)
            for campaign in campaigns:
                campaign_id = campaign["campaign_id"]
                if not campaign_id:
                    continue
                cur.execute(
                    "SELECT status,result,payload FROM operation_jobs WHERE tenant_id=%s AND kind='sales.reconcile' "
                    "AND payload->>'campaign_id'=%s ORDER BY created_at DESC LIMIT 1",
                    (self.sales.tenant, campaign_id),
                )
                last = cur.fetchone()
                if last and (
                    last["status"] != "completed" or not (last["result"] or {}).get("final")
                ):
                    continue
                through = datetime.fromisoformat(last["result"]["through"]) if last else None
                if through and through > now - timedelta(minutes=10):
                    continue
                # Revisit a day of overlap: delayed indexing and equal timestamps
                # are resolved by provider IDs, never by dropping the boundary.
                full_at = (
                    datetime.fromisoformat(last["result"]["full_at"])
                    if last and last["result"].get("full_at")
                    else None
                )
                full = full_at is None or full_at <= now - timedelta(days=1)
                start = campaign["created_at"] - timedelta(minutes=5)
                if not full and through:
                    start = max(start, through - timedelta(days=1))
                payload = {
                    "campaign_id": campaign_id,
                    "action_id": str(campaign["id"]),
                    "after": start.isoformat(),
                    "through": now.isoformat(),
                    "cursor": None,
                    "full_at": (now if full else full_at).isoformat(),
                    "workspace": last["result"].get("workspace") if last else None,
                    "seen_cursors": [],
                }
                self.sales.ops.enqueue("sales.reconcile", digest(payload), payload, cur=cur)

    def _action(self, job):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT * FROM operation_actions WHERE tenant_id=%s AND id=%s AND kind='sales.email'",
                (self.sales.tenant, job["payload"]["action_id"]),
            )
            row = cur.fetchone()
            if not row:
                raise Conflict("Reconciliation action missing")
            return dict(row)

    async def tick(self):
        await asyncio.to_thread(self._plan)
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.reconcile", lease_seconds=120)
        if not job:
            return False
        try:
            workspace = await self.provider.secret("providers/instantly/workspace_id")
            action = await asyncio.to_thread(self._action, job)
            payload = job["payload"]
            bound_workspace = payload.get("workspace") or (job.get("result") or {}).get("workspace")
            if bound_workspace and bound_workspace != workspace:
                raise Conflict("Reconciliation workspace changed; operator review required")
            await asyncio.to_thread(
                self.sales.ops.checkpoint, job["id"], job["lease_token"], {"workspace": workspace}
            )
            page = await self.provider.emails(
                campaign_id=payload["campaign_id"],
                cursor=payload["cursor"],
                min_timestamp_created=payload["after"],
                max_timestamp_created=payload["through"],
                sort_order="asc",
                latest_of_thread=False,
                workspace_id=workspace,
            )
            if (
                not isinstance(page, dict)
                or not isinstance(page.get("items"), list)
                or len(page["items"]) > 100
            ):
                raise Conflict("Invalid provider page")
            cursor = page.get("next_starting_after")
            if cursor is not None and (
                not isinstance(cursor, str)
                or not cursor
                or len(cursor) > 200
                or cursor in payload["seen_cursors"]
            ):
                raise Conflict("Invalid or repeated provider cursor")
            if cursor and not page["items"]:
                raise Conflict("Empty provider page cannot advance")
            messages = []
            for record in page["items"]:
                if not isinstance(record, dict):
                    raise Conflict("Invalid provider message")
                kind = (
                    "auto_reply_received"
                    if record.get("ue_type") == 2 and record.get("is_auto_reply") == 1
                    else "reply_received"
                    if record.get("ue_type") == 2
                    else "email_sent"
                )
                event = {
                    "kind": kind,
                    "workspace": workspace,
                    "campaign_id": payload["campaign_id"],
                    "provider_id": record.get("id"),
                }
                message = _canonical(record, event, action)
                observed = datetime.fromisoformat(message["occurred_at"])
                if (
                    not datetime.fromisoformat(payload["after"])
                    <= observed
                    <= datetime.fromisoformat(payload["through"])
                ):
                    raise Conflict("Provider message falls outside the requested scan window")
                messages.append(message)
            await asyncio.to_thread(self._commit, job, action, messages, cursor, workspace)
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
            await asyncio.to_thread(
                self.sales.ops.defer,
                job["id"],
                job["lease_token"],
                "Provider reconciliation page requires review",
                delay_seconds=300,
            )
        return True

    async def drain(self):
        """At most five pages per scheduled invocation; provider limiter is shared."""
        worked = False
        for _ in range(5):
            if not await self.tick():
                break
            worked = True
        return worked

    def _commit(self, job, action, messages, cursor, workspace):
        payload = job["payload"]
        with self.sales.ops.transaction() as cur:
            # Bind the campaign even on an empty page, so an empty result cannot
            # silently advance a stale/mismatched association.
            current = _owned(
                self.sales, {"campaign_id": payload["campaign_id"], "kind": "email_sent"}, cur
            )
            if not current or current["id"] != action["id"]:
                raise Conflict("Reconciliation campaign association changed")
            for message in messages:
                created = record_provider_message(self.sales, action, message, cur)
                event_id = "instantly:poll:" + message["provider_id"]
                self.sales.ops.receive("instantly", event_id, message, cur=cur)
                cur.execute(
                    "UPDATE operation_inbox SET processed_at=now() WHERE tenant_id=%s AND provider='instantly' AND event_id=%s",
                    (self.sales.tenant, event_id),
                )
                if created and message["direction"] == "inbound":
                    self.sales.ops.enqueue(
                        "sales.stop", event_id, {"prospect_id": message["prospect_id"]}, cur=cur
                    )
                if created:
                    self.sales.ops.audit(cur, message["provider_id"], "message.reconciled")
            if cursor:
                if len(payload["seen_cursors"]) >= 1000:
                    raise Conflict("Reconciliation page bound reached; operator review required")
                next_page = {
                    **payload,
                    "workspace": workspace,
                    "cursor": cursor,
                    "seen_cursors": [*payload["seen_cursors"], cursor],
                }
                self.sales.ops.enqueue("sales.reconcile", digest(next_page), next_page, cur=cur)
            self.sales.ops.complete(
                job["id"],
                job["lease_token"],
                {
                    "final": cursor is None,
                    "through": payload["through"],
                    "full_at": payload["full_at"],
                    "workspace": workspace,
                    "messages": len(messages),
                },
                cur=cur,
            )
