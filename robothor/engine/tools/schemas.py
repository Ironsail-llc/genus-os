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
            "description": "Make an outbound phone call via the voice server. The call connects to Gemini Live for real-time AI conversation.",
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
                        "description": "Why the AI is calling (used in the system prompt)",
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

    schemas["list_tasks_summary"] = {
        "type": "function",
        "function": {
            "name": "list_tasks_summary",
            "description": "Fleet dashboard: task counts by status, requires_human count, by-agent breakdown, SLA overdue, failed auto-tasks.",
            "parameters": {"type": "object", "properties": {}},
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
    schemas["gws_gmail_reply"] = {
        "type": "function",
        "function": {
            "name": "gws_gmail_reply",
            "description": (
                "Reply to an existing email thread. Automatically threads correctly "
                "(sets In-Reply-To/References headers and threadId), includes all "
                "original recipients (reply-all), and prevents duplicate replies. "
                "Use this instead of gws_gmail_send for all replies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Gmail thread ID (from task body) — REQUIRED to keep reply in the existing conversation",
                    },
                    "body": {
                        "type": "string",
                        "description": "Reply body (plain text)",
                    },
                    "cc": {
                        "type": "string",
                        "description": "Additional CC recipients beyond those already in the thread, comma-separated",
                    },
                },
                "required": ["thread_id", "body"],
            },
        },
    }
    schemas["gws_gmail_send"] = {
        "type": "function",
        "function": {
            "name": "gws_gmail_send",
            "description": (
                "Send a NEW email (not a reply). For replies to existing threads, "
                "use gws_gmail_reply instead — it handles threading automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient email address(es), comma-separated",
                    },
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {
                        "type": "string",
                        "description": "Email body text (plain or HTML depending on content_type)",
                    },
                    "content_type": {
                        "type": "string",
                        "description": "Content type: 'text' (default) or 'html' for HTML emails",
                    },
                    "cc": {"type": "string", "description": "CC recipients, comma-separated"},
                    "thread_id": {
                        "type": "string",
                        "description": "Thread ID to reply to (deprecated — use gws_gmail_reply for proper threading)",
                    },
                    "in_reply_to": {
                        "type": "string",
                        "description": "Message-ID header (deprecated — use gws_gmail_reply for proper threading)",
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
                    "with_meet": {
                        "type": "boolean",
                        "description": "Add a Google Meet video conference link (default true)",
                        "default": True,
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Bypass the duplicate-meeting check. By default, a pre-insert search of ±14 days for an event with the same title and overlapping attendees will short-circuit creation; set force=true only when you have verified the existing event is not the one you want to create.",
                        "default": False,
                    },
                    "attendee_confirmed": {
                        "type": "boolean",
                        "description": "Certify that the attendees have already confirmed this time out-of-band (e.g. the operator approved in chat, or an attendee replied 'yes' on an email thread). Bypasses the recurring_meeting_proposal_required guardrail for high-stakes invites. Never set this to true speculatively — only when you can point to the confirmation.",
                        "default": False,
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
                    "parent_task_id": {
                        "type": "string",
                        "description": (
                            "Optional: CRM task UUID this spawn is advancing. "
                            "When set, the child receives the parent's objective "
                            "and next_action in its prompt and is bound to them — "
                            "use this any time you're spawning a worker to drive "
                            "a specific task forward."
                        ),
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
                                "parent_task_id": {
                                    "type": "string",
                                    "description": (
                                        "Optional CRM task UUID — child "
                                        "receives parent objective + next_action"
                                    ),
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

    # ── Princess Freya (PF) vessel tools ──
    schemas["pf_system_status"] = {
        "type": "function",
        "function": {
            "name": "pf_system_status",
            "description": (
                "Get Princess Freya system status: battery voltage, disk/memory usage, "
                "CPU temperature, connectivity (Tailscale, internet, parent), GPS lock, "
                "bilge pump, and uptime."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }

    # ── Federation tools ──
    schemas["federation_query"] = {
        "type": "function",
        "function": {
            "name": "federation_query",
            "description": "Query a connected Genus OS instance's data (health, agent runs, memory).",
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
            "description": "Trigger an agent run on a connected Genus OS instance.",
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

    # ── Browser Automation Tool ────────────────────────────────────────

    schemas["browser"] = {
        "type": "function",
        "function": {
            "name": "browser",
            "description": (
                "Full browser automation via Playwright. Manages a persistent Chromium session. "
                "Actions: start (launch browser), stop (close), navigate (go to URL), "
                "screenshot (capture page), snapshot (ARIA accessibility tree with element refs), "
                "act (interact: click/fill/type/press/scroll/select using refs or selectors), "
                "tabs (list open tabs), pdf (export page), evaluate (run JavaScript), "
                "console (read console), status (check session)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "start",
                            "stop",
                            "status",
                            "navigate",
                            "screenshot",
                            "snapshot",
                            "act",
                            "tabs",
                            "pdf",
                            "console",
                            "evaluate",
                        ],
                        "description": "Browser action to perform",
                    },
                    "targetUrl": {
                        "type": "string",
                        "description": "URL for navigate action",
                    },
                    "url": {
                        "type": "string",
                        "description": "URL for navigate action (alias for targetUrl)",
                    },
                    "fullPage": {
                        "type": "boolean",
                        "description": "Capture full page screenshot (default: false)",
                    },
                    "js": {
                        "type": "string",
                        "description": "JavaScript expression for evaluate action",
                    },
                    "request": {
                        "type": "object",
                        "description": (
                            "Interaction request for act action. "
                            "Fields: kind (click/fill/type/press/scroll/select), "
                            "ref (element ref from snapshot), selector (CSS selector), "
                            "value/text/key/fields/x/y as needed."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    }

    # ── Desktop Control Tools ──────────────────────────────────────────

    schemas["desktop_screenshot"] = {
        "type": "function",
        "function": {
            "name": "desktop_screenshot",
            "description": "Capture the virtual desktop display and return a base64-encoded PNG screenshot with dimensions.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }
    schemas["desktop_click"] = {
        "type": "function",
        "function": {
            "name": "desktop_click",
            "description": "Left click at (x, y) pixel coordinates on the virtual desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (pixels from left)"},
                    "y": {"type": "integer", "description": "Y coordinate (pixels from top)"},
                },
                "required": ["x", "y"],
            },
        },
    }
    schemas["desktop_double_click"] = {
        "type": "function",
        "function": {
            "name": "desktop_double_click",
            "description": "Double click at (x, y) pixel coordinates on the virtual desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate"},
                    "y": {"type": "integer", "description": "Y coordinate"},
                },
                "required": ["x", "y"],
            },
        },
    }
    schemas["desktop_right_click"] = {
        "type": "function",
        "function": {
            "name": "desktop_right_click",
            "description": "Right click at (x, y) pixel coordinates on the virtual desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate"},
                    "y": {"type": "integer", "description": "Y coordinate"},
                },
                "required": ["x", "y"],
            },
        },
    }
    schemas["desktop_mouse_move"] = {
        "type": "function",
        "function": {
            "name": "desktop_mouse_move",
            "description": "Move the mouse cursor to (x, y) without clicking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate"},
                    "y": {"type": "integer", "description": "Y coordinate"},
                },
                "required": ["x", "y"],
            },
        },
    }
    schemas["desktop_drag"] = {
        "type": "function",
        "function": {
            "name": "desktop_drag",
            "description": "Click and drag from (start_x, start_y) to (end_x, end_y).",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer", "description": "Start X coordinate"},
                    "start_y": {"type": "integer", "description": "Start Y coordinate"},
                    "end_x": {"type": "integer", "description": "End X coordinate"},
                    "end_y": {"type": "integer", "description": "End Y coordinate"},
                },
                "required": ["start_x", "start_y", "end_x", "end_y"],
            },
        },
    }
    schemas["desktop_scroll"] = {
        "type": "function",
        "function": {
            "name": "desktop_scroll",
            "description": "Scroll up or down at the current mouse position.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "Scroll direction",
                    },
                    "clicks": {
                        "type": "integer",
                        "description": "Number of scroll steps (default: 3, max: 20)",
                    },
                },
                "required": ["direction"],
            },
        },
    }
    schemas["desktop_type"] = {
        "type": "function",
        "function": {
            "name": "desktop_type",
            "description": "Type a text string at the current cursor position on the virtual desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type"},
                },
                "required": ["text"],
            },
        },
    }
    schemas["desktop_key"] = {
        "type": "function",
        "function": {
            "name": "desktop_key",
            "description": "Press a key combination (e.g. 'ctrl+a', 'Return', 'alt+F4', 'Tab').",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key or key combination (xdotool syntax)",
                    },
                },
                "required": ["key"],
            },
        },
    }
    schemas["desktop_window_list"] = {
        "type": "function",
        "function": {
            "name": "desktop_window_list",
            "description": "List all open windows on the virtual desktop with IDs, titles, positions, and sizes.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }
    schemas["desktop_window_focus"] = {
        "type": "function",
        "function": {
            "name": "desktop_window_focus",
            "description": "Activate and focus a window by its ID (from desktop_window_list).",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_id": {
                        "type": "string",
                        "description": "Window ID (hex, from desktop_window_list)",
                    },
                },
                "required": ["window_id"],
            },
        },
    }
    schemas["desktop_launch"] = {
        "type": "function",
        "function": {
            "name": "desktop_launch",
            "description": "Launch an application on the virtual desktop. Returns the PID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "Application name or path (e.g. 'firefox', 'libreoffice')",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional command-line arguments",
                    },
                },
                "required": ["app"],
            },
        },
    }
    schemas["desktop_describe"] = {
        "type": "function",
        "function": {
            "name": "desktop_describe",
            "description": "Take a screenshot and describe the screen contents using a vision model (llama3.2-vision). Returns a natural language description of what is visible on screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Custom prompt for the vision model (optional — defaults to a comprehensive screen description)",
                    },
                },
            },
        },
    }

    # ── AutoResearch experiment tools ──────────────────────────────────

    schemas["experiment_create"] = {
        "type": "function",
        "function": {
            "name": "experiment_create",
            "description": (
                "Create and initialise an optimization experiment. "
                "Provide a config_file (YAML path) or inline parameters. "
                "The experiment iteratively optimises a single numeric metric."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "experiment_id": {
                        "type": "string",
                        "description": "Unique experiment identifier (kebab-case)",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["metric", "benchmark"],
                        "description": "Experiment mode: 'metric' (default) runs a shell command, 'benchmark' uses a benchmark suite",
                    },
                    "benchmark_agent_id": {
                        "type": "string",
                        "description": "Agent to benchmark (required when mode=benchmark)",
                    },
                    "benchmark_suite_id": {
                        "type": "string",
                        "description": "Benchmark suite to use (required when mode=benchmark)",
                    },
                    "config_file": {
                        "type": "string",
                        "description": "Path to experiment YAML config (optional — use inline params instead)",
                    },
                    "metric_name": {
                        "type": "string",
                        "description": "Human-readable metric name (e.g. 'email reply rate')",
                    },
                    "metric_command": {
                        "type": "string",
                        "description": "Shell command that outputs a single number (the metric value)",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["maximize", "minimize"],
                        "description": "Whether higher or lower is better",
                    },
                    "search_space": {
                        "type": "string",
                        "description": "Markdown describing what the agent is allowed to modify",
                    },
                    "max_iterations": {
                        "type": "integer",
                        "description": "Maximum iterations (default 20, hard cap 200)",
                    },
                    "min_improvement_pct": {
                        "type": "number",
                        "description": "Minimum % improvement to keep a variant (default 1.0)",
                    },
                    "measurement_samples": {
                        "type": "integer",
                        "description": "Number of measurements to average (default 1, max 10)",
                    },
                    "measurement_delay_seconds": {
                        "type": "integer",
                        "description": "Seconds to wait after change before measuring (default 0)",
                    },
                    "revert_command": {
                        "type": "string",
                        "description": "Shell command to revert the last change",
                    },
                    "guardrails": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Constraints the agent must respect",
                    },
                    "cost_budget_usd": {
                        "type": "number",
                        "description": "Maximum experiment cost in USD (default 2.0)",
                    },
                },
                "required": ["experiment_id"],
            },
        },
    }
    schemas["experiment_measure"] = {
        "type": "function",
        "function": {
            "name": "experiment_measure",
            "description": (
                "Run the experiment's metric command and return the measured value. "
                "Optionally average multiple samples for noisy metrics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "experiment_id": {
                        "type": "string",
                        "description": "Experiment identifier",
                    },
                    "samples": {
                        "type": "integer",
                        "description": "Number of samples to take and average (overrides config, max 10)",
                    },
                },
                "required": ["experiment_id"],
            },
        },
    }
    schemas["experiment_commit"] = {
        "type": "function",
        "function": {
            "name": "experiment_commit",
            "description": (
                "Record an iteration's outcome. If verdict is 'revert', the revert_command is executed. "
                "Learnings are stored for future iterations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "experiment_id": {
                        "type": "string",
                        "description": "Experiment identifier",
                    },
                    "hypothesis": {
                        "type": "string",
                        "description": "What you predicted this change would do and why",
                    },
                    "changes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                        "description": "List of files changed and what was modified",
                    },
                    "metric_before": {
                        "type": "number",
                        "description": "Metric value before this iteration's change",
                    },
                    "metric_after": {
                        "type": "number",
                        "description": "Metric value after this iteration's change",
                    },
                    "verdict": {
                        "type": "string",
                        "enum": ["keep", "revert"],
                        "description": "Whether to keep the change or revert it",
                    },
                    "learnings": {
                        "type": "string",
                        "description": "What was learned — explain WHY the change worked or didn't",
                    },
                    "cost_usd": {
                        "type": "number",
                        "description": "Cost of this iteration in USD (optional)",
                    },
                },
                "required": [
                    "experiment_id",
                    "hypothesis",
                    "changes",
                    "metric_before",
                    "metric_after",
                    "verdict",
                    "learnings",
                ],
            },
        },
    }
    schemas["experiment_status"] = {
        "type": "function",
        "function": {
            "name": "experiment_status",
            "description": (
                "Get the current state of an experiment: iterations, learnings, "
                "best value, cumulative improvement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "experiment_id": {
                        "type": "string",
                        "description": "Experiment identifier",
                    },
                    "include_iterations": {
                        "type": "boolean",
                        "description": "Include full iteration history (default false for compact view)",
                    },
                },
                "required": ["experiment_id"],
            },
        },
    }

    # ── AutoAgent benchmark tools ────────────────────────────────────

    schemas["benchmark_define"] = {
        "type": "function",
        "function": {
            "name": "benchmark_define",
            "description": (
                "Define or update a benchmark suite for evaluating an agent's harness. "
                "Provide a config_file (YAML) or inline task definitions. "
                "Each task has a prompt, expected behavior criteria, category, and weight."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent to benchmark",
                    },
                    "suite_id": {
                        "type": "string",
                        "description": "Unique suite identifier (kebab-case)",
                    },
                    "config_file": {
                        "type": "string",
                        "description": "Path to suite YAML config (optional — use inline params instead)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable suite description",
                    },
                    "max_cost_usd": {
                        "type": "number",
                        "description": "Maximum total cost for running the full suite (default 1.00, cap 5.00)",
                    },
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Task identifier"},
                                "prompt": {
                                    "type": "string",
                                    "description": "Message to send to the agent",
                                },
                                "category": {
                                    "type": "string",
                                    "enum": ["correctness", "safety", "efficiency", "tone"],
                                    "description": "Task category (default: correctness)",
                                },
                                "weight": {
                                    "type": "number",
                                    "description": "Scoring weight (default 1.0, safety tasks often 2.0)",
                                },
                                "expected": {
                                    "type": "object",
                                    "properties": {
                                        "must_contain": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "Regex patterns that must appear in output",
                                        },
                                        "must_not_contain": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "Regex patterns that must NOT appear in output",
                                        },
                                        "max_cost_usd": {
                                            "type": "number",
                                            "description": "Max cost for this single task run",
                                        },
                                        "max_iterations": {
                                            "type": "integer",
                                            "description": "Max agent iterations for this task",
                                        },
                                    },
                                    "description": "Expected behavior criteria for scoring",
                                },
                            },
                            "required": ["id", "prompt", "expected"],
                        },
                        "description": "List of benchmark tasks",
                    },
                },
                "required": ["agent_id", "suite_id"],
            },
        },
    }
    schemas["benchmark_run"] = {
        "type": "function",
        "function": {
            "name": "benchmark_run",
            "description": (
                "Execute a benchmark suite against an agent. Runs each task as a "
                "sub-agent invocation, scores output with deterministic pattern matching, "
                "and returns per-task scores, per-category breakdown, and weighted aggregate (0.0-1.0)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent to benchmark",
                    },
                    "suite_id": {
                        "type": "string",
                        "description": "Suite identifier",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Label for this run (e.g. 'baseline', 'iter-3'). Must be unique per suite.",
                    },
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional subset of task IDs to run (default: all tasks)",
                    },
                },
                "required": ["agent_id", "suite_id", "tag"],
            },
        },
    }
    schemas["benchmark_compare"] = {
        "type": "function",
        "function": {
            "name": "benchmark_compare",
            "description": (
                "Compare two benchmark runs. Returns per-task deltas, per-category deltas, "
                "aggregate delta, and flags any safety-category regressions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "suite_id": {
                        "type": "string",
                        "description": "Suite identifier",
                    },
                    "run_a": {
                        "type": "string",
                        "description": "Tag of the baseline run",
                    },
                    "run_b": {
                        "type": "string",
                        "description": "Tag of the comparison run",
                    },
                },
                "required": ["suite_id", "run_a", "run_b"],
            },
        },
    }

    # ── MCP Client ────────────────────────────────────────────────────
    schemas["mcp_list_servers"] = {
        "type": "function",
        "function": {
            "name": "mcp_list_servers",
            "description": "List configured external MCP servers and their connection status.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    schemas["mcp_list_tools"] = {
        "type": "function",
        "function": {
            "name": "mcp_list_tools",
            "description": "List tools available on a specific external MCP server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Name of the MCP server",
                    },
                },
                "required": ["server_name"],
            },
        },
    }
    schemas["mcp_call_tool"] = {
        "type": "function",
        "function": {
            "name": "mcp_call_tool",
            "description": "Call a tool on an external MCP server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Name of the MCP server",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Name of the tool to call",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments to pass to the tool",
                    },
                },
                "required": ["server_name", "tool_name"],
            },
        },
    }
    schemas["mcp_read_resource"] = {
        "type": "function",
        "function": {
            "name": "mcp_read_resource",
            "description": "Read a resource from an external MCP server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Name of the MCP server",
                    },
                    "uri": {
                        "type": "string",
                        "description": "URI of the resource to read",
                    },
                },
                "required": ["server_name", "uri"],
            },
        },
    }

    # ── Skills ────────────────────────────────────────────────────────
    schemas["invoke_skill"] = {
        "type": "function",
        "function": {
            "name": "invoke_skill",
            "description": (
                "Invoke a named skill to get step-by-step instructions. "
                "Skills are pre-built recipes for common multi-step operations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the skill to invoke (e.g. 'send-email', 'crm-lookup')",
                    },
                    "args": {
                        "type": "object",
                        "description": "Named arguments for the skill (see skill catalog for parameters)",
                        "additionalProperties": True,
                    },
                },
                "required": ["name"],
            },
        },
    }
    schemas["list_skills"] = {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List all available skills with their names and descriptions.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }
    schemas["create_skill"] = {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": (
                "Create a new reusable skill from a multi-step procedure you just performed. "
                "The skill becomes available to all agents via invoke_skill."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill identifier (kebab-case, 3-60 chars, e.g. 'deploy-staging')",
                    },
                    "description": {
                        "type": "string",
                        "description": "One-line description of what the skill does",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full markdown body with step-by-step instructions (max 10,000 chars)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Categorization tags (e.g. ['devops', 'deployment'])",
                    },
                    "parameters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string", "default": "string"},
                                "description": {"type": "string"},
                                "required": {"type": "boolean", "default": False},
                                "default": {},
                            },
                            "required": ["name"],
                        },
                        "description": "Typed parameters the skill accepts",
                    },
                    "tools_required": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tools this skill needs (e.g. ['exec', 'gws_gmail_send'])",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": ["text", "json"],
                        "description": "Expected output format (default: text)",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "If true, overwrite an existing skill with the same name",
                    },
                },
                "required": ["name", "description", "content"],
            },
        },
    }
    schemas["update_skill"] = {
        "type": "function",
        "function": {
            "name": "update_skill",
            "description": (
                "Update an existing skill with an improved version. "
                "The previous version is archived in the skill's revision history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the existing skill to update",
                    },
                    "content": {
                        "type": "string",
                        "description": "New markdown body with improved instructions (max 10,000 chars)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Updated one-line description (optional, keeps existing if omitted)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the skill was improved (recorded in revision history)",
                    },
                },
                "required": ["name", "content"],
            },
        },
    }

    # ── Timing ────────────────────────────────────────────────────────
    schemas["wait_seconds"] = {
        "type": "function",
        "function": {
            "name": "wait_seconds",
            "description": "Pause execution for N seconds (max 300). Useful for polling patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": "Seconds to wait (1-300)",
                    },
                },
                "required": ["seconds"],
            },
        },
    }

    # ── Apollo.io contact enrichment & search ──

    schemas["apollo_search_people"] = {
        "type": "function",
        "function": {
            "name": "apollo_search_people",
            "description": (
                "Search Apollo.io for people by name, company, title, or location. "
                "FREE — no credits consumed. Does NOT return email/phone; use "
                "apollo_enrich_person for that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "q_person_name": {
                        "type": "string",
                        "description": "Person name to search for",
                    },
                    "q_organization_name": {
                        "type": "string",
                        "description": "Company/organization name",
                    },
                    "person_titles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Job titles to filter by (e.g. ['CEO', 'CTO'])",
                    },
                    "person_locations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Locations to filter by (e.g. ['New York', 'California'])",
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Results per page (default 10, max 25)",
                    },
                },
            },
        },
    }

    schemas["apollo_enrich_person"] = {
        "type": "function",
        "function": {
            "name": "apollo_enrich_person",
            "description": (
                "Enrich a person to get their email and phone number. "
                "**COSTS CREDITS.** Provide email, linkedin_url, or "
                "(first_name + last_name + organization_name)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string", "description": "First name"},
                    "last_name": {"type": "string", "description": "Last name"},
                    "email": {"type": "string", "description": "Known email address"},
                    "organization_name": {
                        "type": "string",
                        "description": "Company name (helps disambiguation)",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Company domain (e.g. 'apollo.io')",
                    },
                    "linkedin_url": {
                        "type": "string",
                        "description": "LinkedIn profile URL",
                    },
                    "reveal_personal_emails": {
                        "type": "boolean",
                        "description": "Include personal emails (default false)",
                    },
                    "reveal_phone_number": {
                        "type": "boolean",
                        "description": "Include phone numbers (default false)",
                    },
                },
            },
        },
    }

    schemas["apollo_search_companies"] = {
        "type": "function",
        "function": {
            "name": "apollo_search_companies",
            "description": (
                "Search Apollo.io for companies by name, domain, location, or size. "
                "**COSTS CREDITS.**"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "q_organization_name": {
                        "type": "string",
                        "description": "Company name to search for",
                    },
                    "organization_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Domains to search (e.g. ['apollo.io'])",
                    },
                    "organization_locations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Locations to filter by",
                    },
                    "organization_num_employees_ranges": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Employee count ranges (e.g. ['1,50', '51,200'])",
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Results per page (default 10, max 25)",
                    },
                },
            },
        },
    }

    schemas["apollo_enrich_company"] = {
        "type": "function",
        "function": {
            "name": "apollo_enrich_company",
            "description": (
                "Enrich a company by domain via Apollo.io. Returns firmographic data "
                "(industry, size, location, description). **COSTS CREDITS.**"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Company domain (e.g. 'apollo.io')",
                    },
                },
                "required": ["domain"],
            },
        },
    }

    # ── Todo list (in-conversation progress tracking) ──

    schemas["todo_write"] = {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "Replace the in-conversation todo list. Use to track progress on "
                "multi-step tasks. Each item needs content (imperative form), "
                "active_form (present continuous for status display), and status "
                "(pending/in_progress/completed). Max 1 item in_progress at a time. "
                "List auto-clears when all items completed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": ("Imperative form, e.g. 'Fix the login bug'"),
                                },
                                "active_form": {
                                    "type": "string",
                                    "description": (
                                        "Present continuous, e.g. 'Fixing the login bug'"
                                    ),
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                            },
                            "required": ["content", "active_form", "status"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    }

    # ── Identity mapping tools ──

    schemas["link_identity"] = {
        "type": "function",
        "function": {
            "name": "link_identity",
            "description": "Link a channel identity (github, jira, slack, etc.) to a CRM person. Upserts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "CRM person UUID"},
                    "channel": {
                        "type": "string",
                        "description": "Channel name: 'github', 'jira', 'jira_display_name', 'slack', etc.",
                    },
                    "identifier": {
                        "type": "string",
                        "description": "The handle/username on that channel",
                    },
                    "display_name": {
                        "type": "string",
                        "description": "Human-readable label (optional)",
                    },
                },
                "required": ["person_id", "channel", "identifier"],
            },
        },
    }
    schemas["resolve_identities"] = {
        "type": "function",
        "function": {
            "name": "resolve_identities",
            "description": "Look up all known identities for a person across all channels. Returns github, jira, email, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "person_id": {
                        "type": "string",
                        "description": "CRM person UUID (provide this OR channel+identifier)",
                    },
                    "channel": {
                        "type": "string",
                        "description": "Channel to look up (used with identifier)",
                    },
                    "identifier": {
                        "type": "string",
                        "description": "Handle on the channel (used with channel)",
                    },
                },
            },
        },
    }

    # ── Report rendering tools ──

    schemas["render_report"] = {
        "type": "function",
        "function": {
            "name": "render_report",
            "description": "Render any report type as HTML. Template must exist at reports/templates/{report_type}.html.",
            "parameters": {
                "type": "object",
                "properties": {
                    "report_type": {
                        "type": "string",
                        "description": "Template name without extension (e.g. 'devops_weekly', 'sales_pipeline')",
                    },
                    "report_data": {
                        "description": "Report data as JSON object or string — passed as template context",
                    },
                },
                "required": ["report_type", "report_data"],
            },
        },
    }
    schemas["render_devops_report"] = {
        "type": "function",
        "function": {
            "name": "render_devops_report",
            "description": "Render the devops weekly report as HTML (shortcut for render_report with type='devops_weekly').",
            "parameters": {
                "type": "object",
                "properties": {
                    "report_data": {
                        "description": "Structured report data with keys: period, executive_summary, jira, github, people, bottlenecks",
                    },
                },
                "required": ["report_data"],
            },
        },
    }

    # ── JIRA Cloud API tools ──

    schemas["jira_search"] = {
        "type": "function",
        "function": {
            "name": "jira_search",
            "description": "Search JIRA issues using JQL. Returns key, summary, status, assignee, story points, and dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "jql": {"type": "string", "description": "JQL query string"},
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default 50, max 100)",
                    },
                },
                "required": ["jql"],
            },
        },
    }
    schemas["jira_get_issue"] = {
        "type": "function",
        "function": {
            "name": "jira_get_issue",
            "description": "Get a single JIRA issue with changelog for cycle time analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key (e.g. 'ENG-123')"},
                },
                "required": ["issue_key"],
            },
        },
    }
    schemas["jira_get_sprint"] = {
        "type": "function",
        "function": {
            "name": "jira_get_sprint",
            "description": "Get active or recent sprint info for a board with completion rate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "board_id": {"type": "integer", "description": "JIRA board ID"},
                    "state": {
                        "type": "string",
                        "description": "Sprint state: 'active', 'closed', 'future' (default: active)",
                    },
                },
                "required": ["board_id"],
            },
        },
    }
    schemas["jira_get_board_velocity"] = {
        "type": "function",
        "function": {
            "name": "jira_get_board_velocity",
            "description": "Get velocity data for last N closed sprints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "board_id": {"type": "integer", "description": "JIRA board ID"},
                    "num_sprints": {
                        "type": "integer",
                        "description": "Past sprints to include (default 5, max 10)",
                    },
                },
                "required": ["board_id"],
            },
        },
    }
    schemas["jira_list_boards"] = {
        "type": "function",
        "function": {
            "name": "jira_list_boards",
            "description": "List available JIRA boards, optionally filtered by project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_key": {
                        "type": "string",
                        "description": "Filter by project key (optional)",
                    },
                },
            },
        },
    }

    # ── GitHub REST API tools ──

    schemas["github_list_prs"] = {
        "type": "function",
        "function": {
            "name": "github_list_prs",
            "description": "List pull requests for a GitHub repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in owner/repo format"},
                    "state": {
                        "type": "string",
                        "description": "Filter: 'open', 'closed', or 'all' (default: all)",
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort: 'created', 'updated', 'popularity' (default: updated)",
                    },
                    "per_page": {
                        "type": "integer",
                        "description": "Results per page (default 30, max 100)",
                    },
                    "max_pages": {"type": "integer", "description": "Max pages (default 3, max 5)"},
                },
                "required": ["repo"],
            },
        },
    }
    schemas["github_get_pr"] = {
        "type": "function",
        "function": {
            "name": "github_get_pr",
            "description": "Get a single PR with review timeline and time-to-first-review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in owner/repo format"},
                    "pr_number": {"type": "integer", "description": "Pull request number"},
                },
                "required": ["repo", "pr_number"],
            },
        },
    }
    schemas["github_pr_stats"] = {
        "type": "function",
        "function": {
            "name": "github_pr_stats",
            "description": "Aggregated PR metrics: avg/median cycle time, merge count, author breakdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in owner/repo format"},
                    "days": {
                        "type": "integer",
                        "description": "Look-back days (default 30, max 90)",
                    },
                },
                "required": ["repo"],
            },
        },
    }
    schemas["github_commit_activity"] = {
        "type": "function",
        "function": {
            "name": "github_commit_activity",
            "description": "Commit frequency by contributor for a repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in owner/repo format"},
                    "weeks": {
                        "type": "integer",
                        "description": "Recent weeks to show (default 12, max 52)",
                    },
                },
                "required": ["repo"],
            },
        },
    }
    schemas["github_review_stats"] = {
        "type": "function",
        "function": {
            "name": "github_review_stats",
            "description": "Code review participation: reviews given, approvals, turnaround per reviewer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in owner/repo format"},
                    "days": {
                        "type": "integer",
                        "description": "Look-back days (default 30, max 90)",
                    },
                },
                "required": ["repo"],
            },
        },
    }

    # ── DevOps metrics storage tools ──

    schemas["devops_store_metric"] = {
        "type": "function",
        "function": {
            "name": "devops_store_metric",
            "description": "Store a devops metric snapshot for trend analysis. Upserts by date + source + type + scope.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Data source: 'jira', 'github', or 'claude_teams'",
                    },
                    "metric_type": {
                        "type": "string",
                        "description": "Metric type (e.g. 'sprint_velocity', 'pr_cycle_time')",
                    },
                    "value": {"description": "Metric value — number, string, or JSON object"},
                    "snapshot_date": {
                        "type": "string",
                        "description": "Date YYYY-MM-DD (default: today)",
                    },
                    "scope": {"type": "string", "description": "Metric scope (default: 'team')"},
                    "scope_key": {
                        "type": "string",
                        "description": "Scope identifier (e.g. repo name)",
                    },
                },
                "required": ["source", "metric_type", "value"],
            },
        },
    }
    schemas["devops_query_metrics"] = {
        "type": "function",
        "function": {
            "name": "devops_query_metrics",
            "description": "Query stored devops metrics for trend analysis. Returns snapshots ordered by date descending.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Data source: 'jira', 'github', or 'claude_teams'",
                    },
                    "metric_type": {"type": "string", "description": "Metric type to query"},
                    "days": {
                        "type": "integer",
                        "description": "Look-back period in days (default 30, max 90)",
                    },
                    "scope": {"type": "string", "description": "Filter by scope (optional)"},
                    "scope_key": {
                        "type": "string",
                        "description": "Filter by scope key (optional)",
                    },
                },
                "required": ["source", "metric_type"],
            },
        },
    }

    schemas["buddy_refresh"] = {
        "type": "function",
        "function": {
            "name": "buddy_refresh",
            "description": (
                "Compute today's fleet achievement scores and persist to "
                "buddy_stats + agent_buddy_stats. Scores come from goals.py's "
                "compute_achievement_score — 0-100 per agent, weighted across "
                "that agent's declared goals."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }

    schemas["buddy_review_pass"] = {
        "type": "function",
        "function": {
            "name": "buddy_review_pass",
            "description": (
                "Sample recent runs for each agent with declared goals and "
                "write Buddy reviews to agent_reviews. One review per sampled "
                "run. Biased toward failures, runs with error steps, long runs. "
                "Skips runs already reviewed by Buddy. Uses Sonnet 4.6 to phrase "
                "evidence-grounded critiques — the LLM cannot invent content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "runs_per_agent": {
                        "type": "integer",
                        "description": "How many runs to sample per agent. Default 3.",
                    }
                },
            },
        },
    }

    schemas["buddy_aggregate_findings"] = {
        "type": "function",
        "function": {
            "name": "buddy_aggregate_findings",
            "description": (
                "Aggregate recent Buddy reviews and goal breaches into "
                "self-improve CRM tasks. For every (agent, breached_metric) "
                "above severity threshold, creates one task tagged "
                "nightwatch+self-improve+<agent>+<metric> assigned to auto-agent. "
                "Dedups against open tasks for the same (agent, metric)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_hours": {
                        "type": "integer",
                        "description": "Review-aggregation window in hours. Default 24.",
                    }
                },
            },
        },
    }

    schemas["buddy_verify_pass"] = {
        "type": "function",
        "function": {
            "name": "buddy_verify_pass",
            "description": (
                "Grade self-improve tasks. For every DONE self-improve task "
                "older than 48h, re-compute the metric from goals.py and tag "
                "verified_resolved or verify_failed (with escalation:N). At "
                "escalation:2 the task is re-routed to auto-researcher; at "
                "escalation:3 it is tagged requires_human=true. Also runs a "
                "7-day hold check on previously-verified tasks to populate "
                "the held_7d=true/false tag for the weekly auditor."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    }

    schemas["buddy_audit"] = {
        "type": "function",
        "function": {
            "name": "buddy_audit",
            "description": (
                "Weekly hold-rate audit. Computes the fraction of verified "
                "self-improve fixes that held for 7 days over the last 14 days. "
                "If the rate is below 30% (and there are at least 5 samples), "
                "pauses Buddy's cron by editing docs/agents/buddy.yaml and "
                "sends a critical alert to main. Above threshold: logs the "
                "healthy rate and does nothing."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    }

    return schemas
