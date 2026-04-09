"""Constants for the tool registry."""

from __future__ import annotations

# Sub-agent spawning tools
SPAWN_TOOLS = frozenset({"spawn_agent", "spawn_agents"})

# Git tools (Nightwatch system)
GIT_TOOLS = frozenset(
    {"git_status", "git_diff", "git_branch", "git_commit", "git_push", "create_pull_request"}
)

# Google Workspace tools (gws CLI)
GWS_TOOLS = frozenset(
    {
        "gws_gmail_search",
        "gws_gmail_get",
        "gws_gmail_reply",
        "gws_gmail_send",
        "gws_gmail_modify",
        "gws_calendar_list",
        "gws_calendar_create",
        "gws_calendar_delete",
        "gws_chat_send",
        "gws_chat_list_spaces",
        "gws_chat_list_messages",
    }
)

# Browser automation tool
BROWSER_TOOLS = frozenset({"browser"})

# In-conversation todo list
TODO_TOOLS = frozenset({"todo_write"})

# Desktop control tools (computer use)
DESKTOP_TOOLS = frozenset(
    {
        "desktop_screenshot",
        "desktop_click",
        "desktop_double_click",
        "desktop_right_click",
        "desktop_mouse_move",
        "desktop_drag",
        "desktop_scroll",
        "desktop_type",
        "desktop_key",
        "desktop_window_list",
        "desktop_window_focus",
        "desktop_launch",
        "desktop_describe",
    }
)

# Federation tools
FEDERATION_TOOLS = frozenset({"federation_query", "federation_trigger", "federation_sync_status"})

# Princess Freya (PF) vessel tools
PF_TOOLS = frozenset({"pf_system_status"})

# Skill tools
SKILL_TOOLS = frozenset({"invoke_skill", "list_skills"})

# MCP client tools (call external MCP servers)
MCP_CLIENT_TOOLS = frozenset(
    {"mcp_list_servers", "mcp_list_tools", "mcp_call_tool", "mcp_read_resource"}
)

# Messaging and team tools
MESSAGING_TOOLS = frozenset(
    {
        "send_agent_message",
        "receive_agent_messages",
        "create_team",
        "team_scratchpad_write",
        "team_scratchpad_read",
    }
)

# AutoResearch experiment tools
EXPERIMENT_TOOLS = frozenset(
    {"experiment_create", "experiment_measure", "experiment_commit", "experiment_status"}
)

# AutoAgent benchmark tools
BENCHMARK_TOOLS = frozenset({"benchmark_define", "benchmark_run", "benchmark_compare"})

# Apollo.io contact enrichment & search tools
APOLLO_TOOLS = frozenset(
    {
        "apollo_search_people",
        "apollo_enrich_person",
        "apollo_search_companies",
        "apollo_enrich_company",
    }
)

# JIRA Cloud API tools (dev team operations)
JIRA_TOOLS = frozenset(
    {
        "jira_search",
        "jira_get_issue",
        "jira_get_sprint",
        "jira_get_board_velocity",
        "jira_list_boards",
    }
)

# GitHub REST API tools (dev team operations)
GITHUB_API_TOOLS = frozenset(
    {
        "github_list_prs",
        "github_get_pr",
        "github_pr_stats",
        "github_commit_activity",
        "github_review_stats",
    }
)

# DevOps metrics tools (build/deploy/incident tracking)
DEVOPS_METRICS_TOOLS = frozenset(
    {
        "devops_get_deployments",
        "devops_get_incidents",
        "devops_get_metrics_summary",
    }
)

# Branches that agents are NEVER allowed to push to or commit on
PROTECTED_BRANCHES = frozenset({"main", "master"})

# Read-only tools for plan mode — tools with no side effects.
READONLY_TOOLS: frozenset[str] = frozenset(
    {
        # File/system
        "read_file",
        "list_directory",
        # Web
        "web_fetch",
        "web_search",
        # Memory read-only tools
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
        # Vision read-only tools
        "look",
        "who_is_here",
        "list_enrolled_faces",
        # Engine status
        "list_agent_runs",
        "get_agent_run",
        "list_agent_schedules",
        "get_agent_stats",
        # Vault read-only tools
        "vault_get",
        "vault_list",
        # Reasoning
        "deep_reason",
        # PDF
        "analyze_pdf",
        # Federation read-only tools
        "federation_query",
        "federation_sync_status",
        # PF vessel read-only tools
        "pf_system_status",
        # Git read-only tools
        "git_status",
        "git_diff",
        # Google Workspace (read-only)
        "gws_gmail_search",
        "gws_gmail_get",
        "gws_calendar_list",
        "gws_chat_list_spaces",
        "gws_chat_list_messages",
        # Desktop read-only tools
        "desktop_screenshot",
        "desktop_window_list",
        "desktop_describe",
        # Messaging read-only tools
        "receive_agent_messages",
        "team_scratchpad_read",
        # Experiment read-only tools
        "experiment_status",
        # Benchmark read-only tools
        "benchmark_compare",
        # Apollo read-only tools (search is free, no side effects)
        "apollo_search_people",
        # Skill tools (read-only — skills are just instructions)
        "invoke_skill",
        "list_skills",
        # MCP client read-only tools
        "mcp_list_servers",
        "mcp_list_tools",
    }
)
