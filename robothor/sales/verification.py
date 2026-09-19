"""Paid email verification, started once and polled without another purchase."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from psycopg2.extras import Json

from robothor.operations.effects import Effects
from robothor.operations.store import BudgetExceeded, Conflict
from robothor.sales.models import SalesSettings
from robothor.sales.providers import Instantly, ProviderError
from robothor.sales.runtime import ResearchWorker


class VerificationWorker:
    def __init__(self, sales, provider=None):
        self.sales = sales
        self.provider = provider or Instantly(sales.tenant)
        self.effects = Effects(sales.tenant)

    async def tick(self):
        settings = SalesSettings.model_validate(await asyncio.to_thread(self.sales.settings))
        if not settings.enrichment_enabled:
            return False
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.verify", lease_seconds=120)
        if not job:
            return False
        try:
            if not settings.verification_allowance_units:
                raise BudgetExceeded("An approved verification cost allowance is required")
            await asyncio.to_thread(self._check, job)
            existing = await asyncio.to_thread(self._existing, job)
            reservations = []
            if existing is None:
                budget = ResearchWorker(self.sales)
                budget.RUN_ALLOWANCE_UNITS = settings.verification_allowance_units
                reservations = await asyncio.to_thread(budget._reserve, settings, job)

            async def start():
                response = await self.provider.start_verification(job["payload"]["email"])
                self._validate(response, job["payload"]["email"])
                # Email is this provider resource's documented lookup identity.
                return {
                    "id": response["email"],
                    "reservations": reservations,
                    "allowance_units": settings.verification_allowance_units,
                }

            receipt = await self.effects.perform(
                "instantly.verify", str(job["id"]), {"email": job["payload"]["email"]}, start
            )
            # Conservatively book the full operator-approved cost per lookup.
            # Raw provider credits are not USD and never silently treated as USD.
            await asyncio.to_thread(
                self.sales.ops.settle_many, receipt["reservations"], receipt["allowance_units"]
            )
            response = await self.provider.verification(job["payload"]["email"])
            self._validate(response, job["payload"]["email"])
            status = response["verification_status"]
            if status == "pending" or (
                status == "verified" and type(response.get("catch_all")) is not bool
            ):
                await asyncio.to_thread(
                    self.sales.ops.defer,
                    job["id"],
                    job["lease_token"],
                    "Verification pending",
                    delay_seconds=60,
                    busy=True,
                )
                return True
            result = (
                "invalid"
                if status == "invalid"
                else "accept_all"
                if response["catch_all"]
                else "valid"
            )
            await asyncio.to_thread(self._commit, job, result)
        except (Conflict, BudgetExceeded, ProviderError) as exc:
            await asyncio.to_thread(
                self.sales.ops.defer, job["id"], job["lease_token"], str(exc), delay_seconds=300
            )
        return True

    @staticmethod
    def _validate(response, email):
        if response.get("email") != email or response.get("verification_status") not in {
            "pending",
            "verified",
            "invalid",
        }:
            raise ProviderError("Verification identity or status did not match the request")

    def _existing(self, job):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT status FROM operation_effects WHERE tenant_id=%s AND kind='instantly.verify' AND dedup_key=%s",
                (self.sales.tenant, str(job["id"])),
            )
            return cur.fetchone()

    def _check(self, job, cur=None):
        if cur is None:
            with self.sales.ops.transaction() as cursor:
                return self._check(job, cursor)
        p = self.sales.require(job["payload"]["prospect_id"], cur)
        if (p["qualification"] or {}).get("decision") != "qualified":
            raise Conflict("Verification requires a qualified business")
        if self.sales._suppressed(job["payload"]["email"], cur):
            raise Conflict("Contact is suppressed")
        cur.execute(
            "SELECT data FROM sales_contacts WHERE tenant_id=%s AND prospect_id=%s AND id=%s AND email=%s FOR UPDATE",
            (self.sales.tenant, p["id"], job["payload"]["contact_id"], job["payload"]["email"]),
        )
        row = cur.fetchone()
        if not row:
            raise Conflict("Verification contact identity changed")
        return p, row["data"]

    def _commit(self, job, verification):
        with self.sales.ops.transaction() as cur:
            p, contact = self._check(job, cur)
            contact.update(verification=verification, verified_at=datetime.now(UTC).isoformat())
            cur.execute(
                "UPDATE sales_contacts SET data=%s WHERE tenant_id=%s AND id=%s",
                (Json(contact), self.sales.tenant, job["payload"]["contact_id"]),
            )
            self.sales.ops.complete(
                job["id"], job["lease_token"], {"verification": verification}, cur=cur
            )
            self.sales.ops.audit(
                cur,
                job["payload"]["contact_id"],
                "contact.verified",
                detail={"verification": verification},
            )
            if verification == "valid" and p["status"] == "promoted":
                self.sales.ops.enqueue(
                    "sales.draft",
                    str(p["id"]) + ":" + str(p["version"]),
                    {"prospect_id": str(p["id"])},
                    cur=cur,
                )
