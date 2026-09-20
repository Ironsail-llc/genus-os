"""Direct Gmail delivery through the same approved-action and send-slot ledger.

This worker supplies transport, not recipient-delivery proof. Owned conversations
are rescanned before preparation and the final authorization callback.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from robothor.operations.effects import Effects, UnresolvedEffect
from robothor.operations.store import Conflict
from robothor.sales.delivery import DeliveryWorker
from robothor.sales.gmail import Gmail, GmailMailboxError
from robothor.sales.models import SalesSettings
from robothor.sales.providers import ProviderError


class GmailDeliveryWorker:
    _window = staticmethod(DeliveryWorker._window)
    _reserve_slot: Any = DeliveryWorker._reserve_slot

    def __init__(self, sales, provider=None, *, clock=None):
        self.sales = sales
        self.provider = provider or Gmail(sales.tenant)
        self.effects = Effects(sales.tenant)
        self.clock = clock or (lambda: datetime.now(UTC))

    async def _authorize(self, action):
        settings = await asyncio.to_thread(self.sales.validate_send, action)
        if settings.email_provider != "gmail":
            raise Conflict("Gmail is no longer the selected sales provider")
        now = self.clock()
        local = self._window(settings, now)
        sender = action["payload"]["sender"]
        if sender != self.provider.mailbox:
            raise Conflict("Approved sender differs from Gmail account")
        expiry = settings.mailbox_approved_until.get(sender)
        if not expiry or expiry <= now:
            raise Conflict("Current operator mailbox readiness review required")
        # Gmail has no Instantly warmup score. Never fabricate one from OAuth
        # success: operator review and the existing conservative quota apply.
        return settings, local

    async def _before_send(self, action):
        from robothor.sales.gmail_bounces import GmailBounceWorker
        from robothor.sales.gmail_sync import GmailThreadWorker

        await GmailBounceWorker(self.sales, self.provider).ensure_fresh()

        await GmailThreadWorker(self.sales, self.provider).sync_prospect(
            action["payload"]["prospect_id"], exclude_action=action["id"]
        )
        return await self._authorize(action)

    def _finish(self, action, token, status, receipt):
        # A delivery report may arrive while the network acknowledgement is in
        # flight. Preserve that observation under the same lock as completion.
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT receipt FROM operation_actions WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (self.sales.tenant, action),
            )
            row = cur.fetchone()
            failure = (row["receipt"] or {}).get("delivery_failure") if row else None
            if failure and failure.get("action") in {"failed", "delayed"}:
                receipt = {
                    **receipt,
                    "delivery_failure": failure,
                    "delivery_status": "bounced"
                    if failure["action"] == "failed"
                    else "delivery_delayed",
                }
            self.sales.ops.finish_action(action, token, status, receipt, cur=cur)

    async def tick(self):
        settings = SalesSettings.model_validate(await asyncio.to_thread(self.sales.settings))
        if settings.email_provider != "gmail" or not settings.sending_enabled:
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
        started = False
        try:
            _, local = await self._before_send(action)
            prepared = await self.provider.prepare(str(action["id"]), action["payload"])
            await self._authorize(action)
            await asyncio.to_thread(self._reserve_slot, action, local.date())
            await self.provider.profile()
            await self._before_send(action)
            # The ledger is durable before a provider call. Cancellation/process
            # death never makes this action eligible for an automatic retry.
            started = True
            receipt = await self.effects.perform(
                "gmail.send",
                str(action["id"]),
                action["payload"],
                lambda: self.provider.submit(prepared),
            )
            await asyncio.to_thread(
                self._finish,
                action["id"],
                action["lease_token"],
                "completed",
                receipt,
            )
        except (Conflict, ProviderError, ValueError) as exc:
            if isinstance(exc, GmailMailboxError) or (
                started and isinstance(exc, UnresolvedEffect)
            ):
                from robothor.sales.gmail_controls import revoke_gmail

                await asyncio.to_thread(revoke_gmail, self.sales, action["payload"]["sender"])
            status = "unknown" if started or isinstance(exc, UnresolvedEffect) else "cancelled"
            await asyncio.to_thread(
                self._finish,
                action["id"],
                action["lease_token"],
                status,
                {
                    "provider": "gmail",
                    "reason": "Gmail action requires reconciliation"
                    if status == "unknown"
                    else "Gmail preflight failed; review before creating another approval",
                },
            )
        return True
