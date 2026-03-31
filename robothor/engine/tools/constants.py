"""Constants for the tool registry."""

from __future__ import annotations

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
        # Healthcare read-only tools
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
        # Federation read-only tools
        "federation_query",
        "federation_sync_status",
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
    }
)
