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
        scope = await self.provider.identity_scope()
        self.provider.expected_scope = scope
        await asyncio.to_thread(self._bind_scope, prospect_id, scope)
        await asyncio.to_thread(self._current, job)
        await asyncio.to_thread(self.sales.require_assessment, prospect_id)
        p = await asyncio.to_thread(self.sales.get, prospect_id)
        if not p or p["status"] not in {"accepted", "promoted"}:
            raise Conflict("Human acceptance required before Pipedrive promotion")
        if p["version"] != job["payload"].get("version") or (p["qualification"] or {}).get(
            "policy_version"
        ) != job["payload"].get("policy_version"):
            raise Conflict("Queued promotion does not match the reviewed dossier")
        version = p["version"]
        known = dict(p["pipedrive_ids"])

        if "organization_id" not in known:
            receipt = await asyncio.to_thread(
                self._receipt, "pipedrive.organization", str(prospect_id)
            )
            if receipt is None:
                matches = await self.provider.search_organizations(p["name"])
                if matches.get("items"):
                    raise Conflict(
                        "Possible existing Pipedrive organization; identity review required"
                    )
                await asyncio.to_thread(self._current, job)
                receipt = await self.effects.perform(
                    "pipedrive.organization",
                    str(prospect_id),
                    {"name": p["name"]},
                    lambda: self.provider.create_organization(p["name"]),
                )
            known["organization_id"] = receipt["id"]
        contacts = await asyncio.to_thread(self.sales.contacts, prospect_id)
        for contact in contacts:
            data = contact["data"]
            key = "person:" + contact["email"]
            if key in known:
                continue

            receipt = await asyncio.to_thread(
                self._receipt, "pipedrive.person", str(prospect_id) + ":" + contact["email"]
            )
            if receipt is None:
                matches = await self.provider.search_people(data["email"])
                if matches.get("items"):
                    raise Conflict("Possible existing Pipedrive person; identity review required")
                await asyncio.to_thread(self._current, job)
                receipt = await self.effects.perform(
                    "pipedrive.person",
                    str(prospect_id) + ":" + contact["email"],
                    {
                        "name": data["name"],
                        "email": data["email"],
                        "organization_id": known["organization_id"],
                    },
                    lambda data=data: self.provider.create_person(
                        data["name"], data["email"], known["organization_id"]
                    ),
                )
            known[key] = receipt["id"]
        if "lead_id" not in known:
            await asyncio.to_thread(self._current, job)
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

    def _bind_scope(self, prospect_id, scope):
        with self.sales.ops.transaction() as cur:
            p = self.sales.require(prospect_id, cur)
            if p["pipedrive_account_scope"] and p["pipedrive_account_scope"] != scope:
                raise Conflict("Pipedrive account differs from the original CRM identity namespace")
            if p["pipedrive_ids"] and not p["pipedrive_account_scope"]:
                raise Conflict("Legacy CRM IDs require a fetched identity review before use")
            cur.execute(
                "UPDATE sales_prospects SET pipedrive_account_scope=%s WHERE tenant_id=%s AND id=%s",
                (scope, self.sales.tenant, prospect_id),
            )

    def _receipt(self, kind, key):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT status,receipt FROM operation_effects WHERE tenant_id=%s AND kind=%s AND dedup_key=%s",
                (self.sales.tenant, kind, key),
            )
            row = cur.fetchone()
            if row and row["status"] != "completed":
                raise Conflict("External effect requires provider reconciliation")
            return row["receipt"] if row else None

    def _current(self, job):
        with self.sales.ops.transaction() as cur:
            self.sales.require_request_open(job["payload"]["prospect_id"], cur=cur)
            p = self.sales.require(job["payload"]["prospect_id"], cur)
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s", (self.sales.tenant,)
            )
            settings = cur.fetchone()["config"]
            if (
                not settings.get("promotion_enabled")
                or p["owner"] != "agent"
                or p["status"] not in {"accepted", "promoted"}
                or p["version"] != job["payload"].get("version")
                or (p["qualification"] or {}).get("policy_version")
                != job["payload"].get("policy_version")
            ):
                raise Conflict("Promotion paused or reviewed prospect changed")
            self.sales.require_assessment(p["id"], cur=cur)

    def _complete(self, job, prospect_id, version, known):
        with self.sales.ops.transaction() as cur:
            self.sales.require_request_open(prospect_id, cur=cur)
            p = self.sales.require(prospect_id, cur)
            if (
                p["version"] != version
                or p["owner"] != "agent"
                or p["status"] not in {"accepted", "promoted"}
            ):
                raise Conflict(
                    "Prospect changed during promotion; receipts retained for reconciliation"
                )
            cur.execute(
                "UPDATE sales_prospects SET pipedrive_ids=%s,status='promoted',updated_at=now() WHERE tenant_id=%s AND id=%s",
                (Json(known), self.sales.tenant, prospect_id),
            )
            self.sales.ops.complete(job["id"], job["lease_token"], known, cur=cur)
            self.sales.ops.audit(cur, prospect_id, "prospect.promoted", detail=known)
