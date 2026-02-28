# Agent Playbook

> AI-consumable reference for building, modifying, and debugging Robothor agents.
> Manifests in `docs/agents/*.yaml` are the **single source of truth** for each agent.
> Contracts: `docs/agents/schema.yaml` (manifest schema) + `docs/agents/INSTRUCTION_CONTRACT.md` (instruction file format).
> Run `python scripts/validate_agents.py` to check for drift.

---

## 0. Building Your First Agent

The fastest path from zero to a running agent:

```bash
# 1. Scaffold — creates manifest + instruction file from templates
robothor agent scaffold my-first-agent --description "A test agent to learn the framework"

# 2. Edit the manifest — choose model, schedule, tools
#    → docs/agents/my-first-agent.yaml

# 3. Write the instruction file — follow the contract (docs/agents/INSTRUCTION_CONTRACT.md)
#    → brain/MY_FIRST_AGENT.md

# 4. Validate — schema + structure + file existence + tool registration
python scripts/validate_agents.py --agent my-first-agent

# 5. Restart the engine to pick up the new agent
sudo systemctl restart robothor-engine

# 6. Monitor — run manually or wait for cron
robothor engine run my-first-agent         # Manual test
robothor engine history --agent my-first-agent  # Check run history
```

### What the scaffold creates

| File | Purpose |
|------|---------|
| `docs/agents/my-first-agent.yaml` | Manifest — model, schedule, tools, delivery, coordination |
| `brain/MY_FIRST_AGENT.md` | Instruction file — loaded as system prompt |

### Key decisions to make

1. **Model** — `openrouter/moonshotai/kimi-k2.5` (cheap, reliable tool calling) or `openrouter/anthropic/claude-sonnet-4.6` (quality-critical)
2. **Schedule** — Cron expression for periodic runs, or leave empty for hook-only / interactive agents
3. **Tools** — Whitelist via `tools_allowed`. Always include `exec`, `read_file`, `write_file` for file I/O.
4. **Hooks** — Event triggers from Redis Streams (e.g., trigger on `email.new`). Primary fast path; crons as safety net.
5. **Delivery** — `none` (silent worker), `announce` (delivers to user via Telegram), or `log`

### Contracts

Every agent must satisfy two contracts:

- **Manifest schema** (`docs/agents/schema.yaml`) — Machine-readable. Enforced by `validate_agents.py` and at engine startup. Required fields: `id`, `name`, `description`, `version`, `department`.
- **Instruction file contract** (`docs/agents/INSTRUCTION_CONTRACT.md`) — Defines the required sections (Identity, Your Role, Tasks, Output) and behaviors for agents with `task_protocol: true`.

---

## 1. Config Architecture

### 1.1 Manifest-First Design

The Python Agent Engine reads YAML manifests directly — there is no config generation step and no separate runtime config files. Each agent is defined by exactly **two things**:

| File | Location | What it controls |
|------|----------|-----------------|
| Manifest | `docs/agents/<id>.yaml` | Everything: model, schedule, tools, delivery, limits, coordination |
| Instruction file | `brain/<NAME>.md` | Bootstrap context loaded as system prompt (optional) |

The engine (`robothor/engine/`) loads manifests at startup via `config.py → manifest_to_agent_config()`. Changes take effect after `sudo systemctl restart robothor-engine`.

### 1.2 Instruction Source

The agent's system prompt is built from `instruction_file` + `bootstrap_files` (loaded in order, joined with `---` separators). There is no separate "payload message" — the manifest's instruction_file IS the primary instruction source.

Bootstrap file limits: 12,000 chars per file, 30,000 chars total.

### 1.3 Permission Layering

Two layers:

1. **Manifest `tools_allowed` / `tools_denied`** — Engine filters the tool schema before sending to the LLM. Tools not in `tools_allowed` are never presented. Tools in `tools_denied` are explicitly blocked.
2. **Tenant middleware** — All DAL calls are scoped by `tenant_id` (default: `robothor-primary`).

Tool execution goes directly through the ToolRegistry (DAL calls, no HTTP roundtrip to Bridge).

### 1.4 Model Selection at Runtime

Resolution order:
1. `model_override` parameter (if provided, e.g., from Telegram `/model` command)
2. `model.primary` from manifest
3. `model.fallbacks[]` from manifest — tried in order on failure

**Broken model tracking:** Models that return HTTP 401, 403, or 429 are removed from rotation for the remainder of that run. This prevents wasting tokens retrying rate-limited providers on every iteration.

