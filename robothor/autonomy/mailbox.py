"""Owner-authorized verification mail becomes an expiring private resource."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import SecretStr

from robothor.autonomy.models import ResourceInput

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope
    from robothor.autonomy.store import AutonomyStore
    from robothor.engine.tools.dispatch import ToolContext


async def retrieve_verification(
    store: AutonomyStore,
    scope: Scope,
    operation_id: str,
    row: dict[str, Any],
    args: dict[str, Any],
    ctx: ToolContext,
) -> dict[str, Any]:
    # The legacy gws connector represents the appliance owner's
    # mailbox, not every tenant's mailbox. Do not accidentally extend
    # that access to another person's standing grant.
    from robothor.autonomy.verification import extract_verification
    from robothor.constants import DEFAULT_TENANT
    from robothor.crm.dal import get_owner_person
    from robothor.engine.tools.dispatch import get_agent_toolset
    from robothor.engine.tools.handlers import gws

    allowed = get_agent_toolset() or frozenset()
    if not {"gws_gmail_search", "gws_gmail_get"} <= allowed or ctx.tenant_id != DEFAULT_TENANT:
        raise PermissionError("verification_mailbox_not_authorized")
    owner = await asyncio.to_thread(get_owner_person, ctx.tenant_id)
    if scope.owner_id != "person:" + str(owner.get("id", "")):
        raise PermissionError("verification_mailbox_not_authorized")
    policy = await asyncio.to_thread(store.check_authority, scope, operation_id, ctx.agent_id)
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
    host = urlsplit(row["proposal"]["origin"]).hostname or ""
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
    sender_domains = policy.verification_senders.get(row["proposal"]["origin"], frozenset())
    senders = " ".join("from:(@" + domain + ")" for domain in sorted({host} | sender_domains))
    result = await gws.HANDLERS["gws_gmail_search"](
        {"query": f"to:{recipient} {{{senders}}} after:{after}", "max_results": 5}, ctx
    )
    fetched = [
        await asyncio.to_thread(gws._fetch_message, candidate["id"], "full")
        for candidate in result.get("messages", [])[:5]
    ]
    # The mailbox answers newest first. An authorized sender could otherwise
    # send their own message after the website's and be read instead of it, so
    # the earliest message that carries a verification is the one that counts.
    for raw in sorted(fetched, key=_sent_at):
        value = extract_verification(
            raw,
            recipient=recipient,
            destination=row["proposal"]["origin"],
            after=after,
            mode=args.get("mode", "code"),
            sender_domains=sender_domains,
        )
        if value:
            # Mailbox I/O may outlive revocation; never enroll the result
            # under authority that no longer covers this operation.
            await asyncio.to_thread(store.check_authority, scope, operation_id, ctx.agent_id)
            return await asyncio.to_thread(
                store.put_resource,
                scope,
                ResourceInput(
                    kind="credential",
                    label="One-time website verification",
                    origin=row["proposal"]["origin"],
                    payload=SecretStr(json.dumps({"username": "verification", "password": value})),
                ),
                lifetime_seconds=600,
                source="mailbox_verification",
            )
    return {
        "state": "awaiting_external_action",
        "reason": "verification_email_not_available",
    }


def _sent_at(message: dict[str, Any]) -> int:
    """The mailbox's own receipt stamp; an unreadable one sorts last."""
    try:
        return int(message.get("internalDate", 0))
    except (TypeError, ValueError):
        return 2**63 - 1
