"""
Tool Registry for the Agent Engine.

Maps tool names to:
1. OpenAI function-calling JSON schemas (for litellm)
2. Async Python executors (direct DAL calls, no Bridge HTTP)

Schemas extracted from robothor/api/mcp.py. Executors call robothor/crm/dal.py directly.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

import httpx

from robothor.engine.models import AgentConfig

logger = logging.getLogger(__name__)

# Vision service URL
VISION_URL = "http://127.0.0.1:8600"

# Bridge service URL (Impetus One passthrough)
BRIDGE_URL = "http://127.0.0.1:9100"

# Impetus One tools — routed via Bridge MCP passthrough
IMPETUS_TOOLS = frozenset({
    "search_patients",
    "get_patient_details",
    "get_patient_clinical_notes",
    "get_patient_prescriptions",
    "search_prescriptions",
    "get_prescription_status",
    "search_medications",
    "search_pharmacies",
    "get_appointments",
    "list_actable_providers",
    "create_prescription_draft",
    "schedule_appointment",
    "transmit_prescription",
})


class ToolRegistry:
    """Registry of available tools with schema filtering per agent."""

    def __init__(self) -> None:
        self._schemas: dict[str, dict] = {}
        self._executors: dict[str, Any] = {}
        self._register_all()

    def _register_all(self) -> None:
        """Register all tool schemas and executors."""
        from robothor.api.mcp import get_tool_definitions

        for defn in get_tool_definitions():
            name = defn["name"]
            self._schemas[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": defn["description"],
                    "parameters": defn["inputSchema"],
                },
            }

        # Also register shell/file tools not in MCP
        self._schemas["exec"] = {
            "type": "function",
            "function": {
                "name": "exec",
                "description": "Execute a shell command (30s timeout). Use for gog CLI, file operations, etc.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command to execute",
                        },
                    },
                    "required": ["command"],
                },
            },
        }
        self._schemas["read_file"] = {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the contents of a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path (relative to workspace or absolute)",
                        },
                    },
                    "required": ["path"],
                },
            },
        }
        self._schemas["write_file"] = {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write content to a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path (relative to workspace or absolute)",
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        }
        self._schemas["web_fetch"] = {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "Fetch a web page and return its content as markdown text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to fetch",
                        },
                    },
                    "required": ["url"],
                },
            },
        }
        self._schemas["web_search"] = {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web via SearXNG and return results.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 5)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            },
        }

        # ── Voice / outbound calling ──

        self._schemas["make_call"] = {
            "type": "function",
            "function": {
                "name": "make_call",
                "description": "Make an outbound phone call via Robothor's voice server. The call connects to Gemini Live for real-time AI conversation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "string",
                            "description": "Phone number to call in E.164 format (e.g. +12125551234)",
                        },
                        "recipient": {
                            "type": "string",
                            "description": "Name of person being called (for conversation context)",
                        },
                        "purpose": {
                            "type": "string",
                            "description": "Why Robothor is calling (used in the AI's system prompt)",
                        },
                    },
                    "required": ["to", "purpose"],
                },
            },
        }

        # ── Agent observability tools ──

        self._schemas["list_agent_runs"] = {
            "type": "function",
            "function": {
                "name": "list_agent_runs",
                "description": "List recent agent runs with optional filters. Returns run ID, agent, status, duration, model, timestamps.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "Filter by agent ID (e.g. 'vision-monitor', 'email-classifier')",
                        },
                        "status": {
                            "type": "string",
                            "description": "Filter by status: running, completed, failed, timeout",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 20)",
                            "default": 20,
                        },
                    },
                },
            },
        }
        self._schemas["get_agent_run"] = {
            "type": "function",
            "function": {
                "name": "get_agent_run",
                "description": "Get details of a specific agent run including its step-by-step audit trail (tool calls, durations, errors).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "description": "The run UUID",
                        },
                    },
                    "required": ["run_id"],
                },
            },
        }
        self._schemas["list_agent_schedules"] = {
            "type": "function",
            "function": {
                "name": "list_agent_schedules",
                "description": "List all agent schedules with cron expressions, last run info, and next run times.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "enabled_only": {
                            "type": "boolean",
                            "description": "Only show enabled schedules (default true)",
                            "default": True,
                        },
                    },
                },
            },
        }
        self._schemas["get_agent_stats"] = {
            "type": "function",
            "function": {
                "name": "get_agent_stats",
                "description": "Get aggregated stats for an agent: total runs, failures, timeouts, avg duration, token usage, cost over the last N hours.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "Agent ID to get stats for",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "Lookback window in hours (default 24)",
                            "default": 24,
                        },
                    },
                    "required": ["agent_id"],
                },
            },
        }

        # ── Vault tools ──

        self._schemas["vault_get"] = {
            "type": "function",
            "function": {
                "name": "vault_get",
                "description": "Retrieve a decrypted secret from the vault by key.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Secret key (e.g. 'telegram/bot_token', 'openai/api_key')",
                        },
                    },
                    "required": ["key"],
                },
            },
        }
        self._schemas["vault_set"] = {
            "type": "function",
            "function": {
                "name": "vault_set",
                "description": "Store an encrypted secret in the vault.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Secret key (e.g. 'telegram/bot_token')",
                        },
                        "value": {
                            "type": "string",
                            "description": "Secret value to encrypt and store",
                        },
                        "category": {
                            "type": "string",
                            "description": "Category: credential, oauth_token, api_key, certificate (default: credential)",
                            "default": "credential",
                        },
                    },
                    "required": ["key", "value"],
                },
            },
        }
        self._schemas["vault_list"] = {
            "type": "function",
            "function": {
                "name": "vault_list",
                "description": "List secret keys in the vault (not values). Optionally filter by category.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": "Filter by category: credential, oauth_token, api_key, certificate",
                        },
                    },
                },
            },
        }
        self._schemas["vault_delete"] = {
            "type": "function",
            "function": {
                "name": "vault_delete",
                "description": "Delete a secret from the vault.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Secret key to delete",
                        },
                    },
                    "required": ["key"],
                },
            },
        }

        # ── Convenience aliases ──

        self._schemas["list_my_tasks"] = {
            "type": "function",
            "function": {
                "name": "list_my_tasks",
                "description": "List tasks assigned to you (the current agent). Shortcut for list_agent_tasks with your own agent ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "description": "Filter by status: TODO, IN_PROGRESS, REVIEW, DONE",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 50)",
                            "default": 50,
                        },
                    },
                },
            },
        }

        # ── CRM Merge tools ──

        self._schemas["merge_people"] = {
            "type": "function",
            "function": {
                "name": "merge_people",
                "description": "Merge two duplicate people. Keeper is preserved, loser is soft-deleted. Fills empty fields, collects emails/phones, re-links conversations/notes/tasks.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keeperId": {
                            "type": "string",
                            "description": "UUID of the person to keep",
                        },
                        "loserId": {
                            "type": "string",
                            "description": "UUID of the person to merge into keeper and delete",
                        },
                    },
                    "required": ["keeperId", "loserId"],
                },
            },
        }
        # merge_contacts is an alias for merge_people
        self._schemas["merge_contacts"] = {
            "type": "function",
            "function": {
                "name": "merge_contacts",
                "description": "Merge two duplicate contacts (alias for merge_people). Keeper is preserved, loser is soft-deleted.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keeperId": {
                            "type": "string",
                            "description": "UUID of the contact to keep",
                        },
                        "loserId": {
                            "type": "string",
                            "description": "UUID of the contact to merge into keeper and delete",
                        },
                    },
                    "required": ["keeperId", "loserId"],
                },
            },
        }
        self._schemas["merge_companies"] = {
            "type": "function",
            "function": {
                "name": "merge_companies",
                "description": "Merge two duplicate companies. Keeper is preserved, loser is soft-deleted. Fills empty fields, re-links people and notes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keeperId": {
                            "type": "string",
                            "description": "UUID of the company to keep",
                        },
                        "loserId": {
                            "type": "string",
                            "description": "UUID of the company to merge into keeper and delete",
                        },
                    },
                    "required": ["keeperId", "loserId"],
                },
            },
        }

    def build_for_agent(self, config: AgentConfig) -> list[dict]:
        """Return filtered tool schemas for an agent based on allow/deny lists."""
        if config.tools_allowed:
            names = [n for n in config.tools_allowed if n in self._schemas]
        else:
            names = list(self._schemas.keys())

        if config.tools_denied:
            names = [n for n in names if n not in config.tools_denied]

        return [self._schemas[n] for n in names]

    def get_tool_names(self, config: AgentConfig) -> list[str]:
        """Return filtered tool names for an agent."""
        if config.tools_allowed:
            names = [n for n in config.tools_allowed if n in self._schemas]
        else:
            names = list(self._schemas.keys())
        if config.tools_denied:
            names = [n for n in names if n not in config.tools_denied]
        return names

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        agent_id: str = "",
        tenant_id: str = "robothor-primary",
        workspace: str = "",
    ) -> dict[str, Any]:
        """Execute a tool and return the result dict.

        Routes to the appropriate handler — direct DAL calls for CRM tools,
        HTTP for vision, subprocess for exec, etc.
        """
        try:
            return await _execute_tool(
                tool_name, arguments,
                agent_id=agent_id,
                tenant_id=tenant_id,
                workspace=workspace,
            )
        except Exception as e:
            logger.error("Tool %s failed: %s", tool_name, e, exc_info=True)
            return {"error": f"Tool execution failed: {e}"}


# Singleton
_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """Get or create the singleton tool registry."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