Model aliases (for documentation reference only — manifests use full paths):

| Alias | Full model path |
|-------|----------------|
| kimi | openrouter/moonshotai/kimi-k2.5 |
| sonnet | openrouter/anthropic/claude-sonnet-4.6 |
| minimax | openrouter/minimax/minimax-m2.5 |
| gemini-pro | gemini/gemini-2.5-pro |
| gemini-flash | gemini/gemini-2.5-flash |

---

## 2. Manifest Schema

Each agent has a YAML manifest at `docs/agents/<id>.yaml`. Full field reference:

```yaml
# Required fields
id: string                    # kebab-case agent ID
name: string                  # Human-readable name
description: string           # What this agent does (one line)
version: "YYYY-MM-DD"        # Date of last manifest change
department: string            # email | calendar | operations | security | communications | crm | briefings | core | custom

# Hierarchy
reports_to: string            # Agent ID this reports to (usually "supervisor")
creates_tasks_for: [string]   # Agent IDs this creates tasks for
receives_tasks_from: [string] # Agent IDs that create tasks for this
escalates_to: string          # Agent ID for escalations (usually "supervisor")

# Runtime
model:
  primary: string             # Full model path (e.g., openrouter/moonshotai/kimi-k2.5)
  fallbacks: [string]         # Ordered fallback chain
  payload_alias: string       # Human-friendly alias (for docs/logs only)

schedule:
  cron: string                # Cron expression (e.g., "0 6-22/2 * * *"), empty for non-scheduled
  timezone: string            # IANA timezone (e.g., America/Grenada)
  timeout_seconds: int        # Max execution time (default: 600)
  max_iterations: int         # Max LLM loop iterations (default: 20, see section 6)
  session_target: string      # "isolated" (fresh each run) or "persistent"
  stagger_ms: int             # Optional startup delay (e.g., 300000 for vision-monitor)

delivery:
  mode: string                # "announce" (delivers output) or "none" (silent)
  channel: string             # "telegram" (only when mode=announce)
  to: string                  # Telegram chat ID (only when mode=announce)

# Permissions
tools_allowed: [string]       # Tools available to the agent
tools_denied: [string]        # Tools explicitly blocked
bridge_endpoints: [string]    # Bridge HTTP endpoints (for RBAC documentation)
streams:
  read: [string]              # Redis streams this agent can read
  write: [string]             # Redis streams this agent can write

# Coordination
task_protocol: bool           # Must follow task protocol (list_my_tasks → process → resolve)
review_workflow: bool         # Sends tasks to REVIEW status for supervisor approval
notification_inbox: bool      # Checks get_inbox at start of run
status_file: string           # Path to status file (e.g., brain/memory/<id>-status.md)
shared_working_state: bool    # Appends to shared_working_state block at end of run

# Warmup — pre-loaded context for cron/hook runs
warmup:
  memory_blocks: [string]     # Memory blocks to read at start (e.g., operational_findings)
  context_files: [string]     # Files to read and include (e.g., status files)
  peer_agents: [string]       # Peer agent status to fetch

# Files
instruction_file: string      # Path to .md instruction file (null if payload-only)
bootstrap_files: [string]     # Shared bootstrap files (typically AGENTS.md + TOOLS.md)

# Downstream
downstream_agents: [string]   # Agents to trigger after successful cron run

# Event hooks — Redis Stream triggers (parsed at engine startup)
hooks:
  - stream: string            # Redis Stream name (e.g., "email", "calendar")
    event_type: string        # Event type filter (e.g., "email.new")
    message: string           # Initial prompt sent to agent when triggered

# Tags
tags_produced: [string]       # Task tags this agent creates
tags_consumed: [string]       # Task tags this agent processes

# SLA (must match section 6)
sla:
  urgent: string              # e.g., "30m"
  high: string                # e.g., "2h"
  normal: string              # e.g., "8h"
  low: string                 # e.g., "24h"

# Changelog
changelog:
  - date: "YYYY-MM-DD"
    change: string
```

---

## 3. Fleet Registry (Example: Robothor's Deployment)

> This section documents the specific agent fleet deployed for Robothor.
> Your deployment will have different agents. Use this as a reference for structure and conventions.

### 3.1 Active Fleet

