"""Human-only repair of a reviewed practice association, never source identity."""

from uuid import UUID, uuid4

from pydantic import Field, model_validator

from robothor.operations.store import Conflict
from robothor.sales.models import Contract
from robothor.sales.service import operator


class Reassignment(Contract):
    expected_revision: str = Field(min_length=1, max_length=200)
    expected_prospect_id: UUID
    target_prospect_id: UUID
    expected_binding_version: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=10, max_length=2000)

    @model_validator(mode="after")
    def coherent_review(self):
        if self.expected_prospect_id == self.target_prospect_id:
            raise ValueError("Reassignment requires a different customer")
        if len(self.reason.strip()) < 10:
            raise ValueError("Reassignment evidence and reason required")
        return self


def reassign(business, observation_id, *, actor, **review):
    operator(actor)
    decision = Reassignment.model_validate(review)
    old_id, target_id = str(decision.expected_prospect_id), str(decision.target_prospect_id)
    observation_id = str(UUID(str(observation_id)))
    sales, tenant = business.sales, business.tenant
    with business.ops.transaction() as cur:
        cur.execute(
            "SELECT source,account_id FROM sales_business_observations WHERE tenant_id=%s AND id=%s AND kind='practice'",
            (tenant, observation_id),
        )
        source = cur.fetchone()
        if not source:
            raise Conflict("Observed practice missing")
        business._lock(cur, source["source"], source["account_id"])
        cur.execute(
            "SELECT * FROM sales_business_observations WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (tenant, observation_id),
        )
        observation = cur.fetchone()
        cur.execute(
            "SELECT * FROM sales_customer_bindings WHERE tenant_id=%s AND observation_id=%s FOR UPDATE",
            (tenant, observation_id),
        )
        binding = cur.fetchone()
        if (
            not binding
            or str(binding["prospect_id"]) != old_id
            or binding["binding_version"] != decision.expected_binding_version
            or observation["revision"] != decision.expected_revision
        ):
            raise Conflict("Practice or customer match changed; refresh and review again")
        # Stable row order also covers simultaneous repairs of different
        # practices that affect the same pair of customers.
        customers = {key: sales.require(key, cur) for key in sorted((old_id, target_id))}
        if any(p["external_company_id"] for p in customers.values()):
            raise Conflict("Legacy customer attribution needs an explicit migration")
        cur.execute(
            "SELECT o.source,o.account_id FROM sales_customer_bindings b JOIN sales_business_observations o "
            "ON o.tenant_id=b.tenant_id AND o.id=b.observation_id WHERE b.tenant_id=%s AND b.prospect_id=%s",
            (tenant, target_id),
        )
        if any(
            row["source"] != source["source"] or row["account_id"] != source["account_id"]
            for row in cur.fetchall()
        ):
            raise Conflict("One authoritative business account per customer is required")
        cur.execute(
            "UPDATE sales_customer_bindings SET prospect_id=%s,reviewed_revision=%s,identity_hash=%s,"
            "binding_version=binding_version+1,status='confirmed',reviewed_by=%s,reason=%s,reviewed_at=now() "
            "WHERE tenant_id=%s AND observation_id=%s",
            (
                target_id,
                decision.expected_revision,
                business._identity(observation),
                actor,
                decision.reason.strip(),
                tenant,
                observation_id,
            ),
        )
        cur.execute(
            "UPDATE sales_prospects p SET business_attribution_detached=NOT EXISTS("
            "SELECT 1 FROM sales_customer_bindings b WHERE b.tenant_id=p.tenant_id AND b.prospect_id=p.id),"
            "status='customer_review',updated_at=now() WHERE p.tenant_id=%s AND p.id=ANY(%s::uuid[])",
            (tenant, [old_id, target_id]),
        )
        change_id = str(uuid4())
        for customer_id in sorted((old_id, target_id)):
            sales.escalate(
                customer_id,
                "Practice reassigned; review the corrected customer conversation",
                cur=cur,
            )
            business._changed(cur, customer_id, "reassignment:" + change_id)
            business.ops.audit(
                cur,
                customer_id,
                "business.customer_reassigned",
                actor,
                {
                    "observation_id": observation_id,
                    "revision": decision.expected_revision,
                    "from_prospect_id": old_id,
                    "to_prospect_id": target_id,
                    "previous_binding_version": decision.expected_binding_version,
                    "binding_version": decision.expected_binding_version + 1,
                    "reason": decision.reason.strip(),
                },
            )