# ─── Tool Execution Router ───────────────────────────────────────────


async def _execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    agent_id: str = "",
    tenant_id: str = "robothor-primary",
    workspace: str = "",
) -> dict[str, Any]:
    """Route tool call to the correct handler."""

    # ── Memory tools ──

    if name == "search_memory":
        from robothor.memory.facts import search_facts
        results = await search_facts(args.get("query", ""), limit=args.get("limit", 10))
        return {
            "results": [
                {
                    "fact": r["fact_text"],
                    "category": r["category"],
                    "confidence": r["confidence"],
                    "similarity": round(r.get("similarity", 0), 4),
                }
                for r in results
            ]
        }

    if name == "store_memory":
        from robothor.memory.facts import extract_facts, store_fact
        content = args.get("content", "")
        content_type = args.get("content_type", "conversation")
        facts = await extract_facts(content)
        if facts:
            stored_ids = [await store_fact(f, content, content_type) for f in facts]
            return {"id": stored_ids[0], "facts_stored": len(stored_ids)}
        fact = {"fact_text": content, "category": "personal", "entities": [], "confidence": 0.5}
        fact_id = await store_fact(fact, content, content_type)
        return {"id": fact_id, "facts_stored": 1}

    if name == "get_stats":
        from robothor.memory.tiers import get_memory_stats
        return get_memory_stats()

    if name == "get_entity":
        from robothor.memory.entities import get_entity
        try:
            result = await get_entity(args.get("name", ""))
            return result or {"name": args.get("name", ""), "found": False}
        except Exception:
            return {"name": args.get("name", ""), "found": False}

    # ── Vision tools (HTTP proxy to vision service) ──

    if name == "look":
        prompt = args.get("prompt", "Describe what you see in this image in detail.")
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(f"{VISION_URL}/look", json={"prompt": prompt})
            resp.raise_for_status()
            return resp.json()

    if name == "who_is_here":
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{VISION_URL}/health")
            resp.raise_for_status()
            data = resp.json()
            return {
                "people_present": data.get("people_present", []),
                "running": data.get("running", False),
                "mode": data.get("mode"),
                "last_detection": data.get("last_detection"),
            }

    if name == "enroll_face":
        face_name = args.get("name", "")
        if not face_name:
            return {"error": "Name is required for face enrollment"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{VISION_URL}/enroll", json={"name": face_name})
            resp.raise_for_status()
            return resp.json()

    if name == "set_vision_mode":
        mode = args.get("mode", "")
        if mode not in ("disarmed", "basic", "armed"):
            return {"error": f"Invalid mode: {mode}. Valid: disarmed, basic, armed"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{VISION_URL}/mode", json={"mode": mode})
            resp.raise_for_status()
            return resp.json()

    # ── Memory block tools (direct DAL) ──

    if name == "memory_block_read":
        from robothor.memory.blocks import read_block
        return read_block(args.get("block_name", ""))

    if name == "memory_block_write":
        from robothor.memory.blocks import write_block
        return write_block(args.get("block_name", ""), args.get("content", ""))

    if name == "memory_block_list":
        from robothor.memory.blocks import list_blocks
        return list_blocks()

    if name == "append_to_block":
        from robothor.crm.dal import append_to_block
        ok = append_to_block(
            block_name=args.get("block_name", ""),
            entry=args.get("entry", ""),
            max_entries=args.get("maxEntries", 20),
        )
        return {"success": ok, "block_name": args.get("block_name", "")}

    # ── CRM People (direct DAL) ──

    if name == "create_person":
        from robothor.crm.dal import create_person
        person_id = create_person(
            args.get("firstName", ""),
            args.get("lastName", ""),
            args.get("email"),
            args.get("phone"),
            tenant_id=tenant_id,
        )
        return (
            {"id": person_id, "firstName": args.get("firstName", "")}
            if person_id
            else {"error": "Failed to create person"}
        )

    if name == "get_person":
        from robothor.crm.dal import get_person
        return get_person(args["id"], tenant_id=tenant_id) or {"error": "Person not found"}

    if name == "update_person":
        from robothor.crm.dal import update_person
        pid = args.get("id", "")
        field_map = {
            "firstName": "first_name", "lastName": "last_name",
            "email": "email", "phone": "phone", "jobTitle": "job_title",
            "city": "city", "companyId": "company_id",
            "linkedinUrl": "linkedin_url", "avatarUrl": "avatar_url",
        }
        kwargs = {dal_key: args[k] for k, dal_key in field_map.items() if k in args and k != "id"}
        return {"success": update_person(pid, tenant_id=tenant_id, **kwargs), "id": pid}

    if name == "list_people":
        from robothor.crm.dal import list_people
        results = list_people(
            search=args.get("search"), limit=args.get("limit", 20), tenant_id=tenant_id
        )
        return {"people": results, "count": len(results)}

    if name == "delete_person":
        from robothor.crm.dal import delete_person
        return {"success": delete_person(args["id"], tenant_id=tenant_id), "id": args["id"]}

    # ── CRM Companies (direct DAL) ──

    if name == "create_company":
        from robothor.crm.dal import create_company
        company_id = create_company(
            name=args.get("name", ""),
            domain_name=args.get("domainName"),
            employees=args.get("employees"),
            address=args.get("address"),
            linkedin_url=args.get("linkedinUrl"),
            ideal_customer_profile=args.get("idealCustomerProfile", False),
            tenant_id=tenant_id,
        )
        return (
            {"id": company_id, "name": args.get("name", "")}
            if company_id
            else {"error": "Failed to create company"}
        )

    if name == "get_company":
        from robothor.crm.dal import get_company
        return get_company(args["id"], tenant_id=tenant_id) or {"error": "Company not found"}

    if name == "update_company":
        from robothor.crm.dal import update_company
        cid = args.get("id", "")
        field_map = {
            "name": "name", "domainName": "domain_name", "employees": "employees",
            "address": "address", "linkedinUrl": "linkedin_url",
            "idealCustomerProfile": "ideal_customer_profile",
        }
        kwargs = {dal_key: args[k] for k, dal_key in field_map.items() if k in args and k != "id"}
        return {"success": update_company(cid, tenant_id=tenant_id, **kwargs), "id": cid}

    if name == "list_companies":
        from robothor.crm.dal import list_companies
        results = list_companies(
            search=args.get("search"), limit=args.get("limit", 50), tenant_id=tenant_id
        )
        return {"companies": results, "count": len(results)}

    if name == "delete_company":
        from robothor.crm.dal import delete_company
        return {"success": delete_company(args["id"], tenant_id=tenant_id), "id": args["id"]}

    # ── CRM Notes (direct DAL) ──

    if name == "create_note":
        from robothor.crm.dal import create_note
        note_id = create_note(
            title=args.get("title", ""),
            body=args.get("body", ""),
            person_id=args.get("personId"),
            company_id=args.get("companyId"),
            tenant_id=tenant_id,
        )
        return (
            {"id": note_id, "title": args.get("title", "")}
            if note_id
            else {"error": "Failed to create note"}
        )

    if name == "get_note":
        from robothor.crm.dal import get_note
        return get_note(args["id"], tenant_id=tenant_id) or {"error": "Note not found"}

    if name == "list_notes":
        from robothor.crm.dal import list_notes
        results = list_notes(
            person_id=args.get("personId"),
            company_id=args.get("companyId"),
            limit=args.get("limit", 50),
            tenant_id=tenant_id,
        )
        return {"notes": results, "count": len(results)}

    if name == "update_note":
        from robothor.crm.dal import update_note
        nid = args.get("id", "")
        field_map = {"title": "title", "body": "body", "personId": "person_id", "companyId": "company_id"}
        kwargs = {dal_key: args[k] for k, dal_key in field_map.items() if k in args and k != "id"}
        return {"success": update_note(nid, tenant_id=tenant_id, **kwargs), "id": nid}

    if name == "delete_note":
        from robothor.crm.dal import delete_note
        return {"success": delete_note(args["id"], tenant_id=tenant_id), "id": args["id"]}

    # ── CRM Tasks (direct DAL) ──

    if name == "create_task":
        from robothor.crm.dal import create_task
        task_id = create_task(
            title=args.get("title", ""),
            body=args.get("body"),
            status=args.get("status", "TODO"),
            due_at=args.get("dueAt"),
            person_id=args.get("personId"),
            company_id=args.get("companyId"),
            assigned_to_agent=args.get("assignedToAgent"),
            created_by_agent=args.get("createdByAgent", agent_id),
            priority=args.get("priority", "normal"),
            tags=args.get("tags"),
            parent_task_id=args.get("parentTaskId"),
            tenant_id=tenant_id,
        )
        return (
            {"id": task_id, "title": args.get("title", "")}
            if task_id
            else {"error": "Failed to create task"}
        )

    if name == "get_task":
        from robothor.crm.dal import get_task
        return get_task(args["id"], tenant_id=tenant_id) or {"error": "Task not found"}

    if name == "list_tasks":
        from robothor.crm.dal import list_tasks
        results = list_tasks(
            status=args.get("status"),
            person_id=args.get("personId"),
            assigned_to_agent=args.get("assignedToAgent"),
            created_by_agent=args.get("createdByAgent"),
            priority=args.get("priority"),
            tags=args.get("tags"),
            exclude_resolved=args.get("excludeResolved", False),
            limit=args.get("limit", 50),
            tenant_id=tenant_id,
        )
        return {"tasks": results, "count": len(results)}

    if name == "update_task":
        from robothor.crm.dal import update_task
        tid = args.get("id", "")
        field_map = {
            "title": "title", "body": "body", "status": "status",
            "dueAt": "due_at", "personId": "person_id", "companyId": "company_id",
            "assignedToAgent": "assigned_to_agent", "priority": "priority",
            "tags": "tags", "resolution": "resolution",
        }
        kwargs = {dal_key: args[k] for k, dal_key in field_map.items() if k in args and k != "id"}
        return {"success": update_task(tid, tenant_id=tenant_id, **kwargs), "id": tid}

    if name == "delete_task":
        from robothor.crm.dal import delete_task
        return {"success": delete_task(args["id"], tenant_id=tenant_id), "id": args["id"]}

    if name == "resolve_task":
        from robothor.crm.dal import resolve_task
        ok = resolve_task(task_id=args["id"], resolution=args.get("resolution", ""), tenant_id=tenant_id)
        return {"success": ok, "id": args["id"]}

    if name == "list_agent_tasks":
        from robothor.crm.dal import list_agent_tasks
        results = list_agent_tasks(
            agent_id=args.get("agentId", agent_id),
            include_unassigned=args.get("includeUnassigned", False),
            status=args.get("status"),
            limit=args.get("limit", 50),
            tenant_id=tenant_id,
        )
        return {"tasks": results, "count": len(results)}

    # list_my_tasks is an alias for list_agent_tasks with the current agent
    if name == "list_my_tasks":
        from robothor.crm.dal import list_agent_tasks
        results = list_agent_tasks(
            agent_id=agent_id,
            include_unassigned=False,
            status=args.get("status"),
            limit=args.get("limit", 50),
            tenant_id=tenant_id,
        )
        return {"tasks": results, "count": len(results)}

    # ── Task Review Workflow ──

    if name == "approve_task":
        from robothor.crm.dal import approve_task
        result = approve_task(
            task_id=args["id"],
            resolution=args.get("resolution", "Approved"),
            reviewer=agent_id or "engine",
            tenant_id=tenant_id,
        )
        if isinstance(result, dict) and "error" in result:
            return result
        return {"success": True, "id": args["id"]}

    if name == "reject_task":
        from robothor.crm.dal import reject_task
        result = reject_task(
            task_id=args["id"],
            reason=args.get("reason", ""),
            reviewer=agent_id or "engine",
            change_requests=args.get("changeRequests"),
            tenant_id=tenant_id,
        )
        if isinstance(result, dict) and "error" in result:
            return result
        return {"success": True, "id": args["id"]}

    # ── Notifications ──

    if name == "send_notification":
        from robothor.crm.dal import send_notification
        nid = send_notification(
            from_agent=args.get("fromAgent", agent_id),
            to_agent=args.get("toAgent", ""),
            notification_type=args.get("notificationType", ""),
            subject=args.get("subject", ""),
            body=args.get("body"),
            metadata=args.get("metadata"),
            task_id=args.get("taskId"),
            tenant_id=tenant_id,
        )
        return {"id": nid, "subject": args.get("subject", "")} if nid else {"error": "Failed to send notification"}

    if name == "get_inbox":
        from robothor.crm.dal import get_agent_inbox
        results = get_agent_inbox(
            agent_id=args.get("agentId", agent_id),
            unread_only=args.get("unreadOnly", True),
            type_filter=args.get("typeFilter"),
            limit=args.get("limit", 50),
            tenant_id=tenant_id,
        )
        return {"notifications": results, "count": len(results)}

    if name == "ack_notification":
        from robothor.crm.dal import acknowledge_notification
        ok = acknowledge_notification(args.get("notificationId", ""), tenant_id=tenant_id)
        return {"success": ok, "id": args.get("notificationId", "")}

    # ── CRM Interaction ──

    if name == "log_interaction":
        # Use Bridge for interaction logging (it does contact resolution)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "http://127.0.0.1:9100/log-interaction",
                json={k: args.get(k, "") for k in [
                    "contact_name", "channel", "direction",
                    "content_summary", "channel_identifier",
                ]},
            )
            resp.raise_for_status()
            return resp.json()

    # ── CRM Metadata ──

    if name == "get_metadata_objects":
        from robothor.crm.dal import get_metadata_objects
        return {"objects": get_metadata_objects()}

    if name == "get_object_metadata":
        from robothor.crm.dal import get_object_metadata
        return get_object_metadata(args.get("objectName", "")) or {"error": "Object not found"}

    if name == "search_records":
        from robothor.crm.dal import search_records
        results = search_records(
            query=args.get("query", ""),
            object_name=args.get("objectName"),
            limit=args.get("limit", 20),
            tenant_id=tenant_id,
        )
        return {"results": results, "count": len(results)}

    # ── CRM Conversations ──

    if name == "list_conversations":
        from robothor.crm.dal import list_conversations
        convos = list_conversations(
            status=args.get("status", "open"),
            page=args.get("page", 1),
            tenant_id=tenant_id,
        )
        return {"conversations": convos, "count": len(convos)}

    if name == "get_conversation":
        from robothor.crm.dal import get_conversation
        return get_conversation(args["conversationId"], tenant_id=tenant_id) or {"error": "Conversation not found"}

    if name == "list_messages":
        from robothor.crm.dal import list_messages
        return {"payload": list_messages(args["conversationId"], tenant_id=tenant_id)}

    if name == "create_message":
        from robothor.crm.dal import send_message
        result = send_message(
            conversation_id=args["conversationId"],
            content=args.get("content", ""),
            message_type=args.get("messageType", "outgoing"),
            private=args.get("private", False),
            tenant_id=tenant_id,
        )
        return dict(result) if result else {"error": "Failed to create message"}

    if name == "toggle_conversation_status":
        from robothor.crm.dal import toggle_conversation_status
        ok = toggle_conversation_status(
            conversation_id=args["conversationId"],
            status=args.get("status", "resolved"),
            tenant_id=tenant_id,
        )
        return {"success": ok, "conversationId": args["conversationId"]}

    # ── Agent Observability (tracking DAL) ──

    if name == "list_agent_runs":
        from robothor.engine.tracking import list_runs
        runs = list_runs(
            agent_id=args.get("agent_id"),
            status=args.get("status"),
            limit=args.get("limit", 20),
            tenant_id=tenant_id,
        )
        return {
            "runs": [
                {
                    "id": r["id"],
                    "agent_id": r["agent_id"],
                    "status": r["status"],
                    "trigger_type": r.get("trigger_type"),
                    "model_used": r.get("model_used"),
                    "duration_ms": r.get("duration_ms"),
                    "input_tokens": r.get("input_tokens"),
                    "output_tokens": r.get("output_tokens"),
                    "total_cost_usd": float(r["total_cost_usd"]) if r.get("total_cost_usd") else None,
                    "started_at": str(r["started_at"]) if r.get("started_at") else None,
                    "completed_at": str(r["completed_at"]) if r.get("completed_at") else None,
                    "error_message": r.get("error_message"),
                }
                for r in runs
            ],
            "count": len(runs),
        }

    if name == "get_agent_run":
        from robothor.engine.tracking import get_run, list_steps
        run = get_run(args["run_id"])
        if not run:
            return {"error": "Run not found"}
        steps = list_steps(args["run_id"])
        return {
            "run": {
                "id": run["id"],
                "agent_id": run["agent_id"],
                "status": run["status"],
                "trigger_type": run.get("trigger_type"),
                "trigger_detail": run.get("trigger_detail"),
                "model_used": run.get("model_used"),
                "models_attempted": run.get("models_attempted"),
                "duration_ms": run.get("duration_ms"),
                "input_tokens": run.get("input_tokens"),
                "output_tokens": run.get("output_tokens"),
                "total_cost_usd": float(run["total_cost_usd"]) if run.get("total_cost_usd") else None,
                "started_at": str(run["started_at"]) if run.get("started_at") else None,
                "completed_at": str(run["completed_at"]) if run.get("completed_at") else None,
                "error_message": run.get("error_message"),
                "delivery_status": run.get("delivery_status"),
            },
            "steps": [
                {
                    "step_number": s["step_number"],
                    "step_type": s["step_type"],
                    "tool_name": s.get("tool_name"),
                    "duration_ms": s.get("duration_ms"),
                    "error_message": s.get("error_message"),
                }
                for s in steps
            ],
            "step_count": len(steps),
        }

    if name == "list_agent_schedules":
        from robothor.engine.tracking import list_schedules
        schedules = list_schedules(
            enabled_only=args.get("enabled_only", True),
            tenant_id=tenant_id,
        )
        return {
            "schedules": [
                {
                    "agent_id": s["agent_id"],
                    "enabled": s["enabled"],
                    "cron_expr": s.get("cron_expr"),
                    "timezone": s.get("timezone"),
                    "timeout_seconds": s.get("timeout_seconds"),
                    "model_primary": s.get("model_primary"),
                    "last_run_at": str(s["last_run_at"]) if s.get("last_run_at") else None,
                    "last_status": s.get("last_status"),
                    "last_duration_ms": s.get("last_duration_ms"),
                    "next_run_at": str(s["next_run_at"]) if s.get("next_run_at") else None,
                    "consecutive_errors": s.get("consecutive_errors", 0),
                }
                for s in schedules
            ],
            "count": len(schedules),
        }

    if name == "get_agent_stats":
        from robothor.engine.tracking import get_agent_stats as _get_agent_stats
        stats = _get_agent_stats(
            agent_id=args["agent_id"],
            hours=args.get("hours", 24),
            tenant_id=tenant_id,
        )
        # Convert Decimals to floats for JSON serialization
        return {
            "agent_id": args["agent_id"],
            "hours": args.get("hours", 24),
            "total_runs": stats.get("total_runs", 0),
            "completed": stats.get("completed", 0),
            "failed": stats.get("failed", 0),
            "timeouts": stats.get("timeouts", 0),
            "avg_duration_ms": round(float(stats["avg_duration_ms"])) if stats.get("avg_duration_ms") else None,
            "total_input_tokens": stats.get("total_input_tokens"),
            "total_output_tokens": stats.get("total_output_tokens"),
            "total_cost_usd": float(stats["total_cost_usd"]) if stats.get("total_cost_usd") else None,
        }

    # ── Vault tools ──

    if name == "vault_get":
        import robothor.vault as vault
        value = vault.get(args["key"], tenant_id=tenant_id)
        if value is None:
            return {"error": f"Secret not found: {args['key']}"}
        return {"key": args["key"], "value": value}

    if name == "vault_set":
        import robothor.vault as vault
        vault.set(args["key"], args["value"], category=args.get("category", "credential"), tenant_id=tenant_id)
        return {"success": True, "key": args["key"]}

    if name == "vault_list":
        import robothor.vault as vault
        keys = vault.list(category=args.get("category"), tenant_id=tenant_id)
        return {"keys": keys, "count": len(keys)}

    if name == "vault_delete":
        import robothor.vault as vault
        deleted = vault.delete(args["key"], tenant_id=tenant_id)
        return {"success": deleted, "key": args["key"]}

    # ── Shell execution ──

    if name == "exec":
        command = args.get("command", "")
        if not command:
            return {"error": "No command provided"}
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=workspace or None,
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            return {
                "stdout": result.stdout[:4000],
                "stderr": result.stderr[:2000],
                "exit_code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"error": "Command timed out (30s limit)"}
        except Exception as e:
            return {"error": f"Command failed: {e}"}

    # ── File I/O ──

    if name == "read_file":
        from pathlib import Path
        path = Path(args.get("path", ""))
        if not path.is_absolute() and workspace:
            path = Path(workspace) / path
        try:
            content = path.read_text()
            return {"content": content[:50000], "path": str(path), "chars": len(content)}
        except Exception as e:
            return {"error": f"Failed to read file: {e}"}

    if name == "write_file":
        from pathlib import Path
        path = Path(args.get("path", ""))
        if not path.is_absolute() and workspace:
            path = Path(workspace) / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content", ""))
            return {"success": True, "path": str(path)}
        except Exception as e:
            return {"error": f"Failed to write file: {e}"}

    # ── Web tools ──

    if name == "web_fetch":
        url = args.get("url", "")
        if not url:
            return {"error": "No URL provided"}
        try:
            import html2text
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                h = html2text.HTML2Text()
                h.ignore_links = False
                h.body_width = 0
                text = h.handle(resp.text)
                return {"content": text[:20000], "url": str(resp.url), "status": resp.status_code}
        except ImportError:
            return {"error": "html2text not installed"}
        except Exception as e:
            return {"error": f"Fetch failed: {e}"}

    if name == "web_search":
        query = args.get("query", "")
        limit = args.get("limit", 5)
        if not query:
            return {"error": "No query provided"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "http://127.0.0.1:8888/search",
                    params={"q": query, "format": "json", "pageno": 1},
                )
                resp.raise_for_status()
                data = resp.json()
                results = [
                    {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
                    for r in data.get("results", [])[:limit]
                ]
                return {"results": results, "count": len(results)}
        except Exception as e:
            return {"error": f"Search failed: {e}"}

    # ── CRM Merge tools ──

    if name in ("merge_people", "merge_contacts"):
        from robothor.crm.dal import merge_people as _merge_people
        result = _merge_people(
            keeper_id=args.get("keeperId", ""),
            loser_id=args.get("loserId", ""),
            tenant_id=tenant_id,
        )
        if result:
            return {"success": True, "keeper": result}
        return {"error": "Merge failed — one or both IDs not found"}

    if name == "merge_companies":
        from robothor.crm.dal import merge_companies as _merge_companies
        result = _merge_companies(
            keeper_id=args.get("keeperId", ""),
            loser_id=args.get("loserId", ""),
            tenant_id=tenant_id,
        )
        if result:
            return {"success": True, "keeper": result}
        return {"error": "Merge failed — one or both IDs not found"}

    # ── Voice / outbound calling ──

    if name == "make_call":
        to_number = args.get("to", "")
        recipient = args.get("recipient", "someone")
        purpose = args.get("purpose", "")
        if not to_number:
            return {"error": "Missing 'to' phone number"}
        if not purpose:
            return {"error": "Missing 'purpose' for the call"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "http://127.0.0.1:8765/call",
                    json={"to": to_number, "recipient": recipient, "purpose": purpose},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            return {"error": f"Call failed: {e}"}

    # ── Impetus One (Bridge MCP passthrough) ──

    if name in IMPETUS_TOOLS:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{BRIDGE_URL}/api/impetus/tools/call",
                json={"name": name, "arguments": args},
            )
            resp.raise_for_status()
            return resp.json()

    return {"error": f"Unknown tool: {name}"}
