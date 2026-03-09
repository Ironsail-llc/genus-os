"""Engine-specific tool schemas (not in MCP)."""

from __future__ import annotations

from typing import Any


def get_engine_schemas() -> dict[str, dict[str, Any]]:
    """Return all engine-specific tool schemas keyed by tool name."""
    schemas: dict[str, dict[str, Any]] = {}

    schemas["exec"] = {
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
    schemas["read_file"] = {
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
    schemas["list_directory"] = {
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
    schemas["write_file"] = {
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
    schemas["web_fetch"] = {
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
    schemas["web_search"] = {
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
    schemas["analyze_pdf"] = {
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
    schemas["make_call"] = {
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
    schemas["list_agent_runs"] = {
        "type": "function",
        "function": {
            "name": "list_agent_runs",
            "description": "List recent agent runs with optional filters. Returns run ID, agent, status, duration, model, timestamps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Filter by agent ID"},
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
    schemas["get_agent_run"] = {
        "type": "function",
        "function": {
            "name": "get_agent_run",
            "description": "Get details of a specific agent run including its step-by-step audit trail.",
            "parameters": {
                "type": "object",
                "properties": {"run_id": {"type": "string", "description": "The run UUID"}},
                "required": ["run_id"],
            },
        },
    }
    schemas["list_agent_schedules"] = {
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
    schemas["get_agent_stats"] = {
        "type": "function",
        "function": {
            "name": "get_agent_stats",
            "description": "Get aggregated stats for an agent: total runs, failures, timeouts, avg duration, token usage, cost over the last N hours.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Agent ID to get stats for"},
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
    schemas["vault_get"] = {
        "type": "function",
        "function": {
            "name": "vault_get",
            "description": "Retrieve a decrypted secret from the vault by key.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Secret key"}},
                "required": ["key"],
            },
        },
    }
    schemas["vault_set"] = {
        "type": "function",
        "function": {
            "name": "vault_set",
            "description": "Store an encrypted secret in the vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Secret key"},
                    "value": {"type": "string", "description": "Secret value to encrypt and store"},
                    "category": {
                        "type": "string",
                        "description": "Category: credential, oauth_token, api_key, certificate",
                        "default": "credential",
                    },
                },
                "required": ["key", "value"],
            },
        },
    }
    schemas["vault_list"] = {
        "type": "function",
        "function": {
            "name": "vault_list",
            "description": "List secret keys in the vault (not values). Optionally filter by category.",
            "parameters": {
                "type": "object",
                "properties": {"category": {"type": "string", "description": "Filter by category"}},
            },
        },
    }
    schemas["vault_delete"] = {
        "type": "function",
        "function": {
            "name": "vault_delete",
            "description": "Delete a secret from the vault.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Secret key to delete"}},
                "required": ["key"],
            },
        },
    }

    # ── Convenience aliases ──
    schemas["list_my_tasks"] = {
        "type": "function",
        "function": {
            "name": "list_my_tasks",
            "description": "List tasks assigned to you (the current agent).",
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
                    "excludeResolved": {
                        "type": "boolean",
                        "description": "Exclude DONE tasks (default true)",
                        "default": True,
                    },
                },
            },
        },
    }

    # ── CRM Merge tools ──
    for merge_name, merge_desc, obj_type in [
        ("merge_people", "Merge two duplicate people.", "person"),
        ("merge_contacts", "Merge two duplicate contacts (alias for merge_people).", "contact"),
        ("merge_companies", "Merge two duplicate companies.", "company"),
    ]:
        schemas[merge_name] = {
            "type": "function",
            "function": {
                "name": merge_name,
                "description": f"{merge_desc} Keeper is preserved, loser is soft-deleted.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keeperId": {
                            "type": "string",
                            "description": f"UUID of the {obj_type} to keep",
                        },
                        "loserId": {
                            "type": "string",
                            "description": f"UUID of the {obj_type} to merge into keeper and delete",
                        },
                    },
                    "required": ["keeperId", "loserId"],
                },
            },
        }

    # ── Deep reasoning (RLM) ──
    schemas["deep_reason"] = {
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
                    "query": {"type": "string", "description": "The reasoning question to answer"},
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

    # ── Git tools ──
    schemas["git_status"] = {
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
                    }
                },
            },
        },
    }
    schemas["git_diff"] = {
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
    schemas["git_branch"] = {
        "type": "function",
        "function": {
            "name": "git_branch",
            "description": "Create and switch to a new branch. Cannot target main/master.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the branch to create",
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
    schemas["git_commit"] = {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage specified files (or all changes) and commit with a message. Cannot commit on main/master.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message"},
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
    schemas["git_push"] = {
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
                        "description": "Set upstream tracking (default true)",
                        "default": True,
                    },
                },
            },
        },
    }
    schemas["create_pull_request"] = {
        "type": "function",
        "function": {
            "name": "create_pull_request",
            "description": "Create a draft pull request on GitHub using gh CLI. Always creates as draft, auto-labels 'nightwatch'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "PR title (keep under 70 chars)"},
                    "body": {"type": "string", "description": "PR body in markdown"},
                    "base": {
                        "type": "string",
                        "description": "Base branch (default 'main')",
                        "default": "main",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional labels",
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

    # ── Google Workspace tools ──
    schemas["gws_gmail_search"] = {
        "type": "function",
        "function": {
            "name": "gws_gmail_search",
            "description": "Search Gmail messages. Returns message IDs and thread IDs matching the query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum messages to return (default 10, max 100)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    }
    schemas["gws_gmail_get"] = {
        "type": "function",
        "function": {
            "name": "gws_gmail_get",
            "description": "Get a Gmail message or thread by ID. Returns headers, snippet, labels, and body.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Gmail message ID"},
                    "thread_id": {
                        "type": "string",
                        "description": "Gmail thread ID (returns all messages in thread)",
                    },
                    "format": {
                        "type": "string",
                        "description": "Response format: 'full', 'metadata', 'minimal'",
                        "default": "full",
                        "enum": ["full", "metadata", "minimal"],
                    },
                },
            },
        },
    }
    schemas["gws_gmail_send"] = {
        "type": "function",
        "function": {
            "name": "gws_gmail_send",
            "description": "Send an email or reply to a thread. Composes and sends via Gmail API.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient email address(es), comma-separated",
                    },
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {"type": "string", "description": "Email body (plain text)"},
                    "cc": {"type": "string", "description": "CC recipients, comma-separated"},
                    "thread_id": {"type": "string", "description": "Thread ID to reply to"},
                    "in_reply_to": {
                        "type": "string",
                        "description": "Message-ID header of the message being replied to",
                    },
                },
                "required": ["to", "subject", "body"],
            },
        },
    }
    schemas["gws_gmail_modify"] = {
        "type": "function",
        "function": {
            "name": "gws_gmail_modify",
            "description": "Modify Gmail message labels (mark read/unread, archive, add/remove labels).",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Gmail message ID to modify"},
                    "add_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Label IDs to add",
                    },
                    "remove_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Label IDs to remove",
                    },
                },
                "required": ["message_id"],
            },
        },
    }
    schemas["gws_calendar_list"] = {
        "type": "function",
        "function": {
            "name": "gws_calendar_list",
            "description": "List calendar events in a date range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "time_min": {
                        "type": "string",
                        "description": "Start of range in RFC3339 format",
                    },
                    "time_max": {"type": "string", "description": "End of range in RFC3339 format"},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum events to return (default 20)",
                        "default": 20,
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default 'primary')",
                        "default": "primary",
                    },
                },
                "required": ["time_min"],
            },
        },
    }
    schemas["gws_calendar_create"] = {
        "type": "function",
        "function": {
            "name": "gws_calendar_create",
            "description": "Create a calendar event with title, time, attendees, and optional location/description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Event title"},
                    "start": {"type": "string", "description": "Start time in RFC3339 format"},
                    "end": {"type": "string", "description": "End time in RFC3339 format"},
                    "description": {"type": "string", "description": "Event description/notes"},
                    "location": {"type": "string", "description": "Event location"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of attendee email addresses",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default 'primary')",
                        "default": "primary",
                    },
                },
                "required": ["summary", "start", "end"],
            },
        },
    }
    schemas["gws_calendar_delete"] = {
        "type": "function",
        "function": {
            "name": "gws_calendar_delete",
            "description": "Delete a calendar event by its event ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "Calendar event ID to delete"},
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default 'primary')",
                        "default": "primary",
                    },
                },
                "required": ["event_id"],
            },
        },
    }
    schemas["gws_chat_send"] = {
        "type": "function",
        "function": {
            "name": "gws_chat_send",
            "description": "Send a message to a Google Chat space.",
            "parameters": {
                "type": "object",
                "properties": {
                    "space": {"type": "string", "description": "Space resource name"},
                    "text": {"type": "string", "description": "Message text to send"},
                },
                "required": ["space", "text"],
            },
        },
    }
    schemas["gws_chat_list_spaces"] = {
        "type": "function",
        "function": {
            "name": "gws_chat_list_spaces",
            "description": "List Google Chat spaces the authenticated user is a member of.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_size": {
                        "type": "integer",
                        "description": "Max spaces to return (default 50)",
                        "default": 50,
                    },
                },
            },
        },
    }
    schemas["gws_chat_list_messages"] = {
        "type": "function",
        "function": {
            "name": "gws_chat_list_messages",
            "description": "List messages in a Google Chat space. Use for reading conversation thread context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "space": {
                        "type": "string",
                        "description": "Space resource name (e.g. 'spaces/AAAA...')",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Max messages to return (default 25, max 100)",
                        "default": 25,
                    },
                },
                "required": ["space"],
            },
        },
    }

    # ── Sub-agent spawning tools ──
    schemas["spawn_agent"] = {
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
                    "agent_id": {"type": "string", "description": "ID of the agent to spawn"},
                    "message": {
                        "type": "string",
                        "description": "Task message / prompt for the child agent",
                    },
                    "tools_override": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: replace child's tools_allowed",
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
    schemas["spawn_agents"] = {
        "type": "function",
        "function": {
            "name": "spawn_agents",
            "description": (
                "Spawn multiple agents in parallel and wait for all results. "
                "Max 5 parallel sub-agents. Each runs independently."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent_id": {"type": "string", "description": "Agent ID to spawn"},
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

    # ── Federation tools ──
    schemas["federation_query"] = {
        "type": "function",
        "function": {
            "name": "federation_query",
            "description": "Query a connected Robothor instance's data (health, agent runs, memory).",
            "parameters": {
                "type": "object",
                "properties": {
                    "connection_id": {
                        "type": "string",
                        "description": "Federation connection ID",
                    },
                    "query_type": {
                        "type": "string",
                        "description": "What to query: 'health', 'runs'",
                        "enum": ["health", "runs"],
                        "default": "health",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Filter by agent ID (for runs query)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["connection_id"],
            },
        },
    }
    schemas["federation_trigger"] = {
        "type": "function",
        "function": {
            "name": "federation_trigger",
            "description": "Trigger an agent run on a connected Robothor instance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "connection_id": {
                        "type": "string",
                        "description": "Federation connection ID",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Agent ID to trigger on the remote instance",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message/prompt for the agent run",
                    },
                },
                "required": ["connection_id", "agent_id"],
            },
        },
    }
    schemas["federation_sync_status"] = {
        "type": "function",
        "function": {
            "name": "federation_sync_status",
            "description": "Check sync watermarks and pending event counts for federation connections.",
            "parameters": {
                "type": "object",
                "properties": {
                    "connection_id": {
                        "type": "string",
                        "description": "Connection ID (omit for all connections)",
                    },
                },
            },
        },
    }

    return schemas
