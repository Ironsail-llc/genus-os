"""Existing browser tool's delegated, reference-only execution mode."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import SecretStr

from robothor.autonomy.broker import ExecutionPlan
from robothor.autonomy.identity import scope_for_actor
from robothor.autonomy.models import RequestContext, ResourceInput, WebOperation
from robothor.autonomy.runtime import run_browser
from robothor.autonomy.store import AutonomyStore

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope
    from robothor.engine.tools.dispatch import ToolContext


async def _enrollment_link(
    store: AutonomyStore, scope: Scope, args: dict[str, Any]
) -> dict[str, Any]:
    from robothor.autonomy.enrollment import EnrollmentRequest, EnrollmentStore

    spec = EnrollmentRequest.model_validate(args.get("enrollment", {}))
    link = await asyncio.to_thread(EnrollmentStore(store).create, scope, spec)
    # Only a scoped link reaches history; private inputs use the secure page.
    return {
        "setup_path": link["path"],
        "setup_url": link["url"],
        "expires_at": link["expires_at"],
    }


async def handle(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.user_id or not ctx.agent_id or ctx.is_benchmark:
        return {"error": "verified_owner_required"}
    try:
        scope = await asyncio.to_thread(scope_for_actor, ctx.tenant_id, ctx.user_id)
        store = AutonomyStore()
        kind = args.get("kind", "status")
        if kind == "enrollment_link":
            return await _enrollment_link(store, scope, args)
        if kind in {
            "workflow_open",
            "workflow_inspect",
            "workflow_execute",
            "workflow_status",
            "workflow_close",
            "workflow_reconcile",
        }:
            from robothor.autonomy.workflows.client import invoke

            if "verification_code" in args:
                return {"error": "use_secure_code_entry"}
            return await invoke(
                scope,
                ctx.agent_id,
                {
                    **{key: value for key, value in args.items() if key != "kind"},
                    "kind": kind.removeprefix("workflow_"),
                },
            )
        if kind == "status":
            settings = await asyncio.to_thread(store.settings, scope)
            return {
                "settings": settings.model_dump(),
                "resources": await asyncio.to_thread(store.resources, scope),
                "grants": await asyncio.to_thread(store.grants, scope),
                "spending": await asyncio.to_thread(store.spending_projection, scope),
                "operations": await asyncio.to_thread(store.recent_operations, scope),
                "setup_path": "/account/autonomy",
            }
        if kind == "readiness":
            from robothor.autonomy.readiness import ReadinessRequest, task_readiness

            spec = ReadinessRequest.model_validate(
                {key: args[key] for key in ("grant_id", "proposal", "requirements") if key in args}
            )
            return await asyncio.to_thread(task_readiness, store, scope, ctx.agent_id, spec)
        if kind == "prepare":
            proposal = WebOperation.model_validate(args["proposal"])
            return await asyncio.to_thread(
                store.reserve,
                scope,
                str(UUID(args["grant_id"])),
                ctx.agent_id,
                proposal,
                request_context=RequestContext(run_id=UUID(ctx.run_id), actor_id=ctx.user_id)
                if ctx.run_id
                else None,
            )
        if kind == "procedures":
            from robothor.autonomy.procedures import ProcedureQuery

            query = ProcedureQuery.model_validate(
                {"origin": args.get("origin"), "action": args.get("action")}
            )
            return {
                "procedures": await asyncio.to_thread(store.procedures, scope, ctx.agent_id, query)
            }
        operation_id = str(UUID(args["operation_id"]))
        row = await asyncio.to_thread(store.operation, scope, operation_id)
        if row["agent_id"] != ctx.agent_id:
            raise PermissionError("agent_not_allowed")
        if kind in {"handoff", "handoffs"}:
            from robothor.autonomy.handoffs import HandoffRequest, HandoffStore

            handoffs = HandoffStore(store)
            if kind == "handoffs":
                rows = await asyncio.to_thread(handoffs.list, scope, ctx.agent_id)
                return {"handoffs": [item for item in rows if item["operation_id"] == operation_id]}
            spec = HandoffRequest.model_validate(args.get("handoff", {}))
            result = await asyncio.to_thread(
                handoffs.create, scope, operation_id, ctx.agent_id, spec
            )
            return {
                **result,
                "setup_path": "/account/autonomy",
                "next_step": "external_verification_then_read_only_check",
            }
        if kind == "payment_status":
            from robothor.autonomy.payment_journal import PaymentJournal

            return await asyncio.to_thread(PaymentJournal(store).read, scope, operation_id)
        if kind == "operation":
            return row
        if kind == "cancel":
            await asyncio.to_thread(store.finish, scope, operation_id, "cancelled")
            return {"state": "cancelled"}
        if kind == "generate_credential":
            await asyncio.to_thread(store.check_authority, scope, operation_id, ctx.agent_id)
            if row["proposal"]["action"] != "account":
                raise PermissionError("account_authority_required")
            profile = await asyncio.to_thread(
                store.consume_resource,
                scope,
                str(UUID(args["profile_id"])),
                row["proposal"]["origin"],
                kind="profile",
            )
            return await asyncio.to_thread(
                store.put_resource,
                scope,
                ResourceInput(
                    kind="credential",
                    label="Website login",
                    origin=row["proposal"]["origin"],
                    payload=SecretStr(
                        json.dumps(
                            {
                                "username": profile["email"],
                                "password": "Aa1!" + secrets.token_urlsafe(24),
                            }
                        )
                    ),
                ),
                source="generated",
            )
        if kind == "email_verification":
            from robothor.autonomy.mailbox import retrieve_verification

            return await retrieve_verification(store, scope, operation_id, row, args, ctx)
        if kind in {"inspect", "execute", "reconcile"}:
            raw_plan = args.get("plan")
            if kind == "reconcile":
                if not isinstance(raw_plan, dict):
                    raise ValueError("reconciliation_plan_required")
                raw_plan = {**raw_plan, "submit_selector": "__unused__"}
            return await run_browser(
                scope,
                operation_id,
                ctx.agent_id,
                ExecutionPlan.model_validate(raw_plan) if kind != "inspect" else None,
                inspect_url=args.get("url") if kind == "inspect" else None,
                session_resource_id=args.get("session_resource_id"),
                managed=args.get("managed", False),
                reconcile=kind == "reconcile",
            )
        return {"error": "unknown_autonomy_action"}
    except PermissionError as exc:
        return {"error": str(exc)}
    except Exception:
        return {"error": "autonomy_request_failed", "setup_path": "/account/autonomy"}
