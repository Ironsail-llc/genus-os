"""
Agent memory blocks — named persistent text blocks for structured working memory.

Provides read/write/list operations against the agent_memory_blocks table.
Used by the MCP server to expose memory_block_read/write/list tools.

Usage:
    from robothor.memory.blocks import read_block, write_block, list_blocks

    block = read_block("persona")
    write_block("working_context", "Current task: ...")
    all_blocks = list_blocks()
"""

from __future__ import annotations

import logging
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.db import get_connection

logger = logging.getLogger(__name__)

DEFAULT_BLOCK_SEEDS = [
    ("persona", "system", 3000),
    ("user_profile", "system", 5000),
    ("user_model", "persistent", 5000),
    ("working_context", "ephemeral", 5000),
    ("operational_findings", "persistent", 5000),
    ("contacts_summary", "persistent", 5000),
    # Nightwatch self-improvement loop
    ("nightwatch_log", "persistent", 5000),
    ("performance_baselines", "persistent", 5000),
    # AutoAgent / Auto Researcher / Agent Architect learnings
    ("autoagent_learnings", "persistent", 5000),
    ("autoresearch_learnings", "persistent", 5000),
    ("architect_evolution_log", "persistent", 5000),
    ("architect_dispatch_ledger", "persistent", 5000),
    # Curiosity Engine + Self Model
    ("curiosity_engine_findings", "persistent", 5000),
    ("self_model", "persistent", 8000),
    # Skill curator (Rip 5) — last consolidation-pass timestamp (ISO-8601)
    ("curator_state", "persistent", 1000),
    # Preferences (JSON list of {preference, confidence, last_confirmed, evidence_fact_ids, stale})
    ("preferences", "persistent", 10000),
]


def read_block(block_name: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """Read a named memory block and increment its read count.

    Returns:
        dict with block_name, content, last_written_at, or error if not found.
    """
    if not block_name:
        return {"error": "block_name is required"}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                # pruned_at filter: a soft-deleted block must leave the
                # always-loaded tier, otherwise the prune reclaimed nothing.
                "UPDATE agent_memory_blocks "
                "SET read_count = read_count + 1, last_read_at = NOW() "
                "WHERE tenant_id = %s AND block_name = %s AND pruned_at IS NULL "
                "RETURNING content, last_written_at",
                (tenant_id, block_name),
            )
            row = cur.fetchone()
            if not row:
                return {"error": f"Block '{block_name}' not found"}
            return {
                "block_name": block_name,
                "content": row[0] or "",
                "last_written_at": row[1].isoformat() if row[1] else None,
            }


def write_block(block_name: str, content: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """Write or update a named memory block.

    Uses UPSERT — creates the block if it doesn't exist, updates if it does.

    Returns:
        dict with success status and block_name.
    """
    if not block_name:
        return {"error": "block_name is required"}

    from robothor.memory.block_budget import budget_mode, check_budget

    mode = budget_mode()
    verdict = None
    with get_connection() as conn:
        with conn.cursor() as cur:
            if mode != "off":
                # Memory blocks are the always-loaded tier — every character
                # here is paid on every turn. max_chars existed as a column and
                # was read by nothing, which is how a 57k-char block happened.
                cur.execute(
                    "SELECT max_chars FROM agent_memory_blocks "
                    "WHERE tenant_id = %s AND block_name = %s",
                    (tenant_id, block_name),
                )
                row = cur.fetchone()
                verdict = check_budget(content, max_chars=row[0] if row else None)
                if verdict.over and mode == "enforce":
                    # Rejected, never truncated: truncation drops the END of a
                    # block, which is where the most recent context lives, and
                    # does it silently. A refused write is visible and the
                    # caller can summarise instead.
                    logger.warning(
                        "write_block %r refused: %s", block_name, verdict.reason
                    )
                    return {
                        "error": (
                            f"block {block_name!r} exceeds its context budget: "
                            f"{verdict.reason}. Summarise or split it — content "
                            f"was NOT truncated or written."
                        ),
                        "block_name": block_name,
                        "over_budget": True,
                        "overflow_chars": verdict.overflow_chars,
                    }
                if verdict.over:
                    logger.warning(
                        "write_block %r over budget (observe): %s",
                        block_name,
                        verdict.reason,
                    )

            cur.execute(
                "INSERT INTO agent_memory_blocks "
                "(tenant_id, block_name, content, last_written_at, write_count) "
                "VALUES (%s, %s, %s, NOW(), 1) "
                "ON CONFLICT (tenant_id, block_name) DO UPDATE "
                "SET content = EXCLUDED.content, last_written_at = NOW(), "
                "    write_count = agent_memory_blocks.write_count + 1 "
                "RETURNING id",
                (tenant_id, block_name, content),
            )
            result: dict[str, Any] = {"success": True, "block_name": block_name}
            # Observe must produce evidence, not silence: a soak whose finding
            # is "no events" cannot tell a working control from an inert one.
            if verdict is not None and verdict.over:
                result["over_budget"] = True
                result["overflow_chars"] = verdict.overflow_chars
            return result


def list_blocks(tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """List all memory blocks with their sizes and timestamps.

    Returns:
        dict with a 'blocks' list, each entry containing name, size, last_written_at.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT block_name, length(content) AS size, last_written_at "
                "FROM agent_memory_blocks WHERE tenant_id = %s "
                # 1,525 soft-deleted blocks would otherwise still be enumerated
                # here, and the 4.09M characters they hold would still be paid.
                "AND pruned_at IS NULL ORDER BY block_name",
                (tenant_id,),
            )
            return {
                "blocks": [
                    {
                        "name": row[0],
                        "size": row[1] or 0,
                        "last_written_at": row[2].isoformat() if row[2] else None,
                    }
                    for row in cur.fetchall()
                ],
            }


def seed_blocks_for_tenant(tenant_id: str) -> int:
    """Create the default memory blocks for a new tenant.

    Returns:
        Number of blocks seeded.
    """
    count = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for block_name, block_type, max_chars in DEFAULT_BLOCK_SEEDS:
                cur.execute(
                    "INSERT INTO agent_memory_blocks "
                    "(tenant_id, block_name, block_type, max_chars) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (tenant_id, block_name) DO NOTHING",
                    (tenant_id, block_name, block_type, max_chars),
                )
                count += cur.rowcount
    return count
