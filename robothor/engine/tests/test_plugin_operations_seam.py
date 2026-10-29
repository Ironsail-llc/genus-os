"""A plugin's tools reach the operations ledger for their own tenant.

The seam documented in docs/PLUGINS.md: a handler builds
``Operations(ctx.tenant_id)``, proposes with ``proposed_by`` set to its plugin
name, decides by its own policy actor, and a later tool call claims and
finishes the exact approved action. Needs PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from robothor.db.connection import get_connection
from robothor.engine.tools.dispatch import ToolContext
from robothor.operations.store import Conflict, Operations
from robothor.operations.tests.test_store import OPERATIONS_MIGRATIONS

pytestmark = pytest.mark.integration

PLUGIN_NAME = "acme-plugin"
POLICY = f"policy:{PLUGIN_NAME}:2026-10-29"
KIND = f"{PLUGIN_NAME}.note"


async def acme_propose_note(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Propose a note; an enforce-mode rule approves it without a human."""
    ops = Operations(ctx.tenant_id)
    action = ops.propose(KIND, args["key"], {"body": args["body"]}, proposed_by=PLUGIN_NAME)
    if args.get("auto_approve"):
        ops.decide(action, True, POLICY)
    return {"action_id": action}


async def acme_execute_action(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Claim the approved note and record the provider's acknowledgement."""
    ops = Operations(ctx.tenant_id)
    claimed = ops.claim_action_by_id(args["action_id"])
    if claimed is None or claimed["kind"] != KIND:
        return {"error": "nothing approved to execute"}
    ops.finish_action(str(claimed["id"]), claimed["lease_token"], "completed", {"id": "ack-1"})
    return {"executed": str(claimed["id"]), "body": claimed["payload"]["body"]}


async def acme_supersede_note(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Withdraw this plugin's open drafts for a key prefix, then list what is left."""
    ops = Operations(ctx.tenant_id)
    for row in ops.list_actions(kinds=[KIND], status="review", proposed_by=PLUGIN_NAME):
        if row["dedup_key"].startswith(args["prefix"]):
            ops.cancel_action(str(row["id"]), args["reason"], proposed_by=PLUGIN_NAME)
    return {
        "open": sorted(
            r["dedup_key"]
            for r in ops.list_actions(kinds=[KIND], status="review", proposed_by=PLUGIN_NAME)
        )
    }


PLUGIN = {
    "genus_contract_version": "1.0",
    "handlers": {
        "acme_propose_note": acme_propose_note,
        "acme_execute_action": acme_execute_action,
        "acme_supersede_note": acme_supersede_note,
    },
}


@pytest.fixture
def tenant() -> str:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for filename in OPERATIONS_MIGRATIONS:
                cur.execute((Path(__file__).parents[3] / "crm/migrations" / filename).read_text())
        conn.commit()
    return "test-" + uuid4().hex


def _ctx(tenant: str) -> ToolContext:
    return ToolContext(agent_id="workflow:acme-execute-approved", tenant_id=tenant)


@pytest.mark.asyncio
async def test_plugin_tool_proposes_then_claims_after_its_policy_decides(tenant):
    handlers = PLUGIN["handlers"]
    proposed = await handlers["acme_propose_note"](
        {"key": "n:1", "body": "Hi", "auto_approve": True}, _ctx(tenant)
    )
    action = proposed["action_id"]

    done = await handlers["acme_execute_action"]({"action_id": action}, _ctx(tenant))

    assert done == {"executed": action, "body": "Hi"}
    with Operations(tenant).transaction() as cur:
        cur.execute(
            "SELECT status,proposed_by,decided_by,receipt FROM operation_actions "
            "WHERE tenant_id=%s AND id=%s",
            (tenant, action),
        )
        row = cur.fetchone()
    assert row == {
        "status": "completed",
        "proposed_by": PLUGIN_NAME,
        "decided_by": POLICY,
        "receipt": {"id": "ack-1"},
    }


@pytest.mark.asyncio
async def test_unapproved_proposal_is_not_executable(tenant):
    handlers = PLUGIN["handlers"]
    proposed = await handlers["acme_propose_note"]({"key": "n:1", "body": "Hi"}, _ctx(tenant))
    done = await handlers["acme_execute_action"](proposed, _ctx(tenant))
    assert done == {"error": "nothing approved to execute"}


@pytest.mark.asyncio
async def test_another_tenant_cannot_see_or_decide_the_proposal(tenant):
    handlers = PLUGIN["handlers"]
    proposed = await handlers["acme_propose_note"]({"key": "n:1", "body": "Hi"}, _ctx(tenant))
    other = "test-" + uuid4().hex
    with pytest.raises(Conflict):
        Operations(other).decide(proposed["action_id"], True, POLICY)
    assert Operations(other).claim_action(kind=KIND) is None


@pytest.mark.asyncio
async def test_plugin_tool_supersedes_its_own_drafts_through_the_store(tenant):
    handlers = PLUGIN["handlers"]
    old = await handlers["acme_propose_note"]({"key": "old:1", "body": "v1"}, _ctx(tenant))
    await handlers["acme_propose_note"]({"key": "new:1", "body": "v2"}, _ctx(tenant))

    left = await handlers["acme_supersede_note"](
        {"prefix": "old:", "reason": "superseded by new:1"}, _ctx(tenant)
    )

    assert left == {"open": ["new:1"]}
    row = Operations(tenant).get_action(old["action_id"])
    assert row["status"] == "cancelled"
    done = await handlers["acme_execute_action"](old, _ctx(tenant))
    assert done == {"error": "nothing approved to execute"}
