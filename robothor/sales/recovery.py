"""Human-reviewed preparation recovery. Never retries provider writes or approvals."""

import json
from typing import Literal
from uuid import uuid4

from pydantic import Field

from robothor.operations.store import Conflict, digest
from robothor.sales.models import Contract
from robothor.sales.service import operator


class RecoveryChange(Contract):
    command: Literal["resume", "research", "qualify", "contacts", "initial", "reply", "activation"]
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=10, max_length=2000)


class Recovery:
    def __init__(self, sales):
        self.sales, self.tenant = sales, sales.tenant

    def snapshot(self, prospect_id, *, cur=None):
        if cur is None:
            with self.sales.ops.transaction() as cursor:
                return self.snapshot(prospect_id, cur=cursor)
        p = self.sales.require(prospect_id, cur)
        cur.execute(
            "SELECT id,kind,status,updated_at,error FROM operation_jobs WHERE tenant_id=%s AND payload->>'prospect_id'=%s ORDER BY id",
            (self.tenant, str(prospect_id)),
        )
        jobs = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT id,status,payload_hash FROM operation_actions WHERE tenant_id=%s AND kind='sales.email' AND payload->>'prospect_id'=%s ORDER BY id FOR UPDATE",
            (self.tenant, str(prospect_id)),
        )
        actions = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT scope,dedup_key,reserved_units,actual_units FROM operation_reservations WHERE tenant_id=%s AND split_part(dedup_key,':',1)=ANY(%s) ORDER BY scope,dedup_key",
            (self.tenant, [str(j["id"]) for j in jobs]),
        )
        reservations = [dict(r) for r in cur.fetchall()]
        value = {"prospect": p, "jobs": jobs, "actions": actions, "reservations": reservations}
        return {**value, "state_hash": digest(json.loads(json.dumps(value, default=str)))}

    def change(self, prospect_id, command, expected_hash, actor, reason):
        operator(actor)
        RecoveryChange(command=command, expected_hash=expected_hash, reason=reason)
        with self.sales.ops.transaction() as cur:
            self.sales.require_request_open(prospect_id, cur=cur)
            snapshot = self.snapshot(prospect_id, cur=cur)
            if snapshot["state_hash"] != expected_hash:
                raise Conflict("Recovery state changed; reload before deciding")
            p = snapshot["prospect"]
            if any(a["status"] in {"executing", "unknown"} for a in snapshot["actions"]):
                raise Conflict("Reconcile unresolved delivery before recovery")
            # Lock jobs before checking: a worker claim must not slip between the
            # reviewed state and cancellation of superseded preparation work.
            cur.execute(
                "SELECT status FROM operation_jobs WHERE tenant_id=%s AND payload->>'prospect_id'=%s FOR UPDATE",
                (self.tenant, str(prospect_id)),
            )
            if any(r["status"] == "running" for r in cur.fetchall()):
                raise Conflict("Wait for running work to finish or expire before recovery")
            if any(r["actual_units"] is None for r in snapshot["reservations"]):
                raise Conflict(
                    "Unsettled spending must be reconciled before buying replacement work"
                )
            if command == "resume":
                if p["owner"] == "agent":
                    raise Conflict("Prospect is already under agent ownership")
            elif p["owner"] != "agent":
                raise Conflict("Review and resume agent ownership before preparing new work")
            payload = {"prospect_id": str(prospect_id), "version": p["version"]}
            stage = command
            if command in {"contacts", "initial", "reply", "activation"}:
                self.sales.require_assessment(prospect_id, cur=cur)
                if (p["qualification"] or {}).get("decision") != "qualified":
                    raise Conflict("Current qualification required")
            if command in {"initial", "reply", "activation"} and p["status"] not in {
                "accepted",
                "promoted",
                "engaged",
                "onboarding",
                "active",
            }:
                raise Conflict("Human prospect acceptance required")
            if command == "qualify" and not p["dossier"]:
                raise Conflict("Research dossier required before assessment")
            if command == "initial":
                if p["conversation_version"] or p["outcome_version"]:
                    raise Conflict(
                        "Existing conversation or customer outcome requires reply or activation review"
                    )
                stage = "draft"
            if command == "reply":
                cur.execute(
                    "SELECT provider_id,direction FROM sales_messages WHERE tenant_id=%s AND prospect_id=%s ORDER BY occurred_at DESC,provider_id DESC LIMIT 1",
                    (self.tenant, prospect_id),
                )
                message = cur.fetchone()
                if not message or message["direction"] != "inbound":
                    raise Conflict("The latest conversation message must be an inbound reply")
                stage = "conversation"
                payload["provider_id"] = message["provider_id"]
            if command == "activation" and not p["outcome_version"]:
                raise Conflict("Confirmed business outcome required")
            # Never restore an old approval, even when returning to the same owner.
            cur.execute(
                "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s AND kind='sales.email' AND payload->>'prospect_id'=%s AND status IN ('review','approved')",
                (self.tenant, str(prospect_id)),
            )
            cur.execute(
                "UPDATE sales_prospects SET owner='agent',updated_at=now() WHERE tenant_id=%s AND id=%s",
                (self.tenant, prospect_id),
            )
            if command in {"research", "qualify"}:
                cur.execute(
                    "UPDATE sales_prospects SET status='needs_research',qualification=NULL WHERE tenant_id=%s AND id=%s",
                    (self.tenant, prospect_id),
                )
            cur.execute(
                "UPDATE operation_jobs SET status='failed',error='Superseded by reviewed preparation',updated_at=now() WHERE tenant_id=%s AND payload->>'prospect_id'=%s AND kind=ANY(%s) AND status='pending'",
                (
                    self.tenant,
                    str(prospect_id),
                    [
                        "sales." + s
                        for s in (
                            "research",
                            "qualify",
                            "contacts",
                            "draft",
                            "conversation",
                            "activation",
                            "promote",
                        )
                    ],
                ),
            )
            result = {"owner": "agent", "job_id": None}
            if command != "resume":
                payload["operator_brief"] = reason
                result["job_id"] = self.sales.ops.enqueue(
                    "sales." + stage, str(uuid4()), payload, cur=cur
                )
            self.sales.ops.audit(
                cur,
                prospect_id,
                "recovery." + command,
                actor,
                {"reason": reason, "reviewed_hash": expected_hash, **result},
            )
            return result
