"""Fetch CRM identity evidence, then adopt only the exact human-reviewed packet."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from psycopg2.extras import Json
from pydantic import Field

from robothor.operations.store import Conflict, digest
from robothor.sales.models import Contract
from robothor.sales.providers import Pipedrive
from robothor.sales.service import operator


class IdentitySelection(Contract):
    organization_id: int = Field(gt=0, strict=True)
    person_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(
        default_factory=list, max_length=5
    )
    lead_id: UUID | None = None


class IdentityAdoption(Contract):
    packet_id: UUID
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=10, max_length=2000)


def normalized(value):
    return json.loads(json.dumps(value, default=str))


class IdentityReview:
    def __init__(self, sales, provider=None):
        self.sales, self.tenant = sales, sales.tenant
        self.provider = provider or Pipedrive(self.tenant)

    def _state(self, prospect_id, cur):
        p = self.sales.require(prospect_id, cur)
        cur.execute(
            "SELECT email,data FROM sales_contacts WHERE tenant_id=%s AND prospect_id=%s ORDER BY email",
            (self.tenant, prospect_id),
        )
        contacts = [dict(r) for r in cur.fetchall()]
        return normalized({"prospect": p, "contacts": contacts})

    def state(self, prospect_id):
        with self.sales.ops.transaction() as cur:
            return self._state(prospect_id, cur)

    async def search(self, prospect_id):
        state = await asyncio.to_thread(self.state, prospect_id)
        organizations = await self.provider.search_organizations(state["prospect"]["name"])

        def items(data):
            result = []
            for match in data.get("items", [])[:100]:
                item = match.get("item", {})
                if type(item.get("id")) is int and item["id"] > 0:
                    result.append({"id": item["id"], "name": str(item.get("name", ""))[:300]})
            return result

        people = []
        for contact in state["contacts"][:5]:
            found = await self.provider.search_people(contact["email"])
            people.extend({**p, "searched_email": contact["email"]} for p in items(found))
        return {"organizations": items(organizations), "people": people, "limited": True}

    async def inspect(self, prospect_id, *, organization_id, person_ids, lead_id=None):
        selection = IdentitySelection(
            organization_id=organization_id, person_ids=person_ids, lead_id=lead_id
        )
        state = await asyncio.to_thread(self.state, prospect_id)
        scope = await self.provider.identity_scope()
        self.provider.expected_scope = scope
        organization = await self.provider.organization(selection.organization_id)
        if (
            not isinstance(organization, dict)
            or organization.get("id") != selection.organization_id
            or organization.get("is_deleted") is True
            or organization.get("active_flag") is False
        ):
            raise Conflict("Selected Pipedrive organization is missing or deleted")
        records = {
            "organization": {k: organization.get(k) for k in ("id", "name", "update_time")},
            "people": [],
            "lead": None,
        }
        ids = {"organization_id": selection.organization_id}
        known_emails = {c["email"] for c in state["contacts"]}
        for person_id in sorted(set(selection.person_ids)):
            person = await self.provider.person_record(person_id)
            if (
                not isinstance(person, dict)
                or person.get("id") != person_id
                or person.get("org_id") != selection.organization_id
                or person.get("is_deleted") is True
                or person.get("active_flag") is False
            ):
                raise Conflict("Selected person must belong to the reviewed organization")
            emails = {
                str(e.get("value", "")).lower().strip()
                for e in person.get("emails", [])
                if isinstance(e, dict)
            }
            matching = emails & known_emails
            if len(matching) != 1:
                raise Conflict(
                    "Selected person requires one unambiguous existing Genus contact email"
                )
            email = matching.pop()
            if "person:" + email in ids:
                raise Conflict("Multiple people match the same contact")
            ids["person:" + email] = person_id
            records["people"].append(
                {
                    "id": person_id,
                    "name": str(person.get("name", ""))[:300],
                    "email": email,
                    "organization_id": selection.organization_id,
                    "update_time": person.get("update_time"),
                }
            )
        if selection.lead_id:
            lead = await self.provider.lead(str(selection.lead_id))
            valid_people = set(selection.person_ids) | {
                v for k, v in state["prospect"]["pipedrive_ids"].items() if k.startswith("person:")
            }
            if (
                not isinstance(lead, dict)
                or lead.get("id") != str(selection.lead_id)
                or lead.get("organization_id") != selection.organization_id
                or lead.get("is_deleted") is True
                or lead.get("is_archived") is True
                or (lead.get("person_id") is not None and lead["person_id"] not in valid_people)
            ):
                raise Conflict(
                    "Selected lead must belong to the reviewed organization and contacts"
                )
            ids["lead_id"] = str(selection.lead_id)
            records["lead"] = {
                k: lead.get(k)
                for k in ("id", "title", "organization_id", "person_id", "update_time")
            }
        content = {
            "account_scope": scope,
            "selection": selection.model_dump(mode="json"),
            "state_hash": digest(state),
            "records": normalized(records),
            "ids": ids,
            "observed_at": datetime.now(UTC).isoformat(),
        }
        return await asyncio.to_thread(self._save, prospect_id, content)

    def _save(self, prospect_id, content):
        with self.sales.ops.transaction() as cur:
            if digest(self._state(prospect_id, cur)) != content["state_hash"]:
                raise Conflict("Prospect changed during identity fetch; fetch again")
            job_id = self.sales.ops.enqueue(
                "sales.identity_read", str(uuid4()), {"prospect_id": str(prospect_id)}, cur=cur
            )
            packet = {"id": job_id, "content": content, "content_hash": digest(content)}
            cur.execute(
                "UPDATE operation_jobs SET status='completed',result=%s WHERE tenant_id=%s AND id=%s",
                (Json(packet), self.tenant, job_id),
            )
            return packet

    async def adopt(self, prospect_id, packet_id, expected_hash, actor, reason):
        operator(actor)
        IdentityAdoption(packet_id=packet_id, expected_hash=expected_hash, reason=reason)

        def packet():
            with self.sales.ops.transaction() as cur:
                cur.execute(
                    "SELECT result FROM operation_jobs WHERE tenant_id=%s AND id=%s AND kind='sales.identity_read' AND payload->>'prospect_id'=%s",
                    (self.tenant, str(packet_id), str(prospect_id)),
                )
                row = cur.fetchone()
                if not row or digest(row["result"]["content"]) != expected_hash:
                    raise Conflict("Identity review packet not found or changed")
                return row["result"]["content"]

        content = await asyncio.to_thread(packet)
        fresh = await self.inspect(prospect_id, **content["selection"])
        if any(
            fresh["content"][key] != content[key]
            for key in ("account_scope", "records", "ids", "state_hash")
        ):
            raise Conflict("Provider identity changed since review; fetch and review again")
        return await asyncio.to_thread(
            self._adopt, prospect_id, packet_id, expected_hash, actor, reason
        )

    def _adopt(self, prospect_id, packet_id, expected_hash, actor, reason):
        operator(actor)
        IdentityAdoption(packet_id=packet_id, expected_hash=expected_hash, reason=reason)
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.tenant + ":pipedrive-identities",),
            )
            self.sales.require_request_open(prospect_id, cur=cur)
            state = self._state(prospect_id, cur)
            p = state["prospect"]
            if p["owner"] != "agent" or p["status"] not in {"accepted", "promoted"}:
                raise Conflict("Current human-accepted prospect under agent ownership required")
            self.sales.require_assessment(prospect_id, cur=cur)
            cur.execute(
                "SELECT result FROM operation_jobs WHERE tenant_id=%s AND id=%s AND kind='sales.identity_read' AND payload->>'prospect_id'=%s AND status='completed' FOR UPDATE",
                (self.tenant, str(packet_id), str(prospect_id)),
            )
            row = cur.fetchone()
            if not row:
                raise Conflict("Identity review packet not found")
            packet = row["result"]
            content = packet["content"]
            if digest(content) != expected_hash or digest(state) != content["state_hash"]:
                raise Conflict("Identity review changed; fetch again")
            if datetime.fromisoformat(content["observed_at"]) < datetime.now(UTC) - timedelta(
                minutes=15
            ):
                raise Conflict("Identity evidence expired; fetch again")
            cur.execute(
                "SELECT 1 FROM operation_jobs WHERE tenant_id=%s AND kind='sales.promote' AND payload->>'prospect_id'=%s AND status='running'",
                (self.tenant, str(prospect_id)),
            )
            if cur.fetchone():
                raise Conflict("Wait for running promotion before adopting identities")
            if (
                p.get("pipedrive_account_scope")
                and p["pipedrive_account_scope"] != content["account_scope"]
            ):
                raise Conflict("CRM account differs from the original identity namespace")
            ids = dict(p["pipedrive_ids"])
            for key, value in content["ids"].items():
                if key in ids and ids[key] != value:
                    raise Conflict("An existing CRM binding cannot be silently replaced")
                # Do not bind the same remote identity to another Genus company.
                cur.execute(
                    "SELECT id FROM sales_prospects WHERE tenant_id=%s AND id<>%s AND pipedrive_ids->>%s=%s LIMIT 1",
                    (self.tenant, prospect_id, key, str(value)),
                )
                if cur.fetchone():
                    raise Conflict("CRM identity is already bound to a different Genus company")
                ids[key] = value
            self._receipts(cur, prospect_id, ids, actor, reason)
            cur.execute(
                "UPDATE sales_prospects SET pipedrive_ids=%s,pipedrive_account_scope=%s,updated_at=now() WHERE tenant_id=%s AND id=%s",
                (Json(ids), content["account_scope"], self.tenant, prospect_id),
            )
            cur.execute(
                "UPDATE operation_jobs SET status='failed',error='Superseded by reviewed CRM identity' WHERE tenant_id=%s AND kind='sales.promote' AND payload->>'prospect_id'=%s AND status='pending'",
                (self.tenant, str(prospect_id)),
            )
            self.sales.ops.enqueue(
                "sales.promote",
                "identity:" + str(packet_id),
                {
                    "prospect_id": str(prospect_id),
                    "version": p["version"],
                    "policy_version": p["qualification"]["policy_version"],
                },
                cur=cur,
            )
            self.sales.ops.audit(
                cur,
                prospect_id,
                "pipedrive.identity_adopted",
                actor,
                {
                    "packet_id": str(packet_id),
                    "content_hash": expected_hash,
                    "reason": reason,
                    "ids": ids,
                },
            )
            return ids

    def _receipts(self, cur, prospect_id, ids, actor, reason):
        for key, receipt_id in ids.items():
            kind = (
                "pipedrive.person"
                if key.startswith("person:")
                else "pipedrive.organization"
                if key == "organization_id"
                else "pipedrive.lead"
            )
            dedup = str(prospect_id) + (
                ":" + key.removeprefix("person:") if key.startswith("person:") else ""
            )
            cur.execute(
                "SELECT status,receipt,updated_at FROM operation_effects WHERE tenant_id=%s AND kind=%s AND dedup_key=%s FOR UPDATE",
                (self.tenant, kind, dedup),
            )
            effect = cur.fetchone()
            if not effect:
                continue
            if effect["status"] == "completed":
                if str(effect["receipt"].get("id")) != str(receipt_id):
                    raise Conflict("Fetched identity conflicts with a completed provider receipt")
                continue
            if effect["status"] == "executing" and effect["updated_at"] > datetime.now(
                UTC
            ) - timedelta(minutes=2):
                raise Conflict("Provider write may still be in flight; wait before identity review")
            cur.execute(
                "UPDATE operation_effects SET status='completed',receipt=%s,updated_at=now() WHERE tenant_id=%s AND kind=%s AND dedup_key=%s",
                (Json({"id": receipt_id}), self.tenant, kind, dedup),
            )
            self.sales.ops.audit(
                cur, dedup, "effect.reconciled", actor, {"kind": kind, "reason": reason}
            )
