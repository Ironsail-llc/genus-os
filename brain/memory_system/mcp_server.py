"""
MCP Server for Robothor Memory System.

Provides a Model Context Protocol (MCP) interface so external models
(Claude Code, Cursor, etc.) can access the memory system. Runs locally
with stdio transport.

Architecture:
    MCP Client → stdio → this server → fact_extraction/search/entity_graph → PostgreSQL

Tools:
    - Memory: search_memory, store_memory, get_stats, get_entity
    - Vision: look, who_is_here, enroll_face, set_vision_mode
    - Memory blocks: memory_block_read, memory_block_write, memory_block_list
    - CRM interaction: log_interaction
    - CRM People: create_person, get_person, update_person, list_people, delete_person
    - CRM Companies: create_company, get_company, update_company, list_companies, delete_company
    - CRM Notes: create_note, get_note, list_notes, update_note, delete_note
    - CRM Tasks: create_task, get_task, list_tasks, update_task, delete_task
    - CRM Metadata: get_metadata_objects, get_object_metadata, search_records
    - CRM Conversations: list_conversations, get_conversation,
      list_messages, create_message, toggle_conversation_status

Dependencies:
    - mcp library for protocol handling
    - fact_extraction.py for storage and search
    - rag.py for memory stats
    - entity_graph.py for entity lookups
    - crm_dal.py for CRM operations (native PostgreSQL)
"""

import asyncio
import json
import os
import sys
from typing import Any

import httpx
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from robothor.memory.facts import extract_facts, get_memory_stats, search_facts, store_fact

# Import CRM DAL from bridge directory
sys.path.insert(0, os.path.expanduser("~/robothor/crm/bridge"))
import crm_dal


# Service URL lookups with fallback
def _svc_url(name: str, path: str = "") -> str:
    try:
        from service_registry import get_service_url

        url = get_service_url(name, path)
        if url:
            return url
    except ImportError:
        pass
    _fallback = {"bridge": "http://localhost:9100", "vision": "http://localhost:8600"}
    return f"{_fallback.get(name, 'http://localhost')}{path}"


VENV_PYTHON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "venv",
    "bin",
    "python",
)
MCP_SCRIPT = os.path.abspath(__file__)


