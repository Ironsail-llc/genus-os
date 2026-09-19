"""Opt-in durable sales work executed by Genus' existing agent runner.

The worker is explicitly constructed by the instance deployment. Importing
this module does not schedule work, spend money, or activate integrations.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import replace
from datetime import UTC, datetime

from pydantic import ValidationError

from robothor.operations.store import BudgetExceeded, Conflict
from robothor.sales.models import Dossier, Draft, SalesSettings


class NativeStageRunner:
    """Apply workflow limits while retaining the native manifest and permissions."""

    async def run(self, *, agent_id, tenant_id, message, correlation_id, max_cost_usd):
        from robothor.engine.config import load_agent_config
        from robothor.engine.models import TriggerType
        from robothor.engine.request_budget import RequestBudget, budget_scope
        from robothor.engine.tools.handlers.spawn import get_runner

        runner = get_runner()
        if runner is None:
            raise Conflict("Genus agent runner is not available")
        config = load_agent_config(agent_id, runner.config.manifest_dir)
        if config is None:
            raise Conflict("Configured sales agent manifest is missing")
        safe = {"web_search", "web_fetch", "sales_get_prospect", "sales_get_context", "write_file"}
        if (
            not config.tools_allowed
            or not set(config.tools_allowed) <= safe
            or config.can_spawn_agents
        ):
            raise Conflict("Sales research requires a bounded read/research manifest")
        if "write_file" in config.tools_allowed and (
            not config.write_path_allowlist
            or "write_path_restrict" not in config.guardrails
            or config.guardrails_opt_out
        ):
            raise Conflict("Sales status writes require an enforced path allowlist")
        bounded = replace(
            config,
            hard_budget=True,
            max_cost_usd=min(max_cost_usd, config.max_cost_usd or max_cost_usd),
            safety_cap=min(config.safety_cap, 20),
            delivery_mode="none",
            continuous=False,
            auto_task=False,
            downstream_agents=[],
        )
        budget = RequestBudget(math.floor(bounded.max_cost_usd * 1e6))
        with budget_scope(budget):
            result = await runner.execute(
                agent_id=agent_id,
                message=message,
                agent_config=bounded,
                tenant_id=tenant_id,
                trigger_type=TriggerType.WORKFLOW,
                trigger_detail="durable_sales_work",
                correlation_id=correlation_id,
                user_id="service:" + agent_id,
                user_role=bounded.service_role,
            )
        # Includes retries, failed attempts, finalizers and auxiliary model calls
        # omitted from normal run token accounting. Unknown usage stays charged.
        result.total_cost_usd = max(result.total_cost_usd, budget.charged_units / 1e6)
        return result


class ResearchWorker:
    RUN_ALLOWANCE_UNITS = 1_000_000

    def __init__(self, sales, runner=None):
        self.sales = sales
        self.runner = runner or NativeStageRunner()

    def _reserve(self, settings, job):
        # Settings and work authorization are checked under the same transaction
        # as both allowances. A waiting worker cannot restore an old spending cap
        # or start a new paid attempt after its lease was replaced.
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE",
                (self.sales.tenant,),
            )
            row = cur.fetchone()
            current = SalesSettings.model_validate(row["config"] if row else {})
            if current != settings:
                raise Conflict("Sales settings changed before spending admission")
            cur.execute(
                "SELECT id FROM operation_jobs WHERE tenant_id=%s AND id=%s AND lease_token=%s "
                "AND status='running' AND lease_until>clock_timestamp() AND deadline>clock_timestamp() FOR UPDATE",
                (self.sales.tenant, job["id"], job["lease_token"]),
            )
            if not cur.fetchone():
                raise Conflict("Work lease expired or replaced before spending admission")
            now = datetime.now(UTC)
            scopes = {
                f"sales:month:{now:%Y-%m}": current.monthly_limit_units,
                f"sales:day:{now:%Y-%m-%d}": current.daily_limit_units,
            }
            for scope in sorted(scopes):
                self.sales.ops.set_budget(scope, scopes[scope], cur=cur)
            return self.sales.ops.reserve_many(
                scopes,
                str(job["id"]) + ":" + str(job["lease_token"]),
                self.RUN_ALLOWANCE_UNITS,
                active_only=True,
                cur=cur,
            )

    async def tick(self):
        settings = SalesSettings.model_validate(await asyncio.to_thread(self.sales.settings))
        if not settings.research_enabled:
            return False
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.research", lease_seconds=360)
        if not job:
            return False
        try:
            agent = settings.agents.get("research")
            if not agent:
                raise Conflict("Research agent not configured")
            context = await asyncio.to_thread(self.sales.context, job["payload"]["prospect_id"])
            if context["prospect"]["version"] != job["payload"]["version"]:
                raise Conflict("Queued research version is stale")
            reservations = await asyncio.to_thread(self._reserve, settings, job)
            # If a process disappears mid-run the reserved allowance remains;
            # recovery never assumes unknown provider usage was free.
            message = json.dumps(
                {
                    "task": "Research this company and return only the exact dossier JSON contract.",
                    "untrusted_business_data": context,
                    "output_schema": Dossier.model_json_schema(),
                },
                default=str,
            )
            try:
                result = await asyncio.wait_for(
                    self.runner.run(
                        agent_id=agent,
                        tenant_id=self.sales.tenant,
                        message=message,
                        correlation_id=str(job["id"]),
                        max_cost_usd=self.RUN_ALLOWANCE_UNITS / 1e6,
                    ),
                    300,
                )
            except (TimeoutError, Conflict):
                raise Conflict(
                    "Agent did not finish; reserved cost requires reconciliation"
                ) from None
            actual = max(0, math.ceil(result.total_cost_usd * 1e6))
            await asyncio.to_thread(self.sales.ops.settle_many, reservations, actual)
            if str(result.status) != "completed":
                raise Conflict("Agent run did not complete successfully")
            dossier = Dossier.model_validate_json(result.output_text or "")
            await asyncio.to_thread(self._commit, job, dossier, result.id)
        except (Conflict, BudgetExceeded, ValidationError) as exc:
            reason = (
                "Invalid structured research output"
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            await asyncio.to_thread(
                self.sales.ops.defer, job["id"], job["lease_token"], reason, delay_seconds=300
            )
        return True

    def _commit(self, job, dossier, run_id):
        with self.sales.ops.transaction() as cur:
            self.sales.research(
                job["payload"]["prospect_id"],
                dossier,
                expected_version=job["payload"]["version"],
                cur=cur,
            )
            self.sales.ops.complete(job["id"], job["lease_token"], {"run_id": str(run_id)}, cur=cur)

    async def qualify_tick(self):
        settings = SalesSettings.model_validate(await asyncio.to_thread(self.sales.settings))
        if not settings.research_enabled:
            return False
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.qualify")
        if not job:
            return False
        try:
            await asyncio.to_thread(self._qualify, job, settings)
        except Conflict as exc:
            await asyncio.to_thread(
                self.sales.ops.defer, job["id"], job["lease_token"], str(exc), delay_seconds=300
            )
        return True

    def _qualify(self, job, settings):
        with self.sales.ops.transaction() as cur:
            p = self.sales.require(job["payload"]["prospect_id"], cur)
            if p["version"] != job["payload"]["version"]:
                raise Conflict("Queued qualification version is stale")
            version = settings.active_policy_versions.get(p["dossier"]["buying_case"])
            if not version:
                raise Conflict("No active policy for this buying case")
            result = self.sales.qualify(p["id"], version, cur=cur)
            self.sales.ops.complete(job["id"], job["lease_token"], result, cur=cur)


class DraftWorker(ResearchWorker):
    """A native SDR run produces immutable review work, never authorization."""

    async def tick(self):
        settings = SalesSettings.model_validate(await asyncio.to_thread(self.sales.settings))
        if not settings.research_enabled:
            return False
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.draft", lease_seconds=360)
        if not job:
            return False
        try:
            agent = settings.agents.get("draft")
            if not agent:
                raise Conflict("SDR agent not configured")
            context = await asyncio.to_thread(self.sales.context, job["payload"]["prospect_id"])
            if context["prospect"]["owner"] != "agent":
                raise Conflict("Prospect is under human ownership")
            reservations = await asyncio.to_thread(self._reserve, settings, job)
            message = json.dumps(
                {
                    "task": "Return one initial email Draft JSON for human review; use only active claims and evidence.",
                    "untrusted_business_data": context,
                    "output_schema": Draft.model_json_schema(),
                },
                default=str,
            )
            try:
                result = await asyncio.wait_for(
                    self.runner.run(
                        agent_id=agent,
                        tenant_id=self.sales.tenant,
                        message=message,
                        correlation_id=str(job["id"]),
                        max_cost_usd=self.RUN_ALLOWANCE_UNITS / 1e6,
                    ),
                    300,
                )
            except (TimeoutError, Conflict):
                raise Conflict(
                    "SDR did not finish; reserved cost requires reconciliation"
                ) from None
            await asyncio.to_thread(
                self.sales.ops.settle_many,
                reservations,
                max(0, math.ceil(result.total_cost_usd * 1e6)),
            )
            if str(result.status) != "completed":
                raise Conflict("SDR run did not complete")
            draft = Draft.model_validate_json(result.output_text or "")
            if draft.purpose != "initial" or draft.reply_to_uuid:
                raise Conflict("Initial SDR work cannot originate a follow-up or reply")
            await asyncio.to_thread(self._commit_draft, job, context, draft, result.id)
        except (Conflict, BudgetExceeded, ValidationError) as exc:
            reason = (
                "Invalid structured SDR output" if isinstance(exc, ValidationError) else str(exc)
            )
            await asyncio.to_thread(
                self.sales.ops.defer, job["id"], job["lease_token"], reason, delay_seconds=300
            )
        return True

    def _commit_draft(self, job, context, draft, run_id):
        with self.sales.ops.transaction() as cur:
            p = self.sales.require(job["payload"]["prospect_id"], cur)
            if any(
                p[key] != context["prospect"][key]
                for key in ("version", "conversation_version", "outcome_version")
            ):
                raise Conflict("Prospect or conversation changed during drafting")
            action = self.sales.draft(p["id"], draft, cur=cur)
            self.sales.ops.complete(
                job["id"], job["lease_token"], {"action_id": action, "run_id": str(run_id)}, cur=cur
            )
