"""
Tool Registry for the Agent Engine.

Maps tool names to:
1. OpenAI function-calling JSON schemas (for litellm)
2. Async Python executors (direct DAL calls, no Bridge HTTP)

Schemas extracted from robothor/api/mcp.py. Executors call robothor/crm/dal.py directly.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import httpx

from robothor.engine.models import AgentConfig, SpawnContext

if TYPE_CHECKING:
    from robothor.engine.runner import AgentRunner

logger = logging.getLogger(__name__)


def _cfg():
    """Lazy config access (not module-level to avoid import-time side effects)."""
    from robothor.config import get_config

    return get_config()


# Impetus One tools — routed via Bridge MCP passthrough
IMPETUS_TOOLS = frozenset(
    {
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
    }
)


# ─── Runner reference (for spawn_agent tool) ─────────────────────────
# Follows the same pattern as delivery.py's set_telegram_sender()

_runner_ref: AgentRunner | None = None


def set_runner(runner: AgentRunner) -> None:
    """Register the runner instance (called by daemon on startup)."""
    global _runner_ref
    _runner_ref = runner


def get_runner() -> AgentRunner | None:
    """Get the registered runner instance."""
    return _runner_ref


# ─── Spawn context (async-safe via contextvars) ──────────────────────

_current_spawn_context: ContextVar[SpawnContext | None] = ContextVar(
    "_current_spawn_context", default=None
)

# ─── Concurrency semaphore for sub-agent spawns ──────────────────────

_spawn_semaphore: asyncio.Semaphore | None = None
MAX_CONCURRENT_SPAWNS = 3


def _get_spawn_semaphore() -> asyncio.Semaphore:
    """Get or create the spawn concurrency semaphore."""
    global _spawn_semaphore
    if _spawn_semaphore is None:
        _spawn_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SPAWNS)
    return _spawn_semaphore


# ─── Spawn tool names ────────────────────────────────────────────────

SPAWN_TOOLS = frozenset({"spawn_agent", "spawn_agents"})

# ── Git tools (Nightwatch system) ────────────────────────────────────

GIT_TOOLS = frozenset(
    {"git_status", "git_diff", "git_branch", "git_commit", "git_push", "create_pull_request"}
)

# Branches that agents are NEVER allowed to push to or commit on
PROTECTED_BRANCHES = frozenset({"main", "master"})

# ─── Read-only tools for plan mode ────────────────────────────────────
# Tools with no side effects — safe to run during exploration phase.

READONLY_TOOLS: frozenset[str] = frozenset(
    {
        # File/system
        "read_file",
        "list_directory",
        # Web
        "web_fetch",
        "web_search",
        # Memory (read)
        "search_memory",
        "get_entity",
        "memory_block_read",
        "memory_block_list",
        # CRM read
        "list_conversations",
        "get_conversation",
        "list_messages",
        "list_people",
        "get_person",
        "list_companies",
        "get_company",
        "list_notes",
        "get_note",
        "list_tasks",
        "list_my_tasks",
        "get_task",
        "search_records",
        "get_metadata_objects",
        "get_object_metadata",
        "get_inbox",
        # Vision (read)
        "look",
        "who_is_here",
        "list_enrolled_faces",
        # Engine status
        "list_agent_runs",
        "get_agent_run",
        "list_agent_schedules",
        "get_agent_stats",
        # Vault (read)
        "vault_get",
        "vault_list",
        # Healthcare (read)
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
        # Reasoning
        "deep_reason",
        # PDF
        "analyze_pdf",
        # Git (read-only)
        "git_status",
        "git_diff",
    }
)


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
        self._schemas["list_directory"] = {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": "List files and directories. Use to discover file paths before reading them.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory path (relative to workspace or absolute)",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob filter, e.g. '*.yaml', '*.md' (optional)",
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "Search subdirectories (default false)",
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
                        "provider": {
                            "type": "string",
                            "description": "Search provider: 'searxng' (default, free/private) or 'perplexity' (AI-powered, requires API key)",
                            "enum": ["searxng", "perplexity"],
                            "default": "searxng",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

        self._schemas["analyze_pdf"] = {
            "type": "function",
            "function": {
                "name": "analyze_pdf",
                "description": "Analyze a PDF file. Extracts text directly, or uses vision AI for image-based PDFs. Optionally answers a specific question about the PDF content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the PDF file (relative to workspace or absolute)",
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional question to answer about the PDF content",
                        },
                        "pages": {
                            "type": "string",
                            "description": "Page range to analyze (e.g. '1-5', '3,7,10'). Default: first 10 pages.",
                        },
                    },
                    "required": ["path"],
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

        # ── Deep reasoning (RLM) ──

        self._schemas["deep_reason"] = {
            "type": "function",
            "function": {
                "name": "deep_reason",
                "description": (
                    "Run a deep research session using an RLM (Recursive Language Model). "
                    "The RLM writes Python code in a REPL to search the web, execute shell commands, "
                    "read files, query memory, and recursively investigate multi-source questions. "
                    "Best for: codebase analysis, fact-checking across sources, complex investigations. "
                    "EXPENSIVE ($0.50-$2.00/call) — use only for questions needing deep multi-step research."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The reasoning question to answer",
                        },
                        "context": {
                            "type": "string",
                            "description": "Optional raw text context to include",
                        },
                        "context_sources": {
                            "type": "array",
                            "description": "Optional list of context sources to pre-load",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "enum": ["memory", "file", "block", "entity"],
                                        "description": "Source type",
                                    },
                                    "query": {
                                        "type": "string",
                                        "description": "Search query (for memory type)",
                                    },
                                    "path": {
                                        "type": "string",
                                        "description": "File path (for file type)",
                                    },
                                    "block_name": {
                                        "type": "string",
                                        "description": "Block name (for block type)",
                                    },
                                    "name": {
                                        "type": "string",
                                        "description": "Entity name (for entity type)",
                                    },
                                    "limit": {
                                        "type": "integer",
                                        "description": "Max results for memory search (default 10)",
                                    },
                                },
                                "required": ["type"],
                            },
                        },
                    },
                    "required": ["query"],
                },
            },
        }

        # ── Git tools (Nightwatch system) ──

        self._schemas["git_status"] = {
            "type": "function",
            "function": {
                "name": "git_status",
                "description": "Show the working tree status (staged, unstaged, untracked files).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Repository path (defaults to workspace root)",
                        },
                    },
                },
            },
        }
        self._schemas["git_diff"] = {
            "type": "function",
            "function": {
                "name": "git_diff",
                "description": "Show staged and unstaged changes. Use staged=true for staged-only diff.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Repository path (defaults to workspace root)",
                        },
                        "staged": {
                            "type": "boolean",
                            "description": "Show only staged changes (default false)",
                            "default": False,
                        },
                    },
                },
            },
        }
        self._schemas["git_branch"] = {
            "type": "function",
            "function": {
                "name": "git_branch",
                "description": "Create and switch to a new branch. Cannot target main/master.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "branch_name": {
                            "type": "string",
                            "description": "Name of the branch to create (e.g. 'nightwatch/2026-03-04/fix-classifier')",
                        },
                        "path": {
                            "type": "string",
                            "description": "Repository path (defaults to workspace root)",
                        },
                    },
                    "required": ["branch_name"],
                },
            },
        }
        self._schemas["git_commit"] = {
            "type": "function",
            "function": {
                "name": "git_commit",
                "description": "Stage specified files (or all changes) and commit with a message. Cannot commit on main/master.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "Commit message",
                        },
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Files to stage (empty = stage all changes)",
                        },
                        "path": {
                            "type": "string",
                            "description": "Repository path (defaults to workspace root)",
                        },
                    },
                    "required": ["message"],
                },
            },
        }
        self._schemas["git_push"] = {
            "type": "function",
            "function": {
                "name": "git_push",
                "description": "Push current branch to origin. Cannot push to main/master.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Repository path (defaults to workspace root)",
                        },
                        "set_upstream": {
                            "type": "boolean",
                            "description": "Set upstream tracking (-u flag, default true for new branches)",
                            "default": True,
                        },
                    },
                },
            },
        }
        self._schemas["create_pull_request"] = {
            "type": "function",
            "function": {
                "name": "create_pull_request",
                "description": "Create a draft pull request on GitHub using gh CLI. Always creates as draft, auto-labels 'nightwatch'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "PR title (keep under 70 chars)",
                        },
                        "body": {
                            "type": "string",
                            "description": "PR body in markdown",
                        },
                        "base": {
                            "type": "string",
                            "description": "Base branch (default 'main')",
                            "default": "main",
                        },
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Additional labels (nightwatch is always added)",
                        },
                        "path": {
                            "type": "string",
                            "description": "Repository path (defaults to workspace root)",
                        },
                    },
                    "required": ["title", "body"],
                },
            },
        }

        # ── Git tool names set ──
        # (used by READONLY_TOOLS and guardrails)

        # ── Sub-agent spawning tools ──

        self._schemas["spawn_agent"] = {
            "type": "function",
            "function": {
                "name": "spawn_agent",
                "description": (
                    "Spawn another agent as a sub-task and wait for its result. "
                    "The child agent runs synchronously within your tool loop and returns "
                    "structured output. Use for delegating focused work to specialist agents."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "ID of the agent to spawn (must have a manifest)",
                        },
                        "message": {
                            "type": "string",
                            "description": "Task message / prompt for the child agent",
                        },
                        "tools_override": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional: replace child's tools_allowed with this list",
                        },
                        "max_iterations": {
                            "type": "integer",
                            "description": "Optional: cap max LLM iterations for the child",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": "Optional: cap timeout for the child run",
                        },
                    },
                    "required": ["agent_id", "message"],
                },
            },
        }
        self._schemas["spawn_agents"] = {
            "type": "function",
            "function": {
                "name": "spawn_agents",
                "description": (
                    "Spawn multiple agents in parallel and wait for all results. "
                    "Max 5 parallel sub-agents. Each runs independently — one failure "
                    "doesn't cancel others."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agents": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "agent_id": {
                                        "type": "string",
                                        "description": "Agent ID to spawn",
                                    },
                                    "message": {
                                        "type": "string",
                                        "description": "Task message for this agent",
                                    },
                                    "tools_override": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Optional tools override",
                                    },
                                },
                                "required": ["agent_id", "message"],
                            },
                            "description": "List of agents to spawn (max 5)",
                        },
                    },
                    "required": ["agents"],
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

        # Exclude spawn tools unless agent has can_spawn_agents enabled
        if not config.can_spawn_agents:
            names = [n for n in names if n not in SPAWN_TOOLS]

        return [self._schemas[n] for n in names]

    def build_readonly_for_agent(self, config: AgentConfig) -> list[dict]:
        """Return only read-only tool schemas for plan mode.

        Intersects the agent's allowed tools with READONLY_TOOLS so the agent
        can explore and read but cannot make changes.
        """
        full_names = set(self.get_tool_names(config))
        readonly_names = sorted(full_names & READONLY_TOOLS)
        return [self._schemas[n] for n in readonly_names if n in self._schemas]

    def get_readonly_tool_names(self, config: AgentConfig) -> list[str]:
        """Return read-only tool names for plan mode."""
        full_names = set(self.get_tool_names(config))
        return sorted(full_names & READONLY_TOOLS)

    def get_tool_names(self, config: AgentConfig) -> list[str]:
        """Return filtered tool names for an agent."""
        if config.tools_allowed:
            names = [n for n in config.tools_allowed if n in self._schemas]
        else:
            names = list(self._schemas.keys())
        if config.tools_denied:
            names = [n for n in names if n not in config.tools_denied]

        # Exclude spawn tools unless agent has can_spawn_agents enabled
        if not config.can_spawn_agents:
            names = [n for n in names if n not in SPAWN_TOOLS]

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
                tool_name,
                arguments,
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

# Tools that use async I/O (httpx, await) and MUST run on the event loop
_ASYNC_TOOLS = (
    frozenset(
        {
            "search_memory",
            "store_memory",
            "get_entity",
            "look",
            "who_is_here",
            "enroll_face",
            "enroll_face_from_image",
            "list_enrolled_faces",
            "unenroll_face",
            "set_vision_mode",
            "log_interaction",
            "web_fetch",
            "web_search",
            "analyze_pdf",
            "make_call",
            "spawn_agent",
            "spawn_agents",
        }
    )
    | IMPETUS_TOOLS
)


async def _execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    agent_id: str = "",
    tenant_id: str = "robothor-primary",
    workspace: str = "",
) -> dict[str, Any]:
    """Route tool call to the correct handler.

    Async-native tools (httpx, awaitable DAL) run on the event loop.
    Sync tools (psycopg2 DAL, subprocess, file I/O) run in a thread
    via asyncio.to_thread() to avoid blocking the event loop.
    """
    if name in _ASYNC_TOOLS:
        return await _handle_async_tool(
            name, args, agent_id=agent_id, tenant_id=tenant_id, workspace=workspace
        )
    return await asyncio.to_thread(
        _handle_sync_tool, name, args, agent_id=agent_id, tenant_id=tenant_id, workspace=workspace
    )


async def _handle_async_tool(
    name: str,
    args: dict[str, Any],
    *,
    agent_id: str = "",
    tenant_id: str = "robothor-primary",
    workspace: str = "",
) -> dict[str, Any]:
    """Handle tools that use async I/O (httpx, awaitable DAL)."""

    # ── Sub-agent spawning ──

    if name == "spawn_agent":
        return await _handle_spawn_agent(args, agent_id=agent_id)

    if name == "spawn_agents":
        return await _handle_spawn_agents(args, agent_id=agent_id)

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
            resp = await client.post(f"{_cfg().vision_url}/look", json={"prompt": prompt})
            resp.raise_for_status()
            return dict(resp.json())

    if name == "who_is_here":
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{_cfg().vision_url}/health")
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
            resp = await client.post(f"{_cfg().vision_url}/enroll", json={"name": face_name})
            resp.raise_for_status()
            return dict(resp.json())

    if name == "enroll_face_from_image":
        face_name = args.get("name", "")
        image_paths = args.get("image_paths", [])
        if not face_name:
            return {"error": "Name is required"}
        if not image_paths:
            return {"error": "image_paths is required"}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{_cfg().vision_url}/enroll-from-image",
                json={"name": face_name, "image_paths": image_paths},
            )
            resp.raise_for_status()
            return dict(resp.json())

    if name == "list_enrolled_faces":
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{_cfg().vision_url}/enrolled")
            resp.raise_for_status()
            return dict(resp.json())

    if name == "unenroll_face":
        face_name = args.get("name", "")
        if not face_name:
            return {"error": "Name is required"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{_cfg().vision_url}/unenroll", json={"name": face_name})
            resp.raise_for_status()
            return dict(resp.json())

    if name == "set_vision_mode":
        mode = args.get("mode", "")
        if mode not in ("disarmed", "basic", "armed"):
            return {"error": f"Invalid mode: {mode}. Valid: disarmed, basic, armed"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{_cfg().vision_url}/mode", json={"mode": mode})
            resp.raise_for_status()
            return dict(resp.json())

    # ── CRM Interaction (httpx) ──

    if name == "log_interaction":
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_cfg().bridge_url}/log-interaction",
                json={
                    k: args.get(k, "")
                    for k in [
                        "contact_name",
                        "channel",
                        "direction",
                        "content_summary",
                        "channel_identifier",
                    ]
                },
            )
            resp.raise_for_status()
            return dict(resp.json())

    # ── Web tools (httpx) ──

    if name == "web_fetch":
        url = args.get("url", "")
        if not url:
            return {"error": "No URL provided"}
        try:
            import html2text

            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                # Strip HTML comments (potential injection vector)
                import re as _re

                cleaned = _re.sub(r"<!--.*?-->", "", resp.text, flags=_re.DOTALL)
                h = html2text.HTML2Text()
                h.ignore_links = False
                h.body_width = 0
                text = h.handle(cleaned)
                return {"content": text[:8000], "url": str(resp.url), "status": resp.status_code}
        except ImportError:
            return {"error": "html2text not installed"}
        except Exception as e:
            return {"error": f"Fetch failed: {e}"}

    if name == "analyze_pdf":
        return await _handle_analyze_pdf(args, workspace=workspace)

    if name == "web_search":
        query = args.get("query", "")
        limit = args.get("limit", 5)
        provider = args.get("provider", "searxng")
        if not query:
            return {"error": "No query provided"}

        if provider == "perplexity":
            try:
                from robothor.rag.web_search import search_perplexity

                results = await search_perplexity(query, limit=limit)
                return {"results": results, "count": len(results), "provider": "perplexity"}
            except Exception as e:
                return {"error": f"Perplexity search failed: {e}"}

        # Default: SearXNG
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{_cfg().searxng_url}/search",
                    params={"q": query, "format": "json", "pageno": 1},
                )
                resp.raise_for_status()
                data = resp.json()
                results = [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", ""),
                    }
                    for r in data.get("results", [])[:limit]
                ]
                return {"results": results, "count": len(results), "provider": "searxng"}
        except Exception as e:
            return {"error": f"Search failed: {e}"}

    # ── Voice / outbound calling (httpx) ──

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
                    f"{_cfg().voice_url}/call",
                    json={"to": to_number, "recipient": recipient, "purpose": purpose},
                )
                resp.raise_for_status()
                return dict(resp.json())
        except Exception as e:
            return {"error": f"Call failed: {e}"}

    # ── Impetus One (Bridge MCP passthrough) ──

    if name in IMPETUS_TOOLS:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_cfg().bridge_url}/api/impetus/tools/call",
                json={"name": name, "arguments": args},
            )
            resp.raise_for_status()
            return dict(resp.json())

    return {"error": f"Unknown async tool: {name}"}


def _handle_sync_tool(
    name: str,
    args: dict[str, Any],
    *,
    agent_id: str = "",
    tenant_id: str = "robothor-primary",
    workspace: str = "",
) -> dict[str, Any]:
    """Handle tools that use sync I/O (psycopg2, subprocess, file I/O).

    Called via asyncio.to_thread() to avoid blocking the event loop.
    """

    # ── Memory stats (sync) ──

    if name == "get_stats":
        from robothor.memory.facts import get_memory_stats

        return get_memory_stats()

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
            "name": "name",
            "domainName": "domain_name",
            "employees": "employees",
            "address": "address",
            "linkedinUrl": "linkedin_url",
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
        field_map = {
            "title": "title",
            "body": "body",
            "personId": "person_id",
            "companyId": "company_id",
        }
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
            requires_human=args.get("requiresHuman", False),
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
            requires_human=args.get("requiresHuman"),
            limit=args.get("limit", 50),
            tenant_id=tenant_id,
        )
        return {"tasks": results, "count": len(results)}

    if name == "update_task":
        from robothor.crm.dal import update_task

        tid = args.get("id", "")
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
            "requiresHuman": "requires_human",
        }
        kwargs = {dal_key: args[k] for k, dal_key in field_map.items() if k in args and k != "id"}
        return {"success": update_task(tid, tenant_id=tenant_id, **kwargs), "id": tid}

    if name == "delete_task":
        from robothor.crm.dal import delete_task

        return {"success": delete_task(args["id"], tenant_id=tenant_id), "id": args["id"]}

    if name == "resolve_task":
        from robothor.crm.dal import resolve_task

        ok = resolve_task(
            task_id=args["id"], resolution=args.get("resolution", ""), tenant_id=tenant_id
        )
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

        approve_result = approve_task(
            task_id=args["id"],
            resolution=args.get("resolution", "Approved"),
            reviewer=agent_id or "engine",
            tenant_id=tenant_id,
        )
        if isinstance(approve_result, dict) and "error" in approve_result:
            return approve_result
        return {"success": True, "id": args["id"]}

    if name == "reject_task":
        from robothor.crm.dal import reject_task

        reject_result = reject_task(
            task_id=args["id"],
            reason=args.get("reason", ""),
            reviewer=agent_id or "engine",
            change_requests=args.get("changeRequests"),
            tenant_id=tenant_id,
        )
        if isinstance(reject_result, dict) and "error" in reject_result:
            return reject_result
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
        return (
            {"id": nid, "subject": args.get("subject", "")}
            if nid
            else {"error": "Failed to send notification"}
        )

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

        return get_conversation(args["conversationId"], tenant_id=tenant_id) or {
            "error": "Conversation not found"
        }

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
                    "total_cost_usd": float(r["total_cost_usd"])
                    if r.get("total_cost_usd")
                    else None,
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
                "total_cost_usd": float(run["total_cost_usd"])
                if run.get("total_cost_usd")
                else None,
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
            "avg_duration_ms": round(float(stats["avg_duration_ms"]))
            if stats.get("avg_duration_ms")
            else None,
            "total_input_tokens": stats.get("total_input_tokens"),
            "total_output_tokens": stats.get("total_output_tokens"),
            "total_cost_usd": float(stats["total_cost_usd"])
            if stats.get("total_cost_usd")
            else None,
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

        vault.set(
            args["key"],
            args["value"],
            category=args.get("category", "credential"),
            tenant_id=tenant_id,
        )
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
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=workspace or None,
            )
            return {
                "stdout": proc.stdout[:4000],
                "stderr": proc.stderr[:2000],
                "exit_code": proc.returncode,
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

    if name == "list_directory":
        from pathlib import Path

        path = Path(args.get("path", ""))
        if not path.is_absolute() and workspace:
            path = Path(workspace) / path
        if not path.exists():
            return {"error": f"Path does not exist: {path}"}
        if not path.is_dir():
            return {"error": f"Not a directory: {path}"}
        try:
            pattern = args.get("pattern", "")
            recursive = args.get("recursive", False)
            entries = []
            max_entries = 200
            if pattern:
                gen = path.rglob(pattern) if recursive else path.glob(pattern)
                for p in gen:
                    entries.append(
                        {
                            "name": str(p.relative_to(path)),
                            "type": "dir" if p.is_dir() else "file",
                            "size": p.stat().st_size if p.is_file() else 0,
                        }
                    )
                    if len(entries) >= max_entries:
                        break
            else:
                for p in sorted(path.iterdir()):
                    entries.append(
                        {
                            "name": p.name,
                            "type": "dir" if p.is_dir() else "file",
                            "size": p.stat().st_size if p.is_file() else 0,
                        }
                    )
                    if len(entries) >= max_entries:
                        break
            truncated = len(entries) >= max_entries
            return {
                "path": str(path),
                "entries": entries,
                "count": len(entries),
                "truncated": truncated,
            }
        except Exception as e:
            return {"error": f"Failed to list directory: {e}"}

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

    # ── Git tools (Nightwatch system) ──

    if name == "git_status":
        repo_path = args.get("path") or workspace or None
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain", "-b"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path,
            )
            return {"status": proc.stdout.strip(), "exit_code": proc.returncode}
        except Exception as e:
            return {"error": f"git status failed: {e}"}

    if name == "git_diff":
        repo_path = args.get("path") or workspace or None
        staged = args.get("staged", False)
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--cached")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path,
            )
            return {"diff": proc.stdout[:20000], "exit_code": proc.returncode}
        except Exception as e:
            return {"error": f"git diff failed: {e}"}

    if name == "git_branch":
        repo_path = args.get("path") or workspace or None
        branch_name = args.get("branch_name", "")
        if not branch_name:
            return {"error": "branch_name is required"}
        if branch_name in PROTECTED_BRANCHES:
            return {"error": f"Cannot create/switch to protected branch: {branch_name}"}
        try:
            proc = subprocess.run(
                ["git", "checkout", "-b", branch_name],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path,
            )
            if proc.returncode != 0:
                return {"error": f"git checkout -b failed: {proc.stderr.strip()}"}
            return {"branch": branch_name, "created": True}
        except Exception as e:
            return {"error": f"git branch failed: {e}"}

    if name == "git_commit":
        repo_path = args.get("path") or workspace or None
        message = args.get("message", "")
        files = args.get("files", [])
        if not message:
            return {"error": "commit message is required"}

        # Check current branch — reject commits on protected branches
        try:
            branch_proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=repo_path,
            )
            current_branch = branch_proc.stdout.strip()
            if current_branch in PROTECTED_BRANCHES:
                return {"error": f"Cannot commit on protected branch: {current_branch}"}
        except Exception:
            pass  # proceed — branch check is best-effort

        try:
            # Stage files
            if files:
                stage_proc = subprocess.run(
                    ["git", "add"] + files,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=repo_path,
                )
            else:
                stage_proc = subprocess.run(
                    ["git", "add", "-A"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=repo_path,
                )
            if stage_proc.returncode != 0:
                return {"error": f"git add failed: {stage_proc.stderr.strip()}"}

            # Commit
            commit_proc = subprocess.run(
                ["git", "commit", "-m", message],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=repo_path,
            )
            if commit_proc.returncode != 0:
                return {"error": f"git commit failed: {commit_proc.stderr.strip()}"}

            # Get commit hash
            hash_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=repo_path,
            )
            return {
                "committed": True,
                "message": message,
                "sha": hash_proc.stdout.strip()[:12],
                "output": commit_proc.stdout.strip()[:1000],
            }
        except Exception as e:
            return {"error": f"git commit failed: {e}"}

    if name == "git_push":
        repo_path = args.get("path") or workspace or None
        set_upstream = args.get("set_upstream", True)

        # Check current branch — reject push on protected branches
        try:
            branch_proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=repo_path,
            )
            current_branch = branch_proc.stdout.strip()
            if current_branch in PROTECTED_BRANCHES:
                return {"error": f"Cannot push to protected branch: {current_branch}"}
        except Exception as e:
            return {"error": f"Failed to determine current branch: {e}"}

        try:
            cmd = ["git", "push"]
            if set_upstream:
                cmd.extend(["-u", "origin", current_branch])
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path,
            )
            if proc.returncode != 0:
                return {"error": f"git push failed: {proc.stderr.strip()}"}
            return {"pushed": True, "branch": current_branch, "output": proc.stdout.strip()[:1000]}
        except Exception as e:
            return {"error": f"git push failed: {e}"}

    if name == "create_pull_request":
        repo_path = args.get("path") or workspace or None
        title = args.get("title", "")
        body = args.get("body", "")
        base = args.get("base", "main")
        labels = args.get("labels", [])
        if not title:
            return {"error": "PR title is required"}

        # Always add 'nightwatch' label
        all_labels = list(set(["nightwatch"] + labels))
        label_arg = ",".join(all_labels)

        try:
            cmd = [
                "gh",
                "pr",
                "create",
                "--draft",
                "--title",
                title,
                "--body",
                body,
                "--base",
                base,
                "--label",
                label_arg,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_path,
            )
            if proc.returncode != 0:
                return {"error": f"gh pr create failed: {proc.stderr.strip()}"}
            pr_url = proc.stdout.strip()
            return {"created": True, "url": pr_url, "title": title, "draft": True}
        except Exception as e:
            return {"error": f"create_pull_request failed: {e}"}

    # ── Deep reasoning (RLM) ──

    if name == "deep_reason":
        from robothor.engine.rlm_tool import DeepReasonConfig, execute_deep_reason

        config = DeepReasonConfig(workspace=workspace)
        return execute_deep_reason(
            query=args.get("query", ""),
            context=args.get("context", ""),
            context_sources=args.get("context_sources"),
            config=config,
        )

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

    return {"error": f"Unknown tool: {name}"}


# ─── Sub-agent spawn handlers ────────────────────────────────────────


async def _handle_spawn_agent(
    args: dict[str, Any],
    *,
    agent_id: str = "",
) -> dict[str, Any]:
    """Spawn a single child agent and wait for its result."""
    from robothor.engine.config import load_agent_config
    from robothor.engine.models import DeliveryMode, TriggerType

    runner = get_runner()
    if runner is None:
        return {"error": "Runner not available — spawn_agent requires a running engine"}

    spawn_ctx = _current_spawn_context.get()
    if spawn_ctx is None:
        return {"error": "No spawn context — spawn_agent can only be called during an agent run"}

    child_agent_id = args.get("agent_id", "")
    message = args.get("message", "")
    if not child_agent_id or not message:
        return {"error": "agent_id and message are required"}

    # Depth check
    child_depth = spawn_ctx.nesting_depth + 1
    if child_depth > spawn_ctx.max_nesting_depth:
        return {
            "error": (
                f"Max nesting depth exceeded: depth {child_depth} > limit {spawn_ctx.max_nesting_depth}. "
                "Handle this task directly instead of spawning."
            )
        }

    # Load child agent config
    child_config = load_agent_config(child_agent_id, runner.config.manifest_dir)
    if child_config is None:
        return {"error": f"Agent config not found: {child_agent_id}"}

    # Apply tools_override if provided
    tools_override = args.get("tools_override")
    if tools_override and isinstance(tools_override, list):
        child_config.tools_allowed = tools_override

    # Apply max_iterations override (never increase beyond parent's sub_agent_max_iterations)
    child_max_iters = child_config.max_iterations
    # Get the parent agent's sub_agent_max_iterations from the spawn context indirectly
    # by reading from the args (caller's requested cap)
    requested_iters = args.get("max_iterations")
    if requested_iters is not None:
        child_max_iters = min(child_max_iters, int(requested_iters))
    # Also respect the general sub_agent cap — we load it via the parent's config
    # The spawn context doesn't carry this, but the parent set it when creating context
    # For safety, cap at a reasonable maximum
    child_max_iters = min(child_max_iters, 30)
    child_config.max_iterations = child_max_iters

    # Apply timeout override
    requested_timeout = args.get("timeout_seconds")
    if requested_timeout is not None:
        child_config.timeout_seconds = min(child_config.timeout_seconds, int(requested_timeout))

    # Force delivery to NONE — sub-agents never message Philip
    child_config.delivery_mode = DeliveryMode.NONE

    # Disable spawning on child unless explicitly configured
    # (prevents runaway recursion even if manifest has can_spawn_agents)
    if child_depth >= spawn_ctx.max_nesting_depth:
        child_config.can_spawn_agents = False

    # Build child SpawnContext
    child_spawn_ctx = SpawnContext(
        parent_run_id=spawn_ctx.parent_run_id,
        parent_agent_id=agent_id,
        correlation_id=spawn_ctx.correlation_id,
        nesting_depth=child_depth,
        max_nesting_depth=spawn_ctx.max_nesting_depth,
        remaining_token_budget=spawn_ctx.remaining_token_budget,
        remaining_cost_budget_usd=spawn_ctx.remaining_cost_budget_usd,
        parent_trace_id=spawn_ctx.parent_trace_id,
        parent_span_id=spawn_ctx.parent_span_id,
    )

    # Namespaced dedup key to prevent duplicate spawns from the same parent
    dedup_key = f"sub:{spawn_ctx.parent_run_id}:{child_agent_id}"
    from robothor.engine.dedup import release, try_acquire

    if not try_acquire(dedup_key):
        return {"error": f"Agent {child_agent_id} is already running as a sub-agent of this run"}

    start_time = time.monotonic()
    try:
        sem = _get_spawn_semaphore()
        async with sem:
            run = await runner.execute(
                agent_id=child_agent_id,
                message=message,
                trigger_type=TriggerType.SUB_AGENT,
                trigger_detail=f"spawned_by:{agent_id}",
                correlation_id=spawn_ctx.correlation_id,
                agent_config=child_config,
                spawn_context=child_spawn_ctx,
            )
    finally:
        release(dedup_key)

    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    # Deduct child's usage from parent's remaining budget
    if spawn_ctx.remaining_token_budget > 0:
        spawn_ctx.remaining_token_budget = max(
            0, spawn_ctx.remaining_token_budget - run.input_tokens - run.output_tokens
        )
    if spawn_ctx.remaining_cost_budget_usd > 0:
        spawn_ctx.remaining_cost_budget_usd = max(
            0.0, spawn_ctx.remaining_cost_budget_usd - run.total_cost_usd
        )

    result: dict[str, Any] = {
        "agent_id": child_agent_id,
        "run_id": run.id,
        "status": run.status.value,
        "output_text": run.output_text or "",
        "duration_ms": elapsed_ms,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_cost_usd": run.total_cost_usd,
        "steps": len(run.steps),
    }
    if run.error_message:
        result["error"] = run.error_message

    return result


async def _handle_spawn_agents(
    args: dict[str, Any],
    *,
    agent_id: str = "",
) -> dict[str, Any]:
    """Spawn multiple agents in parallel and wait for all results."""
    agents_list = args.get("agents", [])
    if not agents_list:
        return {"error": "agents list is required and must not be empty"}

    if len(agents_list) > 5:
        return {"error": f"Max 5 parallel sub-agents allowed, got {len(agents_list)}"}

    # Create coroutines for each sub-agent
    coros = []
    for spec in agents_list:
        spawn_args = {
            "agent_id": spec.get("agent_id", ""),
            "message": spec.get("message", ""),
        }
        if "tools_override" in spec:
            spawn_args["tools_override"] = spec["tools_override"]
        coros.append(_handle_spawn_agent(spawn_args, agent_id=agent_id))

    # Run all in parallel — one failure doesn't cancel others
    raw_results = await asyncio.gather(*coros, return_exceptions=True)

    results = []
    completed = 0
    failed = 0

    for i, r in enumerate(raw_results):
        if isinstance(r, BaseException):
            failed += 1
            results.append(
                {
                    "agent_id": agents_list[i].get("agent_id", "unknown"),
                    "status": "failed",
                    "error": str(r),
                }
            )
        elif isinstance(r, dict) and r.get("error"):
            failed += 1
            results.append(r)
        else:
            completed += 1
            if isinstance(r, dict):
                results.append(r)
            else:
                results.append({"status": "completed", "result": str(r)})

    return {
        "results": results,
        "total": len(agents_list),
        "completed": completed,
        "failed": failed,
    }


# ─── PDF Analysis ────────────────────────────────────────────────────


async def _handle_analyze_pdf(
    args: dict[str, Any],
    workspace: str = "",
) -> dict[str, Any]:
    """Analyze a PDF: fast-path text extraction, slow-path vision AI."""
    from pathlib import Path

    path_str = args.get("path", "")
    query = args.get("query")
    pages_spec = args.get("pages")

    if not path_str:
        return {"error": "No path provided"}

    max_pdf_size = 50 * 1024 * 1024  # 50MB

    # Resolve path (workspace-relative or absolute)
    path = Path(path_str)
    if not path.is_absolute() and workspace:
        path = Path(workspace) / path

    # Path traversal protection
    resolved = path.resolve()
    if workspace:
        workspace_resolved = Path(workspace).resolve()
        if not resolved.is_relative_to(workspace_resolved):
            return {"error": "Path must be within workspace"}

    if not resolved.exists():
        return {"error": f"File not found: {path}"}
    if not str(resolved).lower().endswith(".pdf"):
        return {"error": "File is not a PDF"}
    if resolved.stat().st_size > max_pdf_size:
        return {"error": f"PDF exceeds {max_pdf_size // (1024 * 1024)}MB limit"}

    try:
        import io

        import pypdf

        raw_bytes = resolved.read_bytes()
        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        total_pages = len(reader.pages)

        # Parse page range
        page_indices = _parse_page_range(pages_spec, total_pages, max_pages=10)

        # Fast path: text extraction
        text_pages = []
        for i in page_indices:
            text = reader.pages[i].extract_text() or ""
            if text.strip():
                text_pages.append(f"[Page {i + 1}]\n{text}")

        if text_pages and not query:
            # Text found and no query — return extracted text
            return {
                "pages_analyzed": len(page_indices),
                "page_count": total_pages,
                "text_content": "\n\n".join(text_pages)[:8000],
            }

        if text_pages and query:
            # Text found with query — use LLM to answer
            text_content = "\n\n".join(text_pages)[:6000]
            try:
                import litellm

                response = await litellm.acompletion(
                    model="gemini/gemini-2.5-flash",
                    messages=[
                        {
                            "role": "system",
                            "content": "Answer the question based on the PDF content provided.",
                        },
                        {
                            "role": "user",
                            "content": f"PDF Content:\n{text_content}\n\nQuestion: {query}",
                        },
                    ],
                    temperature=0.1,
                    max_tokens=2000,
                )
                ai_answer = response.choices[0].message.content
                return {
                    "pages_analyzed": len(page_indices),
                    "page_count": total_pages,
                    "text_content": text_content[:4000],
                    "ai_analysis": ai_answer,
                }
            except Exception as e:
                return {
                    "pages_analyzed": len(page_indices),
                    "page_count": total_pages,
                    "text_content": text_content[:8000],
                    "ai_analysis_error": str(e),
                }

        # Slow path: image-based PDF — convert pages to images and use vision
        try:
            import base64

            images_b64 = []
            for i in page_indices[:5]:  # Max 5 pages for vision (cost control)
                page = reader.pages[i]
                # Try to extract images from the page
                if "/XObject" in (page.get("/Resources") or {}):
                    xobjects = page["/Resources"]["/XObject"].get_object()
                    for obj_name in xobjects:
                        xobj = xobjects[obj_name].get_object()
                        if xobj["/Subtype"] == "/Image":
                            data = xobj.get_data()
                            if len(data) > 100:
                                images_b64.append(base64.b64encode(data).decode())
                                break  # One image per page

            if not images_b64:
                return {
                    "pages_analyzed": len(page_indices),
                    "page_count": total_pages,
                    "text_content": "[No extractable text or images found in this PDF]",
                }

            # Send images to vision model
            import litellm

            content = []
            for idx, img_b64 in enumerate(images_b64[:3]):  # Max 3 images
                content.append({"type": "text", "text": f"Page {page_indices[idx] + 1}:"})
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    }
                )

            prompt = query or "Extract and describe all text and content from these PDF pages."
            content.append({"type": "text", "text": prompt})

            response = await litellm.acompletion(
                model="gemini/gemini-2.5-flash",
                messages=[{"role": "user", "content": content}],
                temperature=0.1,
                max_tokens=4000,
            )
            return {
                "pages_analyzed": len(page_indices),
                "page_count": total_pages,
                "ai_analysis": response.choices[0].message.content,
            }

        except ImportError:
            return {
                "pages_analyzed": 0,
                "page_count": total_pages,
                "text_content": "[Image-based PDF — install Pillow for vision analysis]",
            }
        except Exception as e:
            return {
                "pages_analyzed": 0,
                "page_count": total_pages,
                "error": f"Vision analysis failed: {e}",
            }

    except ImportError:
        return {"error": "pypdf not installed — install with: pip install pypdf"}
    except Exception as e:
        return {"error": f"PDF analysis failed: {e}"}


def _parse_page_range(spec: str | None, total: int, max_pages: int = 10) -> list[int]:
    """Parse a page range specification into a list of 0-based indices."""
    if not spec:
        return list(range(min(total, max_pages)))

    indices = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            start_i = max(0, int(start) - 1)
            end_i = min(total, int(end))
            indices.update(range(start_i, end_i))
        else:
            i = int(part) - 1
            if 0 <= i < total:
                indices.add(i)

    return sorted(indices)[:max_pages]
