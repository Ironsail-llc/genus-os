"""Durable Pipedrive promotion after human dossier acceptance."""

from __future__ import annotations

import asyncio

from psycopg2.extras import Json

from robothor.operations.effects import Effects
from robothor.operations.store import Conflict
from robothor.sales.providers import Pipedrive, ProviderError


class PromotionWorker:
    def __init__(self, sales, provider=None):
        self.sales = sales
        self.provider = provider or Pipedrive(sales.tenant)
        self.effects = Effects(sales.tenant)

    async def tick(self):
        settings = await asyncio.to_thread(self.sales.settings)
        if settings.get("promotion_enabled") is not True:
            return False
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.promote")
        if not job:
            return False
        try:
            await self.promote(job)
        except (Conflict, ProviderError) as exc:
            await asyncio.to_thread(
                self.sales.ops.defer, job["id"], job["lease_token"], str(exc), delay_seconds=3600
            )
        return True

    async def promote(self, job):
        prospect_id = job["payload"]["prospect_id"]
        p = await asyncio.to_thread(self.sales.get, prospect_id)
        if not p or p["status"] not in {"accepted", "promoted"}:
            raise Conflict("Human acceptance required before Pipedrive promotion")
        if p["version"] != job["payload"].get("version") or (p["qualification"] or {}).get(
            "policy_version"
        ) != job["payload"].get("policy_version"):
            raise Conflict("Queued promotion does not match the reviewed dossier")
        version = p["version"]
        known = dict(p["pipedrive_ids"])

        async def organization():
            matches = await self.provider.search_organizations(p["name"])
            if matches.get("items"):
                raise Conflict("Possible existing Pipedrive organization; identity review required")
            return await self.provider.create_organization(p["name"])

        if "organization_id" not in known:
            receipt = await self.effects.perform(
                "pipedrive.organization", str(prospect_id), {"name": p["name"]}, organization
            )
            known["organization_id"] = receipt["id"]
        contacts = await asyncio.to_thread(self.sales.contacts, prospect_id)
        for contact in contacts:
            data = contact["data"]
            key = "person:" + contact["email"]
            if key in known:
                continue

            async def person(data=data):
                matches = await self.provider.search_people(data["email"])
                # Email identity conflicts require a human to confirm the
                # organization relationship; never silently reparent people.
                if matches.get("items"):
                    raise Conflict("Possible existing Pipedrive person; identity review required")
                return await self.provider.create_person(
                    data["name"], data["email"], known["organization_id"]
                )

            receipt = await self.effects.perform(
                "pipedrive.person",
                str(prospect_id) + ":" + contact["email"],
                {
                    "name": data["name"],
                    "email": data["email"],
                    "organization_id": known["organization_id"],
                },
                person,
            )
            known[key] = receipt["id"]
        if "lead_id" not in known:
            person_id = next((v for k, v in known.items() if k.startswith("person:")), None)
            title = p["name"] + " — Genus " + str(prospect_id)
            receipt = await self.effects.perform(
                "pipedrive.lead",
                str(prospect_id),
                {
                    "title": title,
                    "organization_id": known["organization_id"],
                    "person_id": person_id,
                },
                lambda: self.provider.create_lead(title, known["organization_id"], person_id),
            )
            known["lead_id"] = receipt["id"]
        await asyncio.to_thread(self._complete, job, prospect_id, version, known)

    def _complete(self, job, prospect_id, version, known):
        with self.sales.ops.transaction() as cur:
            p = self.sales.require(prospect_id, cur)
            if p["version"] != version or p["status"] not in {"accepted", "promoted"}:
                raise Conflict(
                    "Prospect changed during promotion; receipts retained for reconciliation"
                )
            cur.execute(
                "UPDATE sales_prospects SET pipedrive_ids=%s,status='promoted',updated_at=now() WHERE tenant_id=%s AND id=%s",
                (Json(known), self.sales.tenant, prospect_id),
            )
            self.sales.ops.complete(job["id"], job["lease_token"], known, cur=cur)
            self.sales.ops.enqueue(
                "sales.draft",
                str(prospect_id) + ":" + str(version),
                {"prospect_id": str(prospect_id)},
                cur=cur,
            )
            self.sales.ops.audit(cur, prospect_id, "prospect.promoted", detail=known)
