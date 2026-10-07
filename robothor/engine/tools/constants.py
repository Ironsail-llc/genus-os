"""Constants for the tool registry."""

from __future__ import annotations

#: How many characters of one tool's result are PERSISTED with the run step.
#:
#: Precisely: ``robothor.engine.tracking._truncate_json`` cuts to this, head
#: and tail with a ``[... truncated N chars ...]`` marker in the middle, on the
#: two paths that write the ``steps`` row. It is NOT applied to the message the
#: model sees — that is built in ``AgentSession.record_tool_call``, where the
#: only shortening is ``tool_offload_threshold``, which defaults to 0 and is
#: therefore off.
#:
#: A handler still has to fit inside it — for narrower reasons than the first
#: two corrections claimed, both of which were also wrong. Resume reads
#: ``agent_run_checkpoints.messages`` and persistent history reads
#: ``chat_messages.message``; neither selects this column. Nor does the run
#: viewer: ``crm/bridge/routers/runs.py`` lists the step columns it wants and
#: ``tool_output`` is not among them.
#:
#: Two things do read it back:
#:
#: * ``scripts/cleanup_benchmark_crm_debris.py`` parses ``tool_output->>'id'``
#:   to find the CRM rows a benchmark left behind. A row cut mid-JSON is debris
#:   the cleanup cannot identify, so cannot remove — the one place where
#:   truncation does real damage today;
#: * ``bench/wildclaw/run_one.py`` selects it per step when grading a run.
#:
#: Cutting blind also lands the hole wherever the character count falls:
#: ``gws_gmail_get`` returned the raw Gmail API JSON with the body as one
#: base64 string, so the stored record of every long email was two halves of a
#: base64 blob. A handler that caps itself keeps the beginning intact and says
#: ``body_truncated``.
#:
#: Defined here, in the leaf module, because both the writer (``tracking``) and
#: the handlers that fit inside it need it, and a second copy of a number is a
#: second number.
MAX_TOOL_OUTPUT_CHARS = 4000

# Sub-agent spawning tools
SPAWN_TOOLS = frozenset({"spawn_agent", "spawn_agents"})

# Git tools (Nightwatch system)
GIT_TOOLS = frozenset(
    {"git_status", "git_diff", "git_branch", "git_commit", "git_push", "create_pull_request"}
)

# Claude Code driver (robothor/engine/coding/). Opt-in: listed in OPT_IN_TOOLS.
CLAUDE_CODE_TOOLS = frozenset(
    {
        "claude_code_start",
        "claude_code_status",
        "claude_code_wait",
        "claude_code_followup",
        "claude_code_cancel",
    }
)

# pr-reviewer suite (robothor/pr_review/). Opt-in: listed in OPT_IN_TOOLS.
PR_REVIEW_TOOLS = frozenset({"pr_review_intake", "pr_review_prepare", "pr_review_finalize"})

# ── Workspace tool families (mail, calendar, chat) ──────────────────────
#
# The ONE place a Workspace tool name is spelled. Every guard, accounting and
# verification table elsewhere derives from these, so a tool keeps every guard
# whichever provider serves it. ``test_tool_families.py`` fails on a literal
# Workspace tool name anywhere else in the package.

MAIL_SEARCH_TOOL = "gws_gmail_search"
MAIL_GET_TOOL = "gws_gmail_get"
MAIL_SEND_TOOL = "gws_gmail_send"
MAIL_REPLY_TOOL = "gws_gmail_reply"
MAIL_MODIFY_TOOL = "gws_gmail_modify"
CALENDAR_LIST_TOOL = "gws_calendar_list"
CALENDAR_CREATE_TOOL = "gws_calendar_create"
CALENDAR_UPDATE_TOOL = "gws_calendar_update"
CALENDAR_ADD_ATTENDEES_TOOL = "gws_calendar_add_attendees"
CALENDAR_RESPOND_TOOL = "gws_calendar_respond"
CALENDAR_DELETE_TOOL = "gws_calendar_delete"
CHAT_SEND_TOOL = "gws_chat_send"
CHAT_LIST_SPACES_TOOL = "gws_chat_list_spaces"
CHAT_LIST_MESSAGES_TOOL = "gws_chat_list_messages"