def get_tool_definitions() -> list[dict]:
    """Return the list of MCP tool definitions.

    Returns:
        List of tool definition dicts with name, description, and inputSchema.
    """
    return [
        {
            "name": "search_memory",
            "description": "Search Robothor's memory for facts semantically related to a query.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "store_memory",
            "description": "Store new content in Robothor's memory. Extracts facts automatically.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The content to store",
                    },
                    "content_type": {
                        "type": "string",
                        "description": "Type of content: conversation, email, decision, preference, technical",
                    },
                },
                "required": ["content", "content_type"],
            },
        },
        {
            "name": "get_stats",
            "description": "Get memory system statistics.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "get_entity",
            "description": "Look up an entity and its relationships in the knowledge graph.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The entity name to look up",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "look",
            "description": "Look through the webcam — capture a snapshot and analyze it with vision AI. Returns a scene description.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What to look for or analyze (default: general scene description)",
                    },
                },
            },
        },
        {
            "name": "who_is_here",
            "description": "Check who is currently visible or detected by the vision system.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "enroll_face",
            "description": "Enroll a person's face for future recognition. Person must be visible to the camera.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the person to enroll",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "set_vision_mode",
            "description": "Switch the vision service mode. Modes: disarmed (no processing), basic (motion only), armed (full YOLO + face ID + escalation).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": "Vision mode: disarmed, basic, or armed",
                        "enum": ["disarmed", "basic", "armed"],
                    },
                },
                "required": ["mode"],
            },
        },
        {
            "name": "memory_block_read",
            "description": "Read a named memory block. Blocks are persistent, structured working memory (e.g., persona, user_profile, working_context, operational_findings, contacts_summary).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "block_name": {
                        "type": "string",
                        "description": "Name of the memory block to read",
                    },
                },
                "required": ["block_name"],
            },
        },
        {
            "name": "memory_block_write",
            "description": "Write/replace the content of a named memory block. Content is truncated to the block's max_chars limit.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "block_name": {
                        "type": "string",
                        "description": "Name of the memory block to write",
                    },
                    "content": {
                        "type": "string",
                        "description": "New content for the block (replaces existing)",
                    },
                },
                "required": ["block_name", "content"],
            },
        },
        {
            "name": "memory_block_list",
            "description": "List all memory blocks with their sizes, types, and last-updated timestamps.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "log_interaction",
            "description": "Log an interaction to the CRM layer (Chatwoot + Twenty). Creates conversation records and resolves contacts across systems.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "contact_name": {
                        "type": "string",
                        "description": "Name of the contact",
                    },
                    "channel": {
                        "type": "string",
                        "description": "Channel: email, telegram, voice, web, gchat, api",
                    },
                    "direction": {
                        "type": "string",
                        "description": "Direction: incoming or outgoing",
                        "enum": ["incoming", "outgoing"],
                    },
                    "content_summary": {
                        "type": "string",
                        "description": "Brief summary of the interaction",
                    },
                    "channel_identifier": {
                        "type": "string",
                        "description": "Channel-specific identifier (email address, phone number, etc.)",
                    },
                },
                "required": ["contact_name", "channel", "direction", "content_summary"],
            },
        },
        # ─── CRM People Tools ───────────────────────────────────────────
        {
            "name": "create_person",
            "description": "Create a person in the CRM. Returns the new person's ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "firstName": {"type": "string", "description": "First name"},
                    "lastName": {"type": "string", "description": "Last name"},
                    "email": {"type": "string", "description": "Email address"},
                    "phone": {"type": "string", "description": "Phone number"},
                },
                "required": ["firstName"],
            },
        },
        {
            "name": "get_person",
            "description": "Get a person's full profile by ID, including company info.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Person UUID"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "update_person",
            "description": "Update a person's fields. Only provided fields are changed.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Person UUID"},
                    "firstName": {"type": "string"},
                    "lastName": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "jobTitle": {"type": "string"},
                    "city": {"type": "string"},
                    "companyId": {"type": "string", "description": "Company UUID to link"},
                    "linkedinUrl": {"type": "string"},
                    "avatarUrl": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "list_people",
            "description": "List people in the CRM, optionally filtered by search term.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Search by name"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
            },
        },
        {
            "name": "delete_person",
            "description": "Delete a person from the CRM (soft delete).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Person UUID"},
                },
                "required": ["id"],
            },
        },
        # ─── CRM Company Tools ──────────────────────────────────────────
        {
            "name": "create_company",
            "description": "Create a company in the CRM. Returns the new company's ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Company name"},
                    "domainName": {"type": "string", "description": "Website domain"},
                    "employees": {"type": "integer", "description": "Number of employees"},
                    "address": {"type": "string", "description": "Street address"},
                    "linkedinUrl": {"type": "string"},
                    "idealCustomerProfile": {"type": "boolean", "default": False},
                },
                "required": ["name"],
            },
        },
        {
            "name": "get_company",
            "description": "Get a company's profile by ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Company UUID"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "update_company",
            "description": "Update a company's fields. Only provided fields are changed.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Company UUID"},
                    "name": {"type": "string"},
                    "domainName": {"type": "string"},
                    "employees": {"type": "integer"},
                    "address": {"type": "string"},
                    "linkedinUrl": {"type": "string"},
                    "idealCustomerProfile": {"type": "boolean"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "list_companies",
            "description": "List companies in the CRM, optionally filtered by name.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Search by company name"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "delete_company",
            "description": "Delete a company from the CRM (soft delete).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Company UUID"},
                },
                "required": ["id"],
            },
        },
        # ─── CRM Note Tools ─────────────────────────────────────────────
        {
            "name": "create_note",
            "description": "Create a note in the CRM, optionally linked to a person or company.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title"},
                    "body": {"type": "string", "description": "Note body"},
                    "personId": {"type": "string", "description": "Person UUID to link"},
                    "companyId": {"type": "string", "description": "Company UUID to link"},
                },
                "required": ["title", "body"],
            },
        },
        {
            "name": "get_note",
            "description": "Get a note by ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Note UUID"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "list_notes",
            "description": "List notes, optionally filtered by person or company.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "personId": {"type": "string", "description": "Filter by person UUID"},
                    "companyId": {"type": "string", "description": "Filter by company UUID"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "update_note",
            "description": "Update a note's fields.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Note UUID"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "personId": {"type": "string"},
                    "companyId": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_note",
            "description": "Delete a note from the CRM (soft delete).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Note UUID"},
                },
                "required": ["id"],
            },
        },
        # ─── CRM Task Tools ─────────────────────────────────────────────
        {
            "name": "create_task",
            "description": "Create a task in the CRM, optionally linked to a person or company. Use assignedToAgent for agent-to-agent coordination.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title"},
                    "body": {"type": "string", "description": "Task description"},
                    "status": {
                        "type": "string",
                        "description": "Status: TODO, IN_PROGRESS, DONE",
                        "default": "TODO",
                    },
                    "dueAt": {"type": "string", "description": "Due date (ISO 8601)"},
                    "personId": {"type": "string", "description": "Person UUID to link"},
                    "companyId": {"type": "string", "description": "Company UUID to link"},
                    "createdByAgent": {
                        "type": "string",
                        "description": "Agent ID that created this task",
                    },
                    "assignedToAgent": {
                        "type": "string",
                        "description": "Agent ID to assign (e.g. email-responder, supervisor)",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Priority: low, normal, high, urgent",
                        "default": "normal",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for categorization",
                    },
                    "parentTaskId": {
                        "type": "string",
                        "description": "Parent task UUID for subtask chains",
                    },
                },
                "required": ["title"],
            },
        },
        {
            "name": "get_task",
            "description": "Get a task by ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task UUID"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "list_tasks",
            "description": "List tasks, optionally filtered by status, person, agent, tags, or priority.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status"},
                    "personId": {"type": "string", "description": "Filter by person UUID"},
                    "assignedToAgent": {
                        "type": "string",
                        "description": "Filter by assigned agent",
                    },
                    "createdByAgent": {"type": "string", "description": "Filter by creating agent"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by tags (containment)",
                    },
                    "priority": {"type": "string", "description": "Filter by priority"},
                    "excludeResolved": {"type": "boolean", "description": "Exclude resolved tasks"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "update_task",
            "description": "Update a task's fields.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task UUID"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "status": {"type": "string"},
                    "dueAt": {"type": "string"},
                    "personId": {"type": "string"},
                    "companyId": {"type": "string"},
                    "assignedToAgent": {"type": "string", "description": "Reassign to agent"},
                    "priority": {"type": "string", "description": "New priority"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New tags",
                    },
                    "resolution": {"type": "string", "description": "Resolution summary"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "delete_task",
            "description": "Delete a task from the CRM (soft delete).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task UUID"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "resolve_task",
            "description": "Mark a task as DONE with a resolution summary.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task UUID"},
                    "resolution": {
                        "type": "string",
                        "description": "What was done to complete the task",
                    },
                },
                "required": ["id", "resolution"],
            },
        },
        {
            "name": "list_agent_tasks",
            "description": "List tasks assigned to a specific agent, ordered by priority.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agentId": {"type": "string", "description": "Agent ID to get tasks for"},
                    "status": {"type": "string", "description": "Filter by status"},
                    "includeUnassigned": {
                        "type": "boolean",
                        "description": "Include unassigned tasks",
                        "default": False,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
                "required": ["agentId"],
            },
        },
        # ─── CRM Metadata Tools ─────────────────────────────────────────
        {
            "name": "get_metadata_objects",
            "description": "List available CRM object types (tables) and their labels.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "get_object_metadata",
            "description": "Get column definitions for a CRM object type.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "objectName": {
                        "type": "string",
                        "description": "Table name: crm_people, crm_companies, crm_notes, crm_tasks, crm_conversations, crm_messages",
                    },
                },
                "required": ["objectName"],
            },
        },
        {
            "name": "search_records",
            "description": "Search across CRM tables by keyword.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword"},
                    "objectName": {"type": "string", "description": "Limit to specific table"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        },
        # ─── CRM Conversation Tools ─────────────────────────────────────
        {
            "name": "list_conversations",
            "description": "List CRM conversations filtered by status (open, resolved, pending, snoozed).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status (default: open)",
                        "default": "open",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (default: 1)",
                        "default": 1,
                    },
                },
            },
        },
        {
            "name": "get_conversation",
            "description": "Get a single conversation by ID with contact info.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversationId": {"type": "integer", "description": "Conversation ID"},
                },
                "required": ["conversationId"],
            },
        },
        {
            "name": "list_messages",
            "description": "List all messages in a conversation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversationId": {"type": "integer", "description": "Conversation ID"},
                },
                "required": ["conversationId"],
            },
        },
        {
            "name": "create_message",
            "description": "Create a message in a conversation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversationId": {"type": "integer", "description": "Conversation ID"},
                    "content": {"type": "string", "description": "Message content"},
                    "messageType": {
                        "type": "string",
                        "description": "incoming or outgoing (default: outgoing)",
                        "default": "outgoing",
                    },
                    "private": {
                        "type": "boolean",
                        "description": "Private note (default: false)",
                        "default": False,
                    },
                },
                "required": ["conversationId", "content"],
            },
        },
        {
            "name": "toggle_conversation_status",
            "description": "Change a conversation's status (open, resolved, pending, snoozed).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversationId": {"type": "integer", "description": "Conversation ID"},
                    "status": {
                        "type": "string",
                        "description": "New status: open, resolved, pending, snoozed",
                    },
                },
                "required": ["conversationId", "status"],
            },
        },
    ]