| ID | Dept | Model | Schedule | Delivery | max_iter | Instruction File |
|----|------|-------|----------|----------|----------|-----------------|
| main | core | Gemini Flash | *(interactive)* | none | 30 | SOUL.md |
| email-classifier | email | Kimi K2.5 | `0 6-22/6 * * *` | none | 10 | EMAIL_CLASSIFIER.md |
| email-analyst | email | Kimi K2.5 | `30 8-20/6 * * *` | none | 10 | EMAIL_ANALYST.md |
| email-responder | email | Sonnet 4.6 | `0 8-20/4 * * *` | none | 15 | RESPONDER.md |
| calendar-monitor | calendar | Kimi K2.5 | `0 6-22/6 * * *` | none | 8 | CALENDAR_MONITOR.md |
| supervisor | operations | Kimi K2.5 | `0 6-22/4 * * *` | announce | 15 | HEARTBEAT.md |
| vision-monitor | security | Kimi K2.5 | `0 6-22/6 * * *` | none | 5 | *(payload-only)* |
| conversation-inbox | communications | Kimi K2.5 | `0 6-22 * * *` | none | 5 | CONVERSATION_INBOX.md |
| conversation-resolver | communications | Kimi K2.5 | `0 8,14,20 * * *` | none | 5 | CONVERSATION_RESOLVER.md |
| crm-steward | crm | Kimi K2.5 | `0 10 * * *` | none | 10 | CRM_STEWARD.md |
| morning-briefing | briefings | Kimi K2.5 | `30 6 * * *` | announce | 10 | *(payload-only)* |
| evening-winddown | briefings | Kimi K2.5 | `0 21 * * *` | announce | 10 | *(payload-only)* |

### 3.2 Org Chart

```
                    ┌──────────────┐
                    │  supervisor  │ (Kimi K2.5, every 2h)
                    │ HEARTBEAT.md │ Reads status files, approves REVIEW tasks
                    └──────┬───────┘
           ┌───────────────┼───────────────┬────────────────┐
           ▼               ▼               ▼                ▼
    ┌─────────────┐ ┌─────────────┐ ┌──────────────┐ ┌───────────┐
    │   EMAIL     │ │  CALENDAR   │ │    COMMS      │ │   CRM     │
    └──────┬──────┘ └──────┬──────┘ └──────┬───────┘ └─────┬─────┘
           │               │               │               │
    classifier ──┐  calendar-monitor  conv-inbox      crm-steward
    (routes)     │                    conv-resolver
           │     │
    analyst ◄────┘ (analytical tasks)
           │
    responder (sends replies)

    main (interactive, Telegram) — reports to nobody
```

### 3.3 Task Routing Map

| Creator | Assigned To | Tags | Priority |
|---------|------------|------|----------|
| email-classifier | email-responder | email, reply-needed | normal |
| email-classifier | email-analyst | email, analytical | normal |
| email-classifier | supervisor | email, escalation, needs-philip | high |
| calendar-monitor | supervisor | calendar, conflict/cancellation | high |
| conversation-inbox | supervisor | conversation, escalation | high |
| vision-monitor | supervisor | vision, unknown-person | urgent |
| crm-steward | supervisor (via REVIEW) | crm-hygiene, dedup | normal |
| email-responder | supervisor (via REVIEW) | *(inherits from task)* | *(inherits)* |

### 3.4 Pipeline Flow

```
Gmail ──email_sync.py (*/5m)──► email_hook.py (real-time, ~130s total)
  Stage 1: triage_prep.py → triage-inbox.json
  Stage 2: Email Classifier → create_task(email-responder/supervisor)
  Stage 3: Email Responder → gog gmail send → resolve/REVIEW

Safety net crons:
  :00 Classifier  :30 Analyst  (every 2h)
  :00 Responder (every 4h)
```

---

## 4. Procedures

### 4.1 Adding a New Agent

1. Create `docs/agents/<id>.yaml` manifest (copy existing, fill fields)
2. Create `brain/<INSTRUCTION>.md` (or set `instruction_file: null` for payload-only)
3. `python scripts/validate_agents.py --agent <id>`
4. `sudo systemctl restart robothor-engine`
5. Monitor first cron run via engine health: `curl localhost:18800/health`
6. Commit all files: `agent(<id>): add new agent`

### 4.2 Modifying an Agent

Edit manifest FIRST, then update any related files.

| Change type | Files to update | Validation |
|---|---|---|
| Model change | manifest `model:` section | `validate --agent <id>`, restart engine |
| Schedule change | manifest `schedule:` section | `validate --agent <id>`, restart engine |
| Add/remove tool | manifest `tools_allowed`/`tools_denied` | `validate --agent <id>`, restart engine |
| Behavior change | instruction file `brain/*.md`, manifest changelog | manual test |
| Delivery change | manifest `delivery:` section | `validate --agent <id>`, restart engine |
| Iteration limit | manifest `schedule.max_iterations` | `validate --agent <id>`, restart engine |