MAIL_READ_TOOLS: frozenset[str] = frozenset({MAIL_SEARCH_TOOL, MAIL_GET_TOOL})
#: Tools that put a message in someone's inbox.
MAIL_SEND_TOOLS: frozenset[str] = frozenset({MAIL_SEND_TOOL, MAIL_REPLY_TOOL})
MAIL_WRITE_TOOLS: frozenset[str] = MAIL_SEND_TOOLS | {MAIL_MODIFY_TOOL}

CALENDAR_READ_TOOLS: frozenset[str] = frozenset({CALENDAR_LIST_TOOL})
CALENDAR_CREATE_TOOLS: frozenset[str] = frozenset({CALENDAR_CREATE_TOOL})
#: Edits to an existing event (the effect-ledger journalled writes).
CALENDAR_EDIT_TOOLS: frozenset[str] = frozenset(
    {CALENDAR_UPDATE_TOOL, CALENDAR_ADD_ATTENDEES_TOOL, CALENDAR_RESPOND_TOOL}
)
CALENDAR_WRITE_TOOLS: frozenset[str] = (
    CALENDAR_CREATE_TOOLS | CALENDAR_EDIT_TOOLS | {CALENDAR_DELETE_TOOL}
)

CHAT_READ_TOOLS: frozenset[str] = frozenset({CHAT_LIST_SPACES_TOOL, CHAT_LIST_MESSAGES_TOOL})
CHAT_SEND_TOOLS: frozenset[str] = frozenset({CHAT_SEND_TOOL})
CHAT_TOOLS: frozenset[str] = CHAT_READ_TOOLS | CHAT_SEND_TOOLS

WORKSPACE_TOOLS: frozenset[str] = (
    MAIL_READ_TOOLS | MAIL_WRITE_TOOLS | CALENDAR_READ_TOOLS | CALENDAR_WRITE_TOOLS | CHAT_TOOLS
)

# Google Workspace tools (gws CLI). Kept as the historical name.
GWS_TOOLS = WORKSPACE_TOOLS

# Browser automation tool
BROWSER_TOOLS = frozenset({"browser"})

# In-conversation todo list
TODO_TOOLS = frozenset({"todo_write"})

# Long-running per-agent goals
GOAL_TOOLS = frozenset({"create_goal", "get_goal", "update_goal"})

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

# Skill tools
SKILL_TOOLS = frozenset({"invoke_skill", "list_skills", "create_skill", "update_skill"})

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
        # Workflow approvals: a delivery agent relays the operator's decision
        # from chat into the engine.
        "list_pending_approvals",
        "approve_workflow_step",
        "reject_workflow_step",
    }
)

# AutoResearch experiment tools
EXPERIMENT_TOOLS = frozenset(
    {"experiment_create", "experiment_measure", "experiment_commit", "experiment_status"}
)

# AutoAgent benchmark tools
BENCHMARK_TOOLS = frozenset(
    {
        "benchmark_define",
        "benchmark_run",
        "benchmark_compare",
        "benchmark_run_for_agent",
        "benchmark_run_fleet",
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
        "github_pr_diff",
        "github_pr_files",
        "github_compare",
    }
)

# GitHub review-posting tools: they write to someone else's pull request, so
# they are OPT_IN_TOOLS (below): offered only to a manifest that names them.
GITHUB_REVIEW_WRITE_TOOLS = frozenset(
    {
        "github_create_review",
        "github_reply_review_comment",
        "github_resolve_threads",
    }
)

# DevOps metrics storage tools
DEVOPS_METRICS_TOOLS = frozenset(
    {
        "devops_store_metric",
        "devops_query_metrics",
    }
)

# Identity mapping tools (CRM contact_identifiers)
IDENTITY_TOOLS = frozenset(
    {
        "link_identity",
        "resolve_identities",
    }
)

# Report rendering tools
REPORT_TOOLS = frozenset(
    {
        "render_report",
        "render_devops_report",
    }
)

# Branches that agents are NEVER allowed to push to or commit on
PROTECTED_BRANCHES = frozenset({"main", "master"})

