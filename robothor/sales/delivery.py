"""Human-approved delivery with durable effects and conservative mailbox quotas.

Campaign activation is scheduling, not proof of delivery. Webhook/reconciliation
receipts supply the latter. Unknown writes are held for human reconciliation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from robothor.operations.effects import Effects, UnresolvedEffect
from robothor.operations.store import Conflict
from robothor.sales.models import SalesSettings
from robothor.sales.providers import Instantly, ProviderError


class DeliveryWorker:
    def __init__(self, sales, provider=None, *, clock=None):
        self.sales = sales
        self.provider = provider or Instantly(sales.tenant)
        self.effects = Effects(sales.tenant)
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _window(settings, now):
        local = now.astimezone(ZoneInfo(settings.timezone))
        if local.weekday() > 4 or not 9 <= local.hour < 16:
            raise Conflict("Outside the approved weekday sending window")
        return local

    async def tick(self):
        settings = SalesSettings.model_validate(await asyncio.to_thread(self.sales.settings))
        if not settings.sending_enabled:
            return False
        try:
            self._window(settings, self.clock())
        except Conflict:
            return False
        action = await asyncio.to_thread(
            self.sales.ops.claim_action, kind="sales.email", lease_seconds=300
        )
        if not action:
            return False
        campaign_id = None
        delivery_started = False
        payload = action["payload"]
        key = str(action["id"])
        try:
            settings = await asyncio.to_thread(self.sales.validate_send, action)
            now = self.clock()
            local = self._window(settings, now)
            account = await self.provider.account(payload["sender"])
            self._check_mailbox(settings, payload["sender"], account, now)
            if payload.get("purpose") == "followup":
                workspace = await self.provider.secret("providers/instantly/workspace_id")
                if workspace != payload.get("followup_basis", {}).get("workspace"):
                    raise Conflict("Follow-up workspace changed since reconciliation")
            await asyncio.to_thread(self._reserve_slot, action, local.date())
            if payload.get("reply_to_uuid"):
                await asyncio.to_thread(self.sales.validate_send, action)
                delivery_started = True
                receipt = await self.effects.perform(
                    "instantly.reply", key, payload, lambda: self.provider.reply(payload)
                )
                receipt = {"id": receipt["id"], "delivery_status": "provider_accepted"}
            else:
                # A same-day campaign may queue until 16:00. Authorization must
                # cover that full window; no provider activity past its expiry.
                end = local.replace(hour=16, minute=0, second=0, microsecond=0)
                if action["expires_at"] < end:
                    raise Conflict("Approval expires before the campaign sending window ends")
                campaign = await self.effects.perform(
                    "instantly.campaign",
                    key,
                    {"draft": payload, "date": str(local.date()), "timezone": settings.timezone},
                    lambda: self.provider.create_campaign(
                        key, payload, settings.timezone, str(local.date())
                    ),
                )
                campaign_id = str(campaign["id"])
                await self.effects.perform(
                    "instantly.lead",
                    key,
                    {"campaign_id": campaign_id, "email": payload["recipient"]},
                    lambda: self.provider.add_lead(campaign_id, payload["recipient"]),
                )
                # Review ownership, suppression, conversation and switches again
                # after inert preparation, immediately before activation.
                latest = await asyncio.to_thread(self.sales.validate_send, action)
                self._window(latest, self.clock())
                if latest.timezone != settings.timezone:
                    raise Conflict("Sending timezone changed during preparation")
                delivery_started = True
                await self.effects.perform(
                    "instantly.activate",
                    key,
                    {"campaign_id": campaign_id},
                    lambda: self.provider.activate(campaign_id),
                )
                receipt = {
                    "id": campaign_id,
                    "campaign_id": campaign_id,
                    "delivery_status": "scheduled",
                }
            await asyncio.to_thread(
                self.sales.ops.finish_action,
                action["id"],
                action["lease_token"],
                "completed",
                receipt,
            )
        except (Conflict, ProviderError) as exc:
            status = (
                "unknown" if delivery_started or isinstance(exc, UnresolvedEffect) else "cancelled"
            )
            if campaign_id:
                # Also pause after an uncertain activation: never assume the
                # provider failed to schedule the message just because HTTP failed.
                try:
                    await self.effects.perform(
                        "instantly.pause",
                        key,
                        {"campaign_id": campaign_id},
                        lambda: self.provider.pause(campaign_id),
                    )
                except (Conflict, ProviderError):
                    status = "unknown"
            await asyncio.to_thread(
                self.sales.ops.finish_action,
                action["id"],
                action["lease_token"],
                status,
                {"reason": str(exc), **({"campaign_id": campaign_id} if campaign_id else {})},
            )
        return True

    @staticmethod
    def _check_mailbox(settings, sender, account, now):
        approved_until = settings.mailbox_approved_until.get(sender)
        if not approved_until or not approved_until.tzinfo or approved_until <= now:
            raise Conflict("Current operator mailbox readiness review required")
        if (
            account.get("email") != sender
            or account.get("status") != 1
            or account.get("warmup_status") != 1
            or account.get("setup_pending") is not False
        ):
            raise Conflict("Mailbox is not active and ready")
        score = account.get("stat_warmup_score")
        if type(score) not in (int, float) or not 90 <= score <= 100:
            raise Conflict("Mailbox health is below the pilot threshold or unknown")
        try:
            started = datetime.fromisoformat(account["timestamp_warmup_start"])
        except (KeyError, TypeError, ValueError):
            raise Conflict("Mailbox warmup history required") from None
        if not started.tzinfo or now - started < timedelta(days=14):
            raise Conflict("Mailbox warmup is shorter than the pilot threshold")

    def _reserve_slot(self, action, send_date):
        with self.sales.ops.transaction() as cur:
            settings = self.sales.validate_send(action, cur=cur)
            sender = action["payload"]["sender"]
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"{self.sales.tenant}:mail:{sender}:{send_date}",),
            )
            cur.execute(
                "SELECT 1 FROM sales_send_slots WHERE tenant_id=%s AND action_id=%s",
                (self.sales.tenant, action["id"]),
            )
            if cur.fetchone():
                raise Conflict("This action already reserved a send; reconcile before retrying")
            cur.execute(
                "SELECT count(*) AS n FROM sales_send_slots WHERE tenant_id=%s AND sender=%s AND send_date=%s",
                (self.sales.tenant, sender, send_date),
            )
            if cur.fetchone()["n"] >= settings.mailbox_daily_limit:
                raise Conflict("Mailbox daily approved-send limit reached")
            cur.execute(
                "INSERT INTO sales_send_slots(tenant_id,action_id,sender,send_date) VALUES(%s,%s,%s,%s)",
                (self.sales.tenant, action["id"], sender, send_date),
            )
            self.sales.ops.audit(
                cur, action["id"], "send.slot_reserved", detail={"date": str(send_date)}
            )


class StopWorker:
    """Propagate opt-outs and pauses even while every outbound switch is off."""

    def __init__(self, sales, provider=None):
        self.sales = sales
        self.provider = provider or Instantly(sales.tenant)
        self.effects = Effects(sales.tenant)

    async def tick(self):
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.suppress")
        if not job:
            job = await asyncio.to_thread(self.sales.ops.claim, "sales.stop")
        if not job:
            return False
        try:
            payload = job["payload"]
            if job["kind"] == "sales.suppress":
                await self.effects.perform(
                    "instantly.suppress",
                    payload["email"],
                    {"email": payload["email"]},
                    lambda: self.provider.suppress(payload["email"]),
                )
            campaigns = await asyncio.to_thread(self._campaigns, payload)
            for campaign in campaigns:
                campaign_id = str(campaign["id"])
                await self.effects.perform(
                    "instantly.pause",
                    str(job["id"]) + ":" + campaign_id,
                    {"campaign_id": campaign_id},
                    lambda campaign_id=campaign_id: self.provider.pause(campaign_id),
                )
            await asyncio.to_thread(
                self.sales.ops.complete,
                job["id"],
                job["lease_token"],
                {"paused_campaigns": len(campaigns)},
            )
        except (Conflict, ProviderError) as exc:
            await asyncio.to_thread(
                self.sales.ops.defer, job["id"], job["lease_token"], str(exc), delay_seconds=300
            )
        return True

    def _campaigns(self, payload):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT e.receipt->>'id' AS id FROM operation_effects e "
                "JOIN operation_actions a ON a.tenant_id=e.tenant_id AND a.id::text=e.dedup_key "
                "WHERE e.tenant_id=%s AND e.kind='instantly.campaign' AND e.status='completed' "
                "AND (%s IS NULL OR a.payload->>'prospect_id'=%s) "
                "AND (%s IS NULL OR a.payload->>'sender'=%s) "
                "AND (%s IS NULL OR coalesce((a.payload->>'library_revision')::bigint,0)<=%s) "
                "AND (%s IS NULL OR a.payload->>'recipient'=%s OR '@'||split_part(a.payload->>'recipient','@',2)=%s)",
                (
                    self.sales.tenant,
                    payload.get("prospect_id"),
                    payload.get("prospect_id"),
                    payload.get("sender"),
                    payload.get("sender"),
                    payload.get("library_revision_before"),
                    payload.get("library_revision_before"),
                    payload.get("email"),
                    payload.get("email"),
                    payload.get("email"),
                ),
            )
            return [dict(r) for r in cur.fetchall()]