### 4.3 Rolling Back an Agent

**Surgical (per-agent):**

1. `git log --oneline -- docs/agents/<id>.yaml`
2. `git checkout <commit> -- docs/agents/<id>.yaml`
3. `python scripts/validate_agents.py --agent <id>`
4. `sudo systemctl restart robothor-engine`

**Nuclear (all agents):**

1. `git log --oneline -- docs/agents/`
2. `git checkout <commit> -- docs/agents/`
3. `sudo systemctl restart robothor-engine`

### 4.4 Decommissioning an Agent

1. Delete the manifest from `docs/agents/`
2. Wait for in-flight tasks to complete (`list_tasks --agent <id>`)
3. Archive instruction .md file (move to `docs/agents/archived/`)
4. Remove status file reference from HEARTBEAT.md
5. Restart engine, validate, commit

### 4.5 Changing a Shared Policy

1. Edit PLAYBOOK.md (this file) — e.g., change SLA thresholds
2. Identify affected agents (which manifests reference the changed policy)
3. Update affected manifests
4. Validate all affected agents
5. Commit: `policy: <what changed>`

Policy changes do NOT auto-propagate. The AI decides which agents need updating.

---

## 5. Conventions

| Convention | Rule |
|-----------|------|
| Agent ID | kebab-case (`email-classifier`, not `EmailClassifier`) |
| Instruction file | ALL_CAPS.md in `brain/` (`EMAIL_CLASSIFIER.md`) |
| Status file | `brain/memory/<agent-id>-status.md` |
| Manifest version | `YYYY-MM-DD` (date of last manifest change) |
| Commit: single agent | `agent(<id>): <what changed>` |
| Commit: all agents | `agent(*): <what changed>` |
| Commit: policy | `policy: <what changed>` |
| Commit: engine infra | `engine: <what changed>` |
| Commit: bug fix | `fix(agent/<id>): <what was broken>` |

