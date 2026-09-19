"""Prepare review work from Genus CRM independently of external CRM promotion."""

from datetime import UTC, datetime, timedelta

from robothor.operations.store import Conflict
from robothor.sales.models import Contact, SalesSettings


class InitialDraftPlanner:
    def __init__(self, sales):
        self.sales = sales

    def plan(self):
        """Queue a first draft once; retries/rejected messages need operator handling."""
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE",
                (self.sales.tenant,),
            )
            row = cur.fetchone()
            settings = SalesSettings.model_validate(row["config"] if row else {})
            if not (
                settings.research_enabled
                and settings.agents.get("draft")
                and settings.active_knowledge_version
                and settings.senders
                and settings.postal_address
                and settings.unsubscribe_url
            ):
                return 0
            cur.execute(
                "SELECT p.id FROM sales_prospects p WHERE p.tenant_id=%s AND p.status IN ('accepted','promoted') "
                "AND p.owner='agent' AND p.outcome_version=0 "
                "AND NOT EXISTS (SELECT 1 FROM sales_messages m WHERE m.tenant_id=p.tenant_id AND m.prospect_id=p.id) "
                "AND NOT EXISTS (SELECT 1 FROM operation_actions a WHERE a.tenant_id=p.tenant_id AND a.kind='sales.email' AND a.payload->>'prospect_id'=p.id::text) "
                "AND NOT EXISTS (SELECT 1 FROM operation_jobs j WHERE j.tenant_id=p.tenant_id AND j.kind='sales.draft' AND j.payload->>'prospect_id'=p.id::text) "
                "ORDER BY p.updated_at,p.id",
                (self.sales.tenant,),
            )
            candidates = [str(row["id"]) for row in cur.fetchall()]
            queued = 0
            now = datetime.now(UTC)
            for prospect_id in candidates:
                p = self.sales.require(prospect_id, cur)
                cur.execute(
                    "SELECT 1 FROM operation_actions WHERE tenant_id=%s AND kind='sales.email' AND payload->>'prospect_id'=%s "
                    "UNION ALL SELECT 1 FROM operation_jobs WHERE tenant_id=%s AND kind='sales.draft' AND payload->>'prospect_id'=%s LIMIT 1",
                    (self.sales.tenant, prospect_id, self.sales.tenant, prospect_id),
                )
                if cur.fetchone():
                    continue
                # Recheck after acquiring the same lock used by conversation writes.
                if (
                    p["owner"] != "agent"
                    or p["status"] not in {"accepted", "promoted"}
                    or p["outcome_version"]
                    or p["conversation_version"]
                ):
                    continue
                q = p["qualification"] or {}
                if q.get("decision") != "qualified" or settings.active_policy_versions.get(
                    q.get("buying_case")
                ) != q.get("policy_version"):
                    continue
                try:
                    self.sales.require_assessment(prospect_id, cur=cur)
                except Conflict:
                    continue
                cur.execute(
                    "SELECT data FROM sales_contacts WHERE tenant_id=%s AND prospect_id=%s",
                    (self.sales.tenant, prospect_id),
                )
                contacts = [Contact.model_validate(row["data"]) for row in cur.fetchall()]
                if not any(
                    c.verification == "valid"
                    and c.verified_at
                    and c.verified_at.tzinfo
                    and now - timedelta(days=30) <= c.verified_at <= now
                    and not self.sales._suppressed(c.email, cur)
                    for c in contacts
                ):
                    continue
                self.sales.ops.enqueue(
                    "sales.draft",
                    f"{prospect_id}:{p['version']}",
                    {"prospect_id": prospect_id},
                    cur=cur,
                )
                queued += 1
                if queued >= 100:
                    break
            return queued
