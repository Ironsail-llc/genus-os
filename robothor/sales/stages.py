"""Structured native stages; all domain mutations are fenced with job completion."""

from __future__ import annotations

import asyncio

from pydantic import ValidationError

from robothor.operations.store import BudgetExceeded, Conflict
from robothor.sales.models import (
    ActivationDecision,
    CandidateBatch,
    ContactBatch,
    ConversationDecision,
    SalesSettings,
)
from robothor.sales.runtime import ResearchWorker


class StructuredWorker(ResearchWorker):
    stage = ""
    schema = None
    instruction = ""
    switch = "research_enabled"

    async def context(self, job):
        return await asyncio.to_thread(self.sales.context, job["payload"]["prospect_id"])

    async def tick(self):
        settings = SalesSettings.model_validate(await asyncio.to_thread(self.sales.settings))
        if not getattr(settings, self.switch):
            return False
        job = await asyncio.to_thread(
            self.sales.ops.claim, "sales." + self.stage, lease_seconds=360
        )
        if not job:
            return False
        try:
            context = await self.context(job)
            if context.get("prospect", {}).get("owner", "agent") != "agent":
                await asyncio.to_thread(
                    self.sales.ops.complete, job["id"], job["lease_token"], {"held_for_human": True}
                )
                return True
            output, context, run_id = await self._generate(
                job,
                settings,
                self.stage,
                self.schema,
                context,
                self.instruction,
            )
            await asyncio.to_thread(self.commit, job, context, output, run_id)
        except (Conflict, BudgetExceeded, ValidationError) as exc:
            reason = (
                "Invalid structured stage output" if isinstance(exc, ValidationError) else str(exc)
            )
            await asyncio.to_thread(
                self.sales.ops.defer, job["id"], job["lease_token"], reason, delay_seconds=300
            )
        return True

    def current(self, cur, context):
        p = self.sales.require(context["prospect"]["id"], cur)
        if p["owner"] != "agent" or any(
            p[k] != context["prospect"][k]
            for k in ("version", "conversation_version", "outcome_version")
        ):
            raise Conflict("Company or conversation changed during the stage")
        return p


class ScoutWorker(StructuredWorker):
    stage = "scout"
    schema = CandidateBatch
    instruction = "Discover businesses in the assigned segment, up to the context max_companies limit. Return source-backed CandidateBatch JSON. Do not invent contacts."

    async def context(self, job):
        return {
            "segment": job["payload"]["segment"],
            "max_companies": job["payload"].get("max_companies", 20),
        }

    def commit(self, job, context, output, run_id):
        if len(output.companies) > context["max_companies"]:
            raise Conflict("Scout exceeded the planned candidate allowance")
        with self.sales.ops.transaction() as cur:
            ids = []
            for candidate in output.companies:
                prospect = self.sales.discover(
                    candidate.name, candidate.website, candidate.source_url, cur=cur
                )
                ids.append(str(prospect["id"]))
                self.sales.ops.audit(
                    cur,
                    prospect["id"],
                    "discovery.reason",
                    detail={"reason": candidate.reason, "run_id": str(run_id)},
                )
            self.sales.ops.complete(
                job["id"],
                job["lease_token"],
                {"prospect_ids": sorted(set(ids)), "run_id": str(run_id)},
                cur=cur,
            )


class ContactWorker(StructuredWorker):
    stage = "contacts"
    schema = ContactBatch
    switch = "enrichment_enabled"
    instruction = "Find up to five relevant business decision-makers with public source URLs. Return ContactBatch JSON; verification must be unknown and verified_at null. Never guess an email address."

    async def context(self, job):
        context = await super().context(job)
        if (context["prospect"].get("qualification") or {}).get("decision") != "qualified":
            raise Conflict("Contact research requires qualification")
        return context

    def commit(self, job, context, output, run_id):
        with self.sales.ops.transaction() as cur:
            p = self.current(cur, context)
            ids = []
            for contact in output.contacts:
                contact_id = self.sales.add_contact(p["id"], contact, cur=cur)
                ids.append(contact_id)
                self.sales.ops.enqueue(
                    "sales.verify",
                    contact_id + ":" + str(p["version"]),
                    {"prospect_id": str(p["id"]), "contact_id": contact_id, "email": contact.email},
                    cur=cur,
                )
            self.sales.ops.complete(
                job["id"], job["lease_token"], {"contact_ids": ids, "run_id": str(run_id)}, cur=cur
            )