**Tag vocabulary** (don't invent new tags without updating this list):

`email`, `reply-needed`, `analytical`, `escalation`, `needs-philip`, `calendar`, `conflict`, `cancellation`, `vision`, `unknown-person`, `crm-hygiene`, `dedup`, `enrichment`, `conversation`

**Priority values:** `urgent`, `high`, `normal`, `low`

---

## 6. Shared Policies (Single Canonical Source)

| Policy | Value |
|--------|-------|
| SLA deadlines | urgent=30m, high=2h, normal=8h, low=24h |
| Task protocol | `list_my_tasks` → `IN_PROGRESS` → process → `resolve`/`REVIEW` → `append shared_working_state` |
| Status flow | `TODO` → `IN_PROGRESS` → `REVIEW` → `DONE` (app-enforced state machine) |
| REVIEW approvers | supervisor and helm-user only |
| Model selection | Sonnet for quality-critical (responder). Gemini Flash for interactive (main). Kimi K2.5 for all others. |
| Fallback chain | primary → fallback[0] → fallback[1] (typically kimi → sonnet/minimax → gemini-pro) |
| Broken model tracking | Models returning 401/403/429 are removed from rotation for the rest of that run |
| Max iterations (default) | 20 — override per-agent via `schedule.max_iterations` |
| Max iterations (guideline) | 5 for simple checkers, 8-10 for processors, 15 for complex agents, 30 for interactive |
| Output limit | <500 chars, emoji prefix. Workers write status file and stop silently. HEARTBEAT_OK is supervisor-only. |
| Credentials | NEVER hardcode. Env vars via SOPS → `/run/robothor/secrets.env`. |
| Status files | MANDATORY every run. Supervisor considers >35min = stale. |
| Bootstrap budget | 12,000 chars per file, 30,000 chars total |

---

## 7. Debugging Guide

### Diagnostic Steps

| Step | Check | How |
|------|-------|-----|
| 1 | Is it running? | `curl localhost:18800/health` → check agent status |
| 2 | Is it timing out? | Health endpoint shows `last_duration_ms` vs manifest `timeout_seconds` |
| 3 | Is it hitting iteration limits? | Engine logs: `Max iterations reached` |
| 4 | Model rate-limited? | Engine logs: `permanently failed (403)` |
| 5 | Producing output? | Check delivery config (`announce` vs `none`) |
| 6 | Routing correctly? | `list_tasks`, check tags and `assignedToAgent` |

### Common Failures

| Failure | Root Cause | Fix |
|---------|-----------|-----|
| All models failed | Rate limits, API keys expired | Check engine logs, verify SOPS secrets |
| Token blowout | `max_iterations` too high for agent's workload | Reduce `schedule.max_iterations` in manifest |
| Stale status file | Agent errored mid-run, didn't reach status write | Check engine health for `last_status` truth |
| UUID parse error | LLM passed non-UUID to personId/companyId | Bridge returns 422 — agent should retry |
| Tool not available | Not in `tools_allowed` list | Add to manifest, restart engine |
| Agent outputs nothing useful | Missing `exec`, `read_file`, `write_file` in tools_allowed | Every agent that reads files, runs CLI, or writes status MUST have these 3 tools |
| 500 on task creation | Invalid field types (tags as string not array) | Check Bridge logs: `journalctl -u robothor-bridge` |

### Live State Checks

```bash
# Engine health (all agents at a glance)
curl -s localhost:18800/health | python3 -m json.tool

# Engine logs (recent)
journalctl -u robothor-engine --since "5 min ago" --no-pager | tail -30

# Bridge health
curl -s localhost:9100/health | python3 -m json.tool

# Agent tasks
curl -s localhost:9100/api/tasks/agent/<agent-id> | python3 -m json.tool

# Agent run history (DB)
psql robothor_memory -c "SELECT agent_id, status, duration_ms, input_tokens, error_message FROM agent_runs ORDER BY started_at DESC LIMIT 10;"
```

---

## 8. Declarative Workflow Engine

Multi-step agent pipelines defined in YAML at `docs/workflows/*.yaml`. The engine (`robothor/engine/workflow.py`) executes workflows triggered by Redis Stream events or cron schedules.

### Step types

| Type | What it does |
|------|-------------|
| `agent` | Run an agent via `runner.execute()` — full tool access per agent's manifest |
| `tool` | Call a tool directly (skip LLM) — args support `{{ }}` templates |
| `condition` | Branch based on previous step output — Python expressions with `value` variable |
| `transform` | Reshape data between steps |
| `noop` | Explicit pipeline end marker |

### Trigger types

| Type | Example |
|------|---------|
| `hook` | `stream: email`, `event_type: email.new` — fires on Redis Stream event |
| `cron` | `cron: "0 6-22/6 * * *"` — APScheduler, registered at startup |

### Active workflows

| ID | Steps | Triggers |
|----|-------|----------|
| `email-pipeline` | classify → condition → analyze/respond → done | hook email.new + 6h cron |
| `calendar-pipeline` | monitor → done | hook calendar.* + 6h cron |
| `vision-pipeline` | check → done | hook vision.person_unknown |

### CLI

```bash
robothor engine workflow list    # Show loaded workflows
robothor engine workflow run <id>  # Manual trigger
```

### API (port 18800)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/workflows` | GET | List workflow definitions |
| `/api/workflows/{id}/runs` | GET | List runs for a workflow |
| `/api/workflows/runs/{run_id}` | GET | Run detail with step results |
| `/api/workflows/{id}/execute` | POST | Manual trigger |

### DB tables

`workflow_runs` and `workflow_run_steps` (migration 013). Per-step audit trail with agent_run_id FK for agent steps.

---

## 9. Lessons Learned

| Lesson | Context |
|--------|---------|
| **Every agent needs `exec`, `read_file`, `write_file`** | `build_for_agent()` strictly filters — if tools_allowed is set, ONLY those tools appear. Without `read_file`, agents can't read data files. Without `exec`, they can't run `gog` CLI. |
| **`read_file` uses workspace-relative paths** | Workspace is `~/robothor/`. Brain files are at `~/clawd/` but accessible via symlink at `brain/`. Instruction files should reference `brain/memory/triage-inbox.json`. |
| **HEARTBEAT_OK is supervisor-only** | Workers write status files and stop silently. Cargo-culting HEARTBEAT_OK into worker instructions causes them to skip real work. |
| **Event hooks are the primary trigger** | Crons are 6h safety nets. The fast path is: Python sync → Redis Stream → hook → agent (email: ~60s end-to-end). |
| **Validate after every manifest change** | `python scripts/validate_agents.py --agent <id>` catches missing tools, broken file paths, invalid crons. |

---

Updated: 2026-02-28

---

*Contracts: `docs/agents/schema.yaml` · `docs/agents/INSTRUCTION_CONTRACT.md` · Templates: `templates/agent-manifest.yaml` · `templates/agent-instructions.md`*
