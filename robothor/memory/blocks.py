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

from robothor.db import get_connection

logger = logging.getLogger(__name__)


def read_block(block_name: str) -> dict[str, Any]:
    """Read a named memory block and increment its read count.

    Returns:
        dict with block_name, content, last_written_at, or error if not found.
    """
    if not block_name:
        return {"error": "block_name is required"}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_memory_blocks "
                "SET read_count = read_count + 1, last_read_at = NOW() "
                "WHERE block_name = %s "
                "RETURNING content, last_written_at",
                (block_name,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": f"Block '{block_name}' not found"}
            return {
                "block_name": block_name,
                "content": row[0] or "",
                "last_written_at": row[1].isoformat() if row[1] else None,
            }


def write_block(block_name: str, content: str) -> dict[str, Any]:
    """Write or update a named memory block.

    Uses UPSERT — creates the block if it doesn't exist, updates if it does.

    Returns:
        dict with success status and block_name.
    """
    if not block_name:
        return {"error": "block_name is required"}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_memory_blocks (block_name, content, last_written_at, write_count) "
                "VALUES (%s, %s, NOW(), 1) "
                "ON CONFLICT (block_name) DO UPDATE "
                "SET content = EXCLUDED.content, last_written_at = NOW(), "
                "    write_count = agent_memory_blocks.write_count + 1 "
                "RETURNING id",
                (block_name, content),
            )
            return {"success": True, "block_name": block_name}


def list_blocks() -> dict[str, Any]:
    """List all memory blocks with their sizes and timestamps.

    Returns:
        dict with a 'blocks' list, each entry containing name, size, last_written_at.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT block_name, length(content) AS size, last_written_at "
                "FROM agent_memory_blocks ORDER BY block_name",
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