async def handle_tool_call(name: str, arguments: dict[str, Any]) -> dict:
    """Handle an MCP tool call.

    Args:
        name: The tool name.
        arguments: The tool arguments.

    Returns:
        Result dictionary.
    """
    if name == "search_memory":
        query = arguments.get("query", "")
        limit = arguments.get("limit", 10)
        results = await search_facts(
            query,
            limit=limit,
            use_reranker=True,
            expand_entities=True,
        )
        return {
            "results": [
                {
                    "fact": r["fact_text"],
                    "category": r.get("category", ""),
                    "confidence": r.get("confidence", 0),
                    "similarity": round(r.get("similarity", 0), 4),
                }
                for r in results
            ]
        }

    elif name == "store_memory":
        content = arguments.get("content", "")
        content_type = arguments.get("content_type", "conversation")
        facts = await extract_facts(content)
        if facts:
            stored_ids = []
            for fact in facts:
                fact_id = await store_fact(fact, content, content_type)
                stored_ids.append(fact_id)
            return {"id": stored_ids[0], "facts_stored": len(stored_ids)}
        else:
            # Store as a single fact if extraction fails
            fact = {
                "fact_text": content,
                "category": "personal",
                "entities": [],
                "confidence": 0.5,
            }
            fact_id = await store_fact(fact, content, content_type)
            return {"id": fact_id, "facts_stored": 1}

    elif name == "get_stats":
        stats = get_memory_stats()
        return stats

    elif name == "get_entity":
        entity_name = arguments.get("name", "")
        # Placeholder — will be wired to entity_graph.get_entity() in Phase 4
        try:
            from robothor.memory.entities import get_entity

            result = await get_entity(entity_name)
            return result or {"name": entity_name, "found": False}
        except ImportError:
            return {"name": entity_name, "found": False, "note": "Entity graph not yet available"}

    elif name == "look":
        prompt = arguments.get("prompt", "Describe what you see in this image in detail.")
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    _svc_url("vision", "/look"),
                    json={"prompt": prompt},
                )
                resp.raise_for_status()
                data = resp.json()
                return {
                    "description": data.get("description", ""),
                    "snapshot_path": data.get("snapshot_path", ""),
                }
        except Exception as e:
            return {"error": f"Vision look failed: {e}"}

    elif name == "who_is_here":
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(_svc_url("vision", "/health"))
                resp.raise_for_status()
                data = resp.json()
                return {
                    "people_present": data.get("people_present", []),
                    "running": data.get("running", False),
                    "mode": data.get("mode"),
                    "last_detection": data.get("last_detection"),
                }
        except Exception as e:
            return {"error": f"Vision status check failed: {e}"}

    elif name == "enroll_face":
        face_name = arguments.get("name", "")
        if not face_name:
            return {"error": "Name is required for face enrollment"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _svc_url("vision", "/enroll"),
                    json={"name": face_name},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            return {"error": f"Face enrollment failed: {e}"}

    elif name == "set_vision_mode":
        mode = arguments.get("mode", "")
        if mode not in ("disarmed", "basic", "armed"):
            return {"error": f"Invalid mode: {mode}. Valid: disarmed, basic, armed"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _svc_url("vision", "/mode"),
                    json={"mode": mode},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            return {"error": f"Mode switch failed: {e}"}

    elif name == "memory_block_read":
        block_name = arguments.get("block_name", "")
        from robothor.memory.blocks import read_block

        return read_block(block_name)

    elif name == "memory_block_write":
        block_name = arguments.get("block_name", "")
        content = arguments.get("content", "")
        from robothor.memory.blocks import write_block

        return write_block(block_name, content)

    elif name == "memory_block_list":
        from robothor.memory.blocks import list_blocks

        return list_blocks()

    elif name == "log_interaction":
        contact_name = arguments.get("contact_name", "")
        channel = arguments.get("channel", "api")
        direction = arguments.get("direction", "outgoing")
        content_summary = arguments.get("content_summary", "")
        channel_identifier = arguments.get("channel_identifier", contact_name)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    _svc_url("bridge", "/log-interaction"),
                    json={
                        "contact_name": contact_name,
                        "channel": channel,
                        "direction": direction,
                        "content_summary": content_summary,
                        "channel_identifier": channel_identifier,
                    },
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            return {"error": f"Failed to log interaction: {e}"}

    # ─── CRM People Tools ───────────────────────────────────────────

    elif name == "create_person":
        person_id = crm_dal.create_person(
            arguments.get("firstName", ""),
            arguments.get("lastName", ""),
            arguments.get("email"),
            arguments.get("phone"),
        )
        if person_id:
            return {"id": person_id, "firstName": arguments.get("firstName", "")}
        return {"error": "Failed to create person"}

    elif name == "get_person":
        result = crm_dal.get_person(arguments["id"])
        return result or {"error": "Person not found"}

    elif name == "update_person":
        pid = arguments.pop("id")
        field_map = {
            "firstName": "first_name",
            "lastName": "last_name",
            "email": "email",
            "phone": "phone",
            "jobTitle": "job_title",
            "city": "city",
            "companyId": "company_id",
            "linkedinUrl": "linkedin_url",
            "avatarUrl": "avatar_url",
        }
        kwargs = {}
        for arg_key, dal_key in field_map.items():
            if arg_key in arguments:
                kwargs[dal_key] = arguments[arg_key]
        ok = crm_dal.update_person(pid, **kwargs)
        return {"success": ok, "id": pid}

    elif name == "list_people":
        results = crm_dal.list_people(
            search=arguments.get("search"),
            limit=arguments.get("limit", 20),
        )
        return {"people": results, "count": len(results)}

    elif name == "delete_person":
        ok = crm_dal.delete_person(arguments["id"])
        return {"success": ok, "id": arguments["id"]}

    # ─── CRM Company Tools ──────────────────────────────────────────

    elif name == "create_company":
        company_id = crm_dal.create_company(
            name=arguments.get("name", ""),
            domain_name=arguments.get("domainName"),
            employees=arguments.get("employees"),
            address=arguments.get("address"),
            linkedin_url=arguments.get("linkedinUrl"),
            ideal_customer_profile=arguments.get("idealCustomerProfile", False),
        )
        if company_id:
            return {"id": company_id, "name": arguments.get("name", "")}
        return {"error": "Failed to create company"}

    elif name == "get_company":
        result = crm_dal.get_company(arguments["id"])
        return result or {"error": "Company not found"}

    elif name == "update_company":
        cid = arguments.pop("id")
        field_map = {
            "name": "name",
            "domainName": "domain_name",
            "employees": "employees",
            "address": "address",
            "linkedinUrl": "linkedin_url",
            "idealCustomerProfile": "ideal_customer_profile",
        }
        kwargs = {}
        for arg_key, dal_key in field_map.items():
            if arg_key in arguments:
                kwargs[dal_key] = arguments[arg_key]
        ok = crm_dal.update_company(cid, **kwargs)
        return {"success": ok, "id": cid}

    elif name == "list_companies":
        results = crm_dal.list_companies(
            search=arguments.get("search"),
            limit=arguments.get("limit", 50),
        )
        return {"companies": results, "count": len(results)}

    elif name == "delete_company":
        ok = crm_dal.delete_company(arguments["id"])
        return {"success": ok, "id": arguments["id"]}

    # ─── CRM Note Tools ─────────────────────────────────────────────

    elif name == "create_note":
        note_id = crm_dal.create_note(
            title=arguments.get("title", ""),
            body=arguments.get("body", ""),
            person_id=arguments.get("personId"),
            company_id=arguments.get("companyId"),
        )
        if note_id:
            return {"id": note_id, "title": arguments.get("title", "")}
        return {"error": "Failed to create note"}

    elif name == "get_note":
        result = crm_dal.get_note(arguments["id"])
        return result or {"error": "Note not found"}

    elif name == "list_notes":
        results = crm_dal.list_notes(
            person_id=arguments.get("personId"),
            company_id=arguments.get("companyId"),
            limit=arguments.get("limit", 50),
        )
        return {"notes": results, "count": len(results)}

    elif name == "update_note":
        nid = arguments.pop("id")
        field_map = {
            "title": "title",
            "body": "body",
            "personId": "person_id",
            "companyId": "company_id",
        }
        kwargs = {}
        for arg_key, dal_key in field_map.items():
            if arg_key in arguments:
                kwargs[dal_key] = arguments[arg_key]
        ok = crm_dal.update_note(nid, **kwargs)
        return {"success": ok, "id": nid}

    elif name == "delete_note":
        ok = crm_dal.delete_note(arguments["id"])
        return {"success": ok, "id": arguments["id"]}

    # ─── CRM Task Tools ─────────────────────────────────────────────

    elif name == "create_task":
        task_id = crm_dal.create_task(
            title=arguments.get("title", ""),
            body=arguments.get("body"),
            status=arguments.get("status", "TODO"),
            due_at=arguments.get("dueAt"),
            person_id=arguments.get("personId"),
            company_id=arguments.get("companyId"),
            created_by_agent=arguments.get("createdByAgent"),
            assigned_to_agent=arguments.get("assignedToAgent"),
            priority=arguments.get("priority", "normal"),
            tags=arguments.get("tags"),
            parent_task_id=arguments.get("parentTaskId"),
        )
        if task_id:
            return {"id": task_id, "title": arguments.get("title", "")}
        return {"error": "Failed to create task"}

    elif name == "get_task":
        result = crm_dal.get_task(arguments["id"])
        return result or {"error": "Task not found"}

    elif name == "list_tasks":
        results = crm_dal.list_tasks(
            status=arguments.get("status"),
            person_id=arguments.get("personId"),
            limit=arguments.get("limit", 50),
            assigned_to_agent=arguments.get("assignedToAgent"),
            created_by_agent=arguments.get("createdByAgent"),
            tags=arguments.get("tags"),
            priority=arguments.get("priority"),
            exclude_resolved=arguments.get("excludeResolved", False),
        )
        return {"tasks": results, "count": len(results)}

    elif name == "update_task":
        tid = arguments.pop("id")
        field_map = {
            "title": "title",
            "body": "body",
            "status": "status",
            "dueAt": "due_at",
            "personId": "person_id",
            "companyId": "company_id",
            "assignedToAgent": "assigned_to_agent",
            "priority": "priority",
            "tags": "tags",
            "resolution": "resolution",
        }
        kwargs = {}
        for arg_key, dal_key in field_map.items():
            if arg_key in arguments:
                kwargs[dal_key] = arguments[arg_key]
        ok = crm_dal.update_task(tid, **kwargs)
        return {"success": ok, "id": tid}

    elif name == "delete_task":
        ok = crm_dal.delete_task(arguments["id"])
        return {"success": ok, "id": arguments["id"]}

    elif name == "resolve_task":
        ok = crm_dal.resolve_task(arguments["id"], arguments.get("resolution", ""))
        return {"success": ok, "id": arguments["id"]}

    elif name == "list_agent_tasks":
        results = crm_dal.list_agent_tasks(
            agent_id=arguments.get("agentId", ""),
            include_unassigned=arguments.get("includeUnassigned", False),
            status=arguments.get("status"),
            limit=arguments.get("limit", 50),
        )
        return {"tasks": results, "count": len(results)}

    # ─── CRM Metadata Tools ─────────────────────────────────────────

    elif name == "get_metadata_objects":
        return {"objects": crm_dal.get_metadata_objects()}

    elif name == "get_object_metadata":
        result = crm_dal.get_object_metadata(arguments.get("objectName", ""))
        return result or {"error": "Object not found"}

    elif name == "search_records":
        results = crm_dal.search_records(
            query=arguments.get("query", ""),
            object_name=arguments.get("objectName"),
            limit=arguments.get("limit", 20),
        )
        return {"results": results, "count": len(results)}

    # ─── CRM Conversation Tools ─────────────────────────────────────

    elif name == "list_conversations":
        return crm_dal.list_conversations(
            status=arguments.get("status", "open"),
            page=arguments.get("page", 1),
        )

    elif name == "get_conversation":
        result = crm_dal.get_conversation(arguments["conversationId"])
        return result or {"error": "Conversation not found"}

    elif name == "list_messages":
        messages = crm_dal.list_messages(arguments["conversationId"])
        return {"payload": messages}

    elif name == "create_message":
        result = crm_dal.send_message(
            conversation_id=arguments["conversationId"],
            content=arguments.get("content", ""),
            message_type=arguments.get("messageType", "outgoing"),
            private=arguments.get("private", False),
        )
        return result or {"error": "Failed to create message"}

    elif name == "toggle_conversation_status":
        result = crm_dal.toggle_conversation_status(
            conversation_id=arguments["conversationId"],
            status=arguments.get("status", "resolved"),
        )
        return result or {"error": "Failed to toggle status"}

    return {"error": f"Unknown tool: {name}"}


def create_server() -> Server:
    """Create and configure the MCP server.

    Returns:
        Configured MCP Server instance.
    """
    server = Server("robothor-memory")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        definitions = get_tool_definitions()
        return [
            types.Tool(
                name=d["name"],
                description=d["description"],
                inputSchema=d["inputSchema"],
            )
            for d in definitions
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        result = await handle_tool_call(name, arguments or {})
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    return server


def get_claude_config() -> dict:
    """Generate the MCP server config for .claude.json.

    Returns:
        Dict with the MCP server configuration.
    """
    return {
        "robothor-memory": {
            "type": "stdio",
            "command": VENV_PYTHON,
            "args": [MCP_SCRIPT],
        }
    }


async def main():
    """Run the MCP server with stdio transport."""
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
