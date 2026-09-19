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
from robothor.autonomy.models import ResourceInput, WebOperation
from robothor.autonomy.runtime import run_browser
from robothor.autonomy.store import AutonomyStore

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext


async def handle(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.user_id or not ctx.agent_id or ctx.is_benchmark:
        return {"error": "verified_owner_required"}
    try:
        scope = await asyncio.to_thread(scope_for_actor, ctx.tenant_id, ctx.user_id)
        store = AutonomyStore()
        kind = args.get("kind", "status")
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
        if kind == "prepare":
            proposal = WebOperation.model_validate(args["proposal"])
            return await asyncio.to_thread(
                store.reserve, scope, str(UUID(args["grant_id"])), ctx.agent_id, proposal
            )
        operation_id = str(UUID(args["operation_id"]))
        row = await asyncio.to_thread(store.operation, scope, operation_id)
        if row["agent_id"] != ctx.agent_id:
            raise PermissionError("agent_not_allowed")
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
            )
        if kind == "email_verification":
            # The legacy gws connector represents the appliance owner's
            # mailbox, not every tenant's mailbox. Do not accidentally extend
            # that access to another person's standing grant.
            from robothor.autonomy.verification import extract_verification
            from robothor.constants import DEFAULT_TENANT
            from robothor.crm.dal import get_owner_person
            from robothor.engine.tools.dispatch import get_agent_toolset
            from robothor.engine.tools.handlers import gws

            allowed = get_agent_toolset() or frozenset()
            if (
                not {"gws_gmail_search", "gws_gmail_get"} <= allowed
                or ctx.tenant_id != DEFAULT_TENANT
            ):
                raise PermissionError("verification_mailbox_not_authorized")
            owner = await asyncio.to_thread(get_owner_person, ctx.tenant_id)
            if scope.owner_id != "person:" + str(owner.get("id", "")):
                raise PermissionError("verification_mailbox_not_authorized")
            await asyncio.to_thread(store.check_authority, scope, operation_id, ctx.agent_id)
            profile = await asyncio.to_thread(
                store.consume_resource,
                scope,
                str(UUID(args["profile_id"])),
                row["proposal"]["origin"],
                kind="profile",
            )
            import re
            from urllib.parse import urlsplit

            recipient = profile.get("email", "")
            if not re.fullmatch(r"[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+", recipient):
                raise PermissionError("verification_email_missing")
            host = urlsplit(row["proposal"]["origin"]).hostname
            after = row["created_epoch"]
            if args.get("source_operation_id"):
                source = await asyncio.to_thread(
                    store.operation, scope, str(UUID(args["source_operation_id"]))
                )
                if (
                    source["agent_id"] != ctx.agent_id
                    or source["proposal"]["origin"] != row["proposal"]["origin"]
                ):
                    raise PermissionError("verification_source_mismatch")
                after = source["created_epoch"]
            result = await gws.HANDLERS["gws_gmail_search"](
                {"query": f"to:{recipient} from:(@{host}) after:{after}", "max_results": 5}, ctx
            )
            for candidate in result.get("messages", []):
                raw = await asyncio.to_thread(gws._fetch_message, candidate["id"], "full")
                value = extract_verification(
                    raw,
                    recipient=recipient,
                    destination=row["proposal"]["origin"],
                    after=after,
                    mode=args.get("mode", "code"),
                )
                if value:
                    return await asyncio.to_thread(
                        store.put_resource,
                        scope,
                        ResourceInput(
                            kind="credential",
                            label="One-time website verification",
                            origin=row["proposal"]["origin"],
                            payload=SecretStr(
                                json.dumps({"username": "verification", "password": value})
                            ),
                        ),
                        lifetime_seconds=600,
                    )
            return {
                "state": "awaiting_external_action",
                "reason": "verification_email_not_available",
            }
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
