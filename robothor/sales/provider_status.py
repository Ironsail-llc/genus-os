"""Bounded canonical account/lead reads; recover safety signals without message bodies."""

import asyncio
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from psycopg2.extras import Json

from robothor.operations.store import Conflict, digest
from robothor.sales.delivery import DeliveryWorker
from robothor.sales.models import SalesSettings
from robothor.sales.providers import Instantly, ProviderError, RateLimited


def revoke_mailbox(sales, sender, reason, cur):
    cur.execute("SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE", (sales.tenant,))
    row = cur.fetchone()
    if not row or sender not in row["config"].get("senders", []):
        return
    config = row["config"]
    if sender not in config.get("mailbox_approved_until", {}):
        return
    config.setdefault("mailbox_approved_until", {}).pop(sender, None)
    cur.execute(
        "UPDATE sales_settings SET config=%s,revision=revision+1,updated_at=now() WHERE tenant_id=%s",
        (Json(config), sales.tenant),
    )
    cur.execute(
        "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s AND kind='sales.email' AND payload->>'sender'=%s AND status IN ('review','approved')",
        (sales.tenant, sender),
    )
    sales.ops.enqueue("sales.stop", str(uuid4()), {"sender": sender}, cur=cur)
    sales.ops.audit(cur, sender, "mailbox.readiness_revoked", detail={"reason": reason})