class ConversationWorker(StructuredWorker):
    stage = "conversation"
    schema = ConversationDecision
    instruction = "Classify the supplied inbound reply. Return ConversationDecision JSON. Explicit opt-outs stop outreach; complaints, clinical questions and custom commitments require human handling. Any permitted response is a reply draft for individual human review, never a send."

    async def context(self, job):
        context = await super().context(job)
        incoming = next(
            (
                m["data"]
                for m in context["messages"]
                if m["provider_id"] == job["payload"]["provider_id"]
            ),
            None,
        )
        if not incoming or incoming["direction"] != "inbound":
            raise Conflict("Known inbound provider message required")
        if incoming["sender"] not in {c["email"] for c in context["contacts"]}:
            raise Conflict("Inbound sender requires contact identity review")
        context["trigger_message"] = incoming
        return context

    def commit(self, job, context, output, run_id):
        with self.sales.ops.transaction() as cur:
            # A clear stop request remains binding even if a later message
            # arrived while it was classified. All other output must be current.
            if output.classification in {"opt_out", "wrong_person"}:
                p = self.sales.require(context["prospect"]["id"], cur)
                self.sales.suppress(
                    context["trigger_message"]["sender"],
                    output.classification,
                    "service:sales-conversation",
                    cur=cur,
                )
            else:
                p = self.current(cur, context)
            result = {"classification": output.classification, "run_id": str(run_id)}
            if output.classification in {"complaint", "human_required"}:
                self.sales.escalate(p["id"], output.reason, cur=cur)
            elif (
                output.classification in {"interested", "question", "objection"}
                and output.draft is None
            ):
                self.sales.escalate(
                    p["id"], "No supported response draft: " + output.reason, cur=cur
                )
            elif output.draft:
                draft = output.draft
                trigger = context["trigger_message"]
                if (
                    draft.reply_to_uuid != trigger["provider_id"]
                    or draft.recipient != trigger["sender"]
                    or draft.sender != trigger["recipient"]
                ):
                    raise Conflict("Reply must address the triggering conversation")
                result["action_id"] = self.sales.draft(p["id"], draft, cur=cur)
            self.sales.ops.audit(
                cur,
                p["id"],
                "conversation.classified",
                detail={"classification": output.classification, "reason": output.reason},
            )
            self.sales.ops.complete(job["id"], job["lease_token"], result, cur=cur)


class ActivationWorker(StructuredWorker):
    stage = "activation"
    switch = "outcomes_enabled"
    schema = ActivationDecision
    instruction = "Using only confirmed business milestones, propose the next standard self-service onboarding step. Return ActivationDecision JSON. Any message requires its own human review and an existing thread. Never infer fulfillment from placement or administrative close-out; never offer custom terms."

    async def context(self, job):
        context = await super().context(job)
        context["retention"] = await asyncio.to_thread(
            self.sales.retention, job["payload"]["prospect_id"]
        )
        context["trigger_milestone"] = job["payload"]["milestone"]
        return context

    def commit(self, job, context, output, run_id):
        with self.sales.ops.transaction() as cur:
            p = self.current(cur, context)
            if (
                context["retention"].get("coverage_complete") is False
                and not context["retention"]["completed_orders"]
            ):
                cur.execute(
                    "UPDATE sales_prospects SET status='awaiting_outcome_evidence',updated_at=now() WHERE tenant_id=%s AND id=%s",
                    (self.sales.tenant, p["id"]),
                )
                if output.human_required:
                    self.sales.escalate(p["id"], output.next_step, cur=cur)
                result = {
                    "awaiting_business_evidence": True,
                    "human_required": output.human_required,
                    "run_id": str(run_id),
                }
                self.sales.ops.audit(cur, p["id"], "activation.awaiting_evidence", detail=result)
                self.sales.ops.complete(job["id"], job["lease_token"], result, cur=cur)
                return
            # Metric and lifecycle state come from verified event data, not an
            # LLM assertion that a prospect has become a purchasing customer.
            status = "active" if context["retention"]["completed_orders"] else "onboarding"
            cur.execute(
                "UPDATE sales_prospects SET status=%s,updated_at=now() WHERE tenant_id=%s AND id=%s",
                (status, self.sales.tenant, p["id"]),
            )
            result = {
                "next_step": output.next_step,
                "human_required": output.human_required,
                "run_id": str(run_id),
            }
            if output.human_required:
                self.sales.escalate(p["id"], output.next_step, cur=cur)
            elif output.draft:
                result["action_id"] = self.sales.draft(p["id"], output.draft, cur=cur)
            self.sales.ops.audit(cur, p["id"], "activation.reviewed", detail=result)
            self.sales.ops.complete(job["id"], job["lease_token"], result, cur=cur)