# ── Deny-list entries that name no registered tool, on purpose ─────────
#
# A tool name in an ALLOW table has to resolve or the table is a lie: the agent
# is advertised a tool that does not exist, or a manifest's entry is dropped
# after one journald warning. A name in a DENY table is different — denying
# something that does not exist costs nothing and covers the day a plugin, an
# adapter or a rename brings it into being. That is a real defence and it is
# also indistinguishable from rot, which is how `gws_calendar_update` (a
# real tool now),
# `gws_gmail_draft` and `send_email` survived in four engine tables for months
# while an agent read them in the source and hallucinated calls to them.
#
# So the deliberate ones are written down HERE, once, and
# ``test_registered_tool_names.py`` enforces both directions: a deny table may
# only name a tool that is dispatchable or is listed below, and nothing listed
# below may be dispatchable — the moment one of these becomes a real tool, this
# entry has to go, and the suite says so.
UNREGISTERED_DENY_GUARDS: frozenset[str] = frozenset(
    {
        # Filesystem verbs other harnesses use; this engine has write_file.
        "append_file",
        "create_file",
        "edit_file",
        # Channel sends. Delivery here is the run's delivery_mode, and the
        # Telegram tools belong to a plugin that may or may not be installed.
        "message",
        "send_message",
        "send_telegram",
        "telegram_send",
        # A browser sub-verb and a voice verb, neither of them a tool name.
        "browser_navigate",
        "speak",
        # The vault writer is `vault_set`; `vault_put` is the name people try.
        "vault_put",
        # Scheduler verbs: the registered one is `register_user_cron`.
        "create_schedule",
        "register_cron",
        "update_schedule",
    }
)

# Read-only tools for plan mode — tools with no side effects.
READONLY_TOOLS: frozenset[str] = frozenset(
    {
        # File/system
        "read_file",
        "list_directory",
        # Looking at an image reads a file and mutates nothing. Asking a
        # vision model about a batch of them spends tokens, which is a cost
        # and not a side effect: nothing on this box or anywhere else changes.
        "view_image",
        "analyze_image",
        # Web
        "web_fetch",
        "web_render",
        "web_search",
        # Memory read-only tools
        "search_memory",
        "get_entity",
        "get_knowledge_gaps",
        "memory_block_read",
        "memory_block_list",
        # Knowledge Vault: search is read-only (caption-only, no value).
        # memory_vault_get is deliberately NOT readonly — it reveals values
        # and writes an audit row, so plan-mode cannot exfiltrate via it.
        "memory_vault_search",
        # Intent memory: search/list are read-only.
        "intent_search",
        "intent_list",
        # Symbolic memory: reads a stored tool-output file.
        "recall_node",
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
        "get_contact_360",
        "list_contact_messages",
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
        "classify_run_failure",
        "list_agent_schedules",
        "get_agent_stats",
        "get_agent_performance_summary",
        "get_goal",
        "get_pursuit_goal",
        "list_pursuit_goals",
        "report_pursuit_goal",
        # Buddy's per-run reviews and the fleet roll-up — SELECT-only.
        # Added 2026-08-21: agent-architect's instructions require citing a
        # review_id, and the benchmark harness (which derives its allow-list
        # from this set) was stripping the only tools that can produce one.
        "list_agent_reviews",
        "get_agent_review",
        "get_fleet_achievement_score",
        # Memory corpus statistics (SELECT-only aggregate).
        "get_stats",
        # Memory write-status probe (read-only)
        "memory_write_status",
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
        # Git read-only tools
        "git_status",
        "git_diff",
        # Google Workspace (read-only)
        *MAIL_READ_TOOLS,
        *CALENDAR_READ_TOOLS,
        *CHAT_READ_TOOLS,
        # Desktop read-only tools
        "desktop_screenshot",
        "desktop_window_list",
        "desktop_describe",
        # Messaging read-only tools.
        # `receive_agent_messages` is deliberately NOT here: `Messenger.receive`
        # is an `rpop`, so "reading" the inbox destroys it. Classified read-only
        # until 2026-08-21, which let plan mode — and, once the benchmark
        # allow-list started deriving from this set, benchmark sub-agents —
        # drain a live agent's Redis inbox.
        "team_scratchpad_read",
        # Experiment read-only tools
        "experiment_status",
        # Benchmark read-only tools
        "benchmark_compare",
        # Skill tools (list is read-only; invoke_skill writes usage metadata)
        "list_skills",
        # MCP client read-only tools
        "mcp_list_servers",
        "mcp_list_tools",
        # JIRA read-only tools
        "jira_search",
        "jira_get_issue",
        "jira_get_sprint",
        "jira_get_board_velocity",
        "jira_list_boards",
        # GitHub API read-only tools
        "github_list_prs",
        "github_get_pr",
        "github_pr_stats",
        "github_commit_activity",
        "github_review_stats",
        "github_pr_diff",
        "github_pr_files",
        "github_compare",
        # DevOps metrics read-only tools
        "devops_query_metrics",
        # Identity read-only tools
        "resolve_identities",
        # Report rendering (pure output, no side effects)
        "render_report",
        "render_devops_report",
        # Claude Code driver: reading a coding job's state changes nothing.
        # Unclassified until 2026-10-05, so a claude_code_wait cut short by its
        # deadline was journalled as an UNCERTAIN write, and the effects guard
        # then refused every later wait for that principal across runs.
        "claude_code_status",
        "claude_code_wait",
    }
)