class ProviderStatusWorker:
    def __init__(self, sales, provider=None):
        self.sales, self.tenant = sales, sales.tenant
        self.provider = provider or Instantly(self.tenant)

    def _plan(self):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.tenant + ":provider-status",),
            )
            settings = SalesSettings.model_validate(self.sales.settings())
            now = datetime.now(UTC)
            planned = 0

            def enqueue(scope, identity, payload):
                nonlocal planned
                if planned >= 50:
                    return
                cur.execute(
                    "SELECT status,updated_at FROM operation_jobs WHERE tenant_id=%s AND kind='sales.provider_status' AND payload->>'identity'=%s ORDER BY created_at DESC,id DESC LIMIT 1",
                    (self.tenant, identity),
                )
                previous = cur.fetchone()
                if previous and (
                    previous["status"] != "completed"
                    or previous["updated_at"] > now - timedelta(minutes=10)
                ):
                    return
                self.sales.ops.enqueue(
                    "sales.provider_status",
                    digest([identity, now.isoformat()]),
                    {"scope": scope, "identity": identity, **payload},
                    cur=cur,
                )
                planned += 1

            if settings.sending_enabled or settings.enrichment_enabled:
                for sender in settings.senders:
                    enqueue("mailbox", "mailbox:" + sender, {"sender": sender})
            cur.execute(
                "SELECT a.id,a.payload,c.receipt->>'id' AS campaign_id,l.receipt->>'id' AS lead_id FROM operation_actions a JOIN operation_effects c ON c.tenant_id=a.tenant_id AND c.dedup_key=a.id::text AND c.kind='instantly.campaign' AND c.status='completed' JOIN operation_effects l ON l.tenant_id=a.tenant_id AND l.dedup_key=a.id::text AND l.kind='instantly.lead' AND l.status='completed' JOIN sales_prospects p ON p.tenant_id=a.tenant_id AND p.id::text=a.payload->>'prospect_id' LEFT JOIN LATERAL(SELECT status,updated_at FROM operation_jobs j WHERE j.tenant_id=a.tenant_id AND j.kind='sales.provider_status' AND j.payload->>'identity'='lead:'||(l.receipt->>'id') ORDER BY created_at DESC,id DESC LIMIT 1) last ON true WHERE a.tenant_id=%s AND a.kind='sales.email' AND p.owner='agent' AND a.created_at>now()-interval '90 days' AND (last.status IS NULL OR (last.status='completed' AND last.updated_at<now()-interval '10 minutes')) ORDER BY last.updated_at NULLS FIRST,a.created_at,a.id LIMIT 100",
                (self.tenant,),
            )
            campaigns = [dict(r) for r in cur.fetchall()]
            for campaign in campaigns:
                email = campaign["payload"]["recipient"]
                if self.sales._suppressed(email, cur):
                    continue
                enqueue(
                    "lead",
                    "lead:" + campaign["lead_id"],
                    {
                        "action_id": str(campaign["id"]),
                        "prospect_id": campaign["payload"]["prospect_id"],
                        "email": email,
                        "campaign_id": campaign["campaign_id"],
                        "lead_id": campaign["lead_id"],
                    },
                )
            return planned

    async def tick(self):
        await asyncio.to_thread(self._plan)
        job = await asyncio.to_thread(
            self.sales.ops.claim, "sales.provider_status", lease_seconds=120
        )
        if not job:
            return False
        try:
            allowed = await asyncio.to_thread(
                self.sales.ops.admit_request, "instantly:status", limit=20, window_seconds=60
            )
            if not allowed:
                raise RateLimited(60)
            workspace = await self.provider.secret("providers/instantly/workspace_id")
            previous = job.get("result") or {}
            if previous.get("workspace") and previous["workspace"] != workspace:
                raise Conflict("Provider status workspace changed; review configuration")
            await asyncio.to_thread(
                self.sales.ops.checkpoint, job["id"], job["lease_token"], {"workspace": workspace}
            )
            payload = job["payload"]
            record = (
                await self.provider.account(payload["sender"])
                if payload["scope"] == "mailbox"
                else await self.provider.lead(payload["lead_id"])
            )
            await asyncio.to_thread(self._commit, job, record, workspace)
        except (Conflict, ProviderError) as exc:
            with suppress(Conflict):
                await asyncio.to_thread(
                    self.sales.ops.defer,
                    job["id"],
                    job["lease_token"],
                    str(exc),
                    delay_seconds=exc.retry_after if isinstance(exc, RateLimited) else 300,
                    busy=isinstance(exc, RateLimited),
                )
        return True

    async def drain(self):
        deadline = time.monotonic() + 60
        worked = False
        for _ in range(10):
            if time.monotonic() >= deadline or not await self.tick():
                break
            worked = True
        return worked

    def _commit(self, job, record, workspace):
        payload = job["payload"]
        if not isinstance(record, dict):
            raise Conflict("Invalid provider status response")
        with self.sales.ops.transaction() as cur:
            result = {
                "scope": payload["scope"],
                "workspace": workspace,
                "observed_at": datetime.now(UTC).isoformat(),
            }
            if payload["scope"] == "mailbox":
                if record.get("email") != payload["sender"]:
                    raise Conflict("Mailbox status identity mismatch")
                cur.execute(
                    "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE",
                    (self.tenant,),
                )
                settings = SalesSettings.model_validate(cur.fetchone()["config"])
                if payload["sender"] not in settings.senders:
                    result["status"] = "sender_removed"
                else:
                    try:
                        DeliveryWorker._check_mailbox(
                            settings, payload["sender"], record, datetime.now(UTC)
                        )
                        result["status"] = "ready"
                    except Conflict as exc:
                        result["status"] = "readiness_required"
                        result["reason"] = str(exc)
                        revoke_mailbox(self.sales, payload["sender"], str(exc), cur)
                result["sender"] = payload["sender"]
            else:
                result.update(self._observe_lead(cur, payload, record, workspace))
            self.sales.ops.complete(job["id"], job["lease_token"], result, cur=cur)
            self.sales.ops.audit(
                cur,
                job["id"],
                "provider.status_observed",
                detail={"scope": payload["scope"], "status": result["status"]},
            )

    async def check_before_send(self, action):
        draft = action["payload"]

        def owned():
            with self.sales.ops.transaction() as cur:
                cur.execute(
                    "SELECT a.id,c.receipt->>'id' AS campaign_id,l.receipt->>'id' AS lead_id FROM operation_actions a JOIN operation_effects c ON c.tenant_id=a.tenant_id AND c.dedup_key=a.id::text AND c.kind='instantly.campaign' AND c.status='completed' JOIN operation_effects l ON l.tenant_id=a.tenant_id AND l.dedup_key=a.id::text AND l.kind='instantly.lead' AND l.status='completed' WHERE a.tenant_id=%s AND a.kind='sales.email' AND a.payload->>'prospect_id'=%s AND a.payload->>'recipient'=%s ORDER BY a.created_at DESC,a.id DESC LIMIT 1",
                    (self.tenant, draft["prospect_id"], draft["recipient"]),
                )
                row = cur.fetchone()
                return dict(row) if row else None

        scope = await asyncio.to_thread(owned)
        if not scope:
            return
        allowed = await asyncio.to_thread(
            self.sales.ops.admit_request, "instantly:status", limit=20, window_seconds=60
        )
        if not allowed:
            raise RateLimited(60)
        workspace = await self.provider.secret("providers/instantly/workspace_id")
        record = await self.provider.lead(scope["lead_id"])
        payload = {**scope, "prospect_id": draft["prospect_id"], "email": draft["recipient"]}

        def apply():
            with self.sales.ops.transaction() as cur:
                result = self._observe_lead(cur, payload, record, workspace)
                self.sales.ops.audit(
                    cur, action["id"], "delivery.provider_status_checked", detail=result
                )
                return result

        result = await asyncio.to_thread(apply)
        if result["status"] != "no_negative_signal":
            raise Conflict("Provider lead status stopped this delivery; human review required")

    def _observe_lead(self, cur, payload, record, workspace):
        if not isinstance(record, dict):
            raise Conflict("Invalid provider lead status response")
        result: dict[str, Any] = {}
        if (
            record.get("id") != payload["lead_id"]
            or record.get("campaign") != payload["campaign_id"]
            or record.get("email") != payload["email"]
            or record.get("organization") != workspace
        ):
            raise Conflict("Lead status workspace or owned identity mismatch")
        status: Any = record.get("status")
        interest: Any = record.get("lt_interest_status")
        if (
            type(status) is not int
            or status not in {1, 2, 3, -1, -2, -3}
            or (interest is not None and type(interest) is not int)
        ):
            raise Conflict("Unknown provider status contract")
        if status in {-1, -2} or interest in {-1, -2}:
            # `interest` is a validated int whenever `status` is not itself
            # the negative signal — the guard above refuses any other shape.
            negative: dict[Any, str] = {-1: "not_interested", -2: "wrong_person"}
            reason = {-1: "bounced", -2: "unsubscribed"}.get(status) or negative[interest]
            self.sales.suppress(
                payload["email"], "provider_status:" + reason, "service:provider-status", cur=cur
            )
            result["status"] = "suppressed"
        elif status in {2, -3} or interest not in {None, 1}:
            self.sales.escalate(
                payload["prospect_id"],
                "Provider lead status requires human review; no business fulfillment is inferred",
                cur=cur,
            )
            result["status"] = "human_review"
        else:
            result["status"] = "no_negative_signal"
        result.update(
            lead_id=payload["lead_id"],
            campaign_id=payload["campaign_id"],
            provider_status=status,
            interest_status=interest,
        )
        return result
