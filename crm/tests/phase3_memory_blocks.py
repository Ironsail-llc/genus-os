#!/usr/bin/env python3
"""
Phase 3 tests for agent memory blocks.
Tests the DB table and MCP tool handlers directly.
"""

import sys

import psycopg2
from psycopg2.extras import RealDictCursor

DB = {"dbname": "robothor_memory", "user": "philip", "host": "/var/run/postgresql"}
PASS = FAIL = 0


def test(name, result):
    global PASS, FAIL
    if result:
        print(f"  PASS: {name}")
        PASS += 1
    else:
        print(f"  FAIL: {name}")
        FAIL += 1


conn = psycopg2.connect(**DB)
cur = conn.cursor(cursor_factory=RealDictCursor)

# T3.1: Table exists
cur.execute("SELECT to_regclass('public.agent_memory_blocks');")
test("T3.1 Table exists", cur.fetchone()["to_regclass"] is not None)

# T3.2: Core blocks seeded
cur.execute("SELECT count(*) as cnt FROM agent_memory_blocks WHERE block_type = 'core';")
test("T3.2 Core blocks seeded (>=4)", cur.fetchone()["cnt"] >= 4)

# T3.3: Block names are correct
cur.execute("SELECT array_agg(block_name ORDER BY block_name) as names FROM agent_memory_blocks;")
names = cur.fetchone()["names"]
for expected in [
    "persona",
    "user_profile",
    "working_context",
    "operational_findings",
    "contacts_summary",
]:
    test(f"T3.3 Block '{expected}' exists", expected in names)

# T3.4: Write and read round-trip
TEST_CONTENT = "TDD test content — round trip verification"
cur.execute(
    "UPDATE agent_memory_blocks SET content = %s, write_count = write_count + 1 "
    "WHERE block_name = 'working_context' RETURNING block_name;",
    (TEST_CONTENT,),
)
conn.commit()
cur.execute("SELECT content FROM agent_memory_blocks WHERE block_name = 'working_context';")
test("T3.4 Write/read round-trip", cur.fetchone()["content"] == TEST_CONTENT)

# T3.5: max_chars enforcement (application-level, not DB-level)
cur.execute("SELECT max_chars FROM agent_memory_blocks WHERE block_name = 'working_context';")
max_chars = cur.fetchone()["max_chars"]
test("T3.5 max_chars set", max_chars > 0 and max_chars <= 10000)

# T3.6: UNIQUE constraint on block_name
try:
    cur.execute(
        "INSERT INTO agent_memory_blocks (block_name, content) VALUES ('persona', 'duplicate');"
    )
    conn.commit()
    test("T3.6 UNIQUE constraint", False)  # Should have raised
except psycopg2.errors.UniqueViolation:
    conn.rollback()
    test("T3.6 UNIQUE constraint enforced", True)

# T3.7: MCP tool definitions include memory block tools
sys.path.insert(0, "/home/philip/clawd/memory_system")
try:
    from mcp_server import get_tool_definitions

    tools = get_tool_definitions()
    tool_names = [t["name"] for t in tools]
    test("T3.7a memory_block_read in MCP tools", "memory_block_read" in tool_names)
    test("T3.7b memory_block_write in MCP tools", "memory_block_write" in tool_names)
    test("T3.7c memory_block_list in MCP tools", "memory_block_list" in tool_names)
    test("T3.7d log_interaction in MCP tools", "log_interaction" in tool_names)
except Exception as e:
    test(f"T3.7 MCP tool import failed: {e}", False)

# T3.8: MCP handler round-trip via handle_tool_call
import asyncio

try:
    from mcp_server import handle_tool_call

    # Test memory_block_list
    result = asyncio.run(handle_tool_call("memory_block_list", {}))
    test(
        "T3.8a memory_block_list returns blocks", "blocks" in result and len(result["blocks"]) >= 5
    )

    # Test memory_block_read
    result = asyncio.run(handle_tool_call("memory_block_read", {"block_name": "persona"}))
    test(
        "T3.8b memory_block_read returns content",
        "content" in result and len(result["content"]) > 0,
    )

    # Test memory_block_write
    test_val = "Phase 3 test write"
    result = asyncio.run(
        handle_tool_call(
            "memory_block_write", {"block_name": "working_context", "content": test_val}
        )
    )
    test("T3.8c memory_block_write succeeds", result.get("chars_written", 0) > 0)

    # Verify the write persisted
    result = asyncio.run(handle_tool_call("memory_block_read", {"block_name": "working_context"}))
    test("T3.8d memory_block_write persisted", result.get("content") == test_val)

except Exception as e:
    test(f"T3.8 MCP handler test failed: {e}", False)

# Cleanup test data
cur.execute("UPDATE agent_memory_blocks SET content = '' WHERE block_name = 'working_context';")
conn.commit()
conn.close()

print(f"\n=== Phase 3: {PASS} passed, {FAIL} failed ===")
print("READY FOR PHASE 4" if FAIL == 0 else "FIX FAILURES BEFORE PROCEEDING")
sys.exit(FAIL)