# ── Deferred / searchable tool loading (Rip 16 / G4) ──────────────────────
# The meta-tools that drive tools-as-code. Excluded from the normal advertised
# set; injected only when an agent's toolset is deferred (see registry).
TOOLSEARCH_TOOLS = frozenset({"tool_search", "tool_describe", "tool_call"})

# Registered, but never part of the default "everything" set an agent with no
# `tools_allowed` receives. An agent gets one of these only by naming it in its
# manifest — the same opt-in shape as SPAWN_TOOLS (gated on can_spawn_agents)
# and TODO_TOOLS (gated on todo_list_enabled), and for the same reason.
#
# The ten sales tools are a subsystem most instances never deploy; advertising
# them cost every agent on every instance ~6.5k characters of schema and two
# unbounded CRM writes (`sales_discover`, `sales_propose_email`) it had no
# business being offered. `web_render` drives a headless browser, which is a
# separate capability from `web_fetch` and should be asked for. The
# `claude_code_*` tools start a whole autonomous coding agent that spends real
# money; only an agent whose manifest names them (`tools_allowed`, or
# `tools_opt_in` on top of the default set) is offered them. The
# `pr_review_*` tools post reviews and read a chat space; same rule.
#
# Advertisement is only half the gate. RBAC is the other half: the `__default__`
# `service`/`user` roles hold a `*` allow, so migration 138 adds the matching
# explicit denies. Both halves are needed — a tool an agent cannot see is still
# a tool it can name.
#
# The three GitHub review-posting tools post, reply and resolve on pull
# requests other people own; an agent reviews code only when its manifest
# says so. Their RBAC half is NOT added yet: a `service`-role deny would also
# stop the reviewer agent that names them, so it waits for a dedicated role.
#
# Spelled out rather than imported from robothor.sales so this module stays
# import-light; test_opt_in_tools_are_not_default pins the two together.
OPT_IN_TOOLS: frozenset[str] = frozenset(
    {
        "web_render",
        *GITHUB_REVIEW_WRITE_TOOLS,
        "sales_create_request",
        "sales_discover",
        "sales_get_context",
        "sales_get_prospect",
        "sales_get_report",
        "sales_get_request",
        "sales_get_workspace",
        "sales_process_queue",
        "sales_propose_email",
        "sales_research_parallel",
        *CLAUDE_CODE_TOOLS,
        *PR_REVIEW_TOOLS,
    }
)

# The always-advertised tool set when deferral is active. Chosen as the
# highest-frequency tools so most turns never need a tool_search round-trip;
# everything else loads on demand via tool_search → tool_describe → tool_call.
# GOAL_TOOLS are force-added by the registry filter, so they're omitted here.
CORE_TOOLS: frozenset[str] = frozenset(
    {
        # Durable operator goal controls must remain directly reachable.
        "create_pursuit_goal",
        "get_pursuit_goal",
        "list_pursuit_goals",
        "report_pursuit_goal",
        "update_pursuit_goal",
        # Calendar: the operator's meetings are everyday work, not a deferred
        # niche. Deferring them behind tool_search is how "add Bob to the 3pm"
        # became a capability gap.
        *CALENDAR_READ_TOOLS,
        *CALENDAR_WRITE_TOOLS,
        # File / shell
        "read_file",
        "write_file",
        "list_directory",
        "exec",
        # Web
        "web_fetch",
        "web_search",
        # Memory reads
        "search_memory",
        "memory_block_read",
        "memory_block_list",
        # Skills (the on-demand workflow layer)
        "invoke_skill",
        "list_skills",
        # CRM essentials (the operator's domain)
        "list_my_tasks",
        "get_task",
        "create_task",
        "search_records",
        # NOTE: no "message" here. It was listed for months and has never been
        # a registered schema, so under deferral the advertised set was one
        # shorter than it looked and an agent reading CORE_TOOLS in the source
        # would believe in a tool that does not exist. Delivery back to the
        # operator is the run's delivery_mode, not a tool.
        # Waiting / polling
        "wait_seconds",
    }
)
