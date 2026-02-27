# Agent Playbook

> AI-consumable reference for building, modifying, and debugging Robothor agents.
> Manifests in `docs/agents/*.yaml` are the source of truth for each agent.
> Run `python scripts/validate_agents.py` to check for drift.

---

## 1. Config Architecture

### 1.1 The 4 Config Files

| File | Location (git) | Runtime location | What it controls | Per-agent isolated? |
|------|---------------|-----------------|------------------|---------------------|
| openclaw.json | `runtime/openclaw.json` | `~/.openclaw/openclaw.json` | Model, fallbacks, deny list, workspace, heartbeat | No (array in `agents.list`) |
| jobs.json | `runtime/cron/jobs.json` | `~/.openclaw/cron/jobs.json` | Schedule, payload (instructions!), delivery, timeout | Yes (1 object per job) |
| agent_capabilities.json | `brain/agent_capabilities.json` | `~/clawd/agent_capabilities.json` | RBAC: allowed tools, Bridge endpoints, streams | Yes (keyed by agent ID) |
| Instruction files | `brain/*.md` | `~/clawd/*.md` | Bootstrap context (NOT primary instructions) | Yes (1 file per agent) |

### 1.2 Instruction Source: Payload vs File

**The agent's PRIMARY instructions come from `payload.message` in jobs.json, NOT from the .md instruction file.**

The .md file is loaded as bootstrap context, capped at `bootstrapMaxChars` (currently 12000 per file, 30000 total). If the payload says "do X" and the .md says "do Y", the payload wins.

When changing agent behavior: update jobs.json payload FIRST, then .md file for consistency.

### 1.3 Permission Layering

Three layers evaluated in order:

1. **Gateway deny list** (`openclaw.json agents.list[].tools.deny`) — blocks tool BEFORE any request
2. **Bridge RBAC** (`agent_capabilities.json` tools + endpoints) — blocks at HTTP layer
3. **Tenant middleware** (`X-Tenant-Id` header) — scopes data access

If a tool is in the deny list, RBAC never sees the request. If a tool is NOT in the deny list but NOT in RBAC allowed list, Bridge returns 403.

### 1.4 Model Selection at Runtime

Resolution order:
1. **Payload `model` alias** (in jobs.json) — takes priority if present
2. `openclaw.json agents.list[].model.primary` — used if no payload alias
3. `openclaw.json agents.defaults.model` — fallback if no agent-specific primary
4. **Fallback chain:** `agents.list[].model.fallbacks[]` — tried in order on failure

Model aliases (defined in `agents.defaults.models`):

| Alias | Full model path |
|-------|----------------|
| kimi | openrouter/moonshotai/kimi-k2.5 |
| sonnet | openrouter/anthropic/claude-sonnet-4.6 |
| minimax | openrouter/minimax/minimax-m2.5 |
| opus | anthropic/claude-opus-4-6 |
| gemini-pro | google/gemini-2.5-pro |
| gemini-flash | google/gemini-2.5-flash |
| qwen3-14b | ollama/qwen3:14b |
| qwen3 | ollama/qwen3-next:latest |

---

## 2. Manifest Schema

Each agent has a YAML manifest at `docs/agents/<id>.yaml`. Full field reference:

```yaml
# Required fields
id: string                    # kebab-case agent ID (must match across all config files)
name: string                  # Human-readable name
description: string           # What this agent does (one line)
version: "YYYY-MM-DD"        # Date of last manifest change
department: string            # email | calendar | operations | security | communications | crm | briefings

# Hierarchy
reports_to: string            # Agent ID this reports to (usually "supervisor")
creates_tasks_for: [string]   # Agent IDs this creates tasks for
receives_tasks_from: [string] # Agent IDs that create tasks for this
escalates_to: string          # Agent ID for escalations (usually "supervisor")

# Runtime — maps to openclaw.json + jobs.json
model:
  primary: string             # Full model path (e.g., openrouter/moonshotai/kimi-k2.5)
  fallbacks: [string]         # Ordered fallback chain
  payload_alias: string       # Model alias used in jobs.json payload (e.g., "kimi")

schedule:
  cron: string                # Cron expression (e.g., "0 6-22/2 * * *")
  timezone: string            # IANA timezone (e.g., America/Grenada)
  timeout_seconds: int        # Max execution time
  session_target: string      # "isolated" (fresh each run) or "persistent"
  stagger_ms: int             # Optional stagger delay (e.g., 300000 for vision-monitor)

delivery:
  mode: string                # "announce" (delivers output) or "none" (silent)
  channel: string             # "telegram" (only when mode=announce)
  to: string                  # Telegram chat ID (only when mode=announce)

# Permissions — maps to agent_capabilities.json + openclaw.json deny list
tools_allowed: [string]       # Tools in RBAC allow list
tools_denied: [string]        # Tools in openclaw.json deny list
bridge_endpoints: [string]    # Bridge HTTP endpoints (e.g., "GET /health", "POST /api/tasks")
streams:
  read: [string]              # Redis streams this agent can read
  write: [string]             # Redis streams this agent can write

# Coordination
task_protocol: bool           # Must follow task protocol (list_my_tasks → process → resolve)
review_workflow: bool         # Sends tasks to REVIEW status for supervisor approval
notification_inbox: bool      # Checks get_inbox at start of run
status_file: string           # Path to status file (e.g., brain/memory/email-classifier-status.md)
shared_working_state: bool    # Appends to shared_working_state block at end of run

# Files
instruction_file: string      # Path to .md bootstrap file (null if payload-only)
bootstrap_files: [string]     # Shared bootstrap files (typically AGENTS.md + TOOLS.md)

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

## 3. Fleet Registry

### 3.1 Active Fleet

| ID | Dept | Runtime Model | Schedule | Delivery | Instruction File |
|----|------|---------------|----------|----------|-----------------|
| email-classifier | email | Kimi K2.5 | `0 6-22/2 * * *` | announce | EMAIL_CLASSIFIER.md |
| email-analyst | email | Kimi K2.5 | `30 8-20/2 * * *` | announce | EMAIL_ANALYST.md |
| email-responder | email | Sonnet 4.6 | `0 8-20/4 * * *` | announce | RESPONDER.md |
| calendar-monitor | calendar | Kimi K2.5 | `0 6-22/2 * * *` | announce | CALENDAR_MONITOR.md |
| supervisor | operations | Kimi K2.5 | `0 6-22/2 * * *` | announce | HEARTBEAT.md |
| vision-monitor | security | Kimi K2.5 | `0 * * * *` | none | *(payload-only)* |
| conversation-inbox | communications | Kimi K2.5 | `0 6-22 * * *` | none | *(payload-only)* |
| conversation-resolver | communications | Kimi K2.5 | `0 8,14,20 * * *` | none | CONVERSATION_RESOLVER.md |
| crm-steward | crm | Kimi K2.5 | `0 10 * * *` | announce | CRM_STEWARD.md |
| morning-briefing | briefings | Kimi K2.5 | `30 6 * * *` | announce | *(payload-only)* |
| evening-winddown | briefings | Kimi K2.5 | `0 21 * * *` | announce | *(payload-only)* |

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
  :00 Classifier  :30 Analyst  :45 Responder (every 2-4h)
```

### 3.5 Config Location Map

| Agent ID | openclaw.json path | jobs.json job ID | capabilities key | Instruction file |
|----------|-------------------|-----------------|-----------------|-----------------|
| email-classifier | `agents.list[2]` | `email-classifier-0001` | `email-classifier` | `brain/EMAIL_CLASSIFIER.md` |
| email-analyst | `agents.list[3]` | `email-analyst-0001` | `email-analyst` | `brain/EMAIL_ANALYST.md` |
| email-responder | `agents.list[4]` | `email-responder-0001` | `email-responder` | `brain/RESPONDER.md` |
| calendar-monitor | `agents.list[5]` | `calendar-monitor-0001` | `calendar-monitor` | `brain/CALENDAR_MONITOR.md` |
| supervisor | `agents.list[1]` | `b7e3f1a0-supervisor-heartbeat-0001` | `supervisor` | `brain/HEARTBEAT.md` |
| vision-monitor | `agents.list[6]` | `vision-monitor-0001-000000000001` | `vision-monitor` | *(none)* |
| conversation-inbox | `agents.list[7]` | `conversation-inbox-monitor-0001` | `conversation-inbox` | *(none)* |
| conversation-resolver | `agents.list[8]` | `conversation-resolver-0001` | `conversation-resolver` | `brain/CONVERSATION_RESOLVER.md` |
| crm-steward | `agents.list[9]` | `crm-steward-0001` | `crm-steward` | `brain/CRM_STEWARD.md` |
| morning-briefing | `agents.list[10]` | `282b1c2f-664e-4efa-bcfa-fdab3febb829` | `morning-briefing` | *(none)* |
| evening-winddown | `agents.list[11]` | `88db3403-15ca-4edd-abd4-3add4b55dca0` | `evening-winddown` | *(none)* |

---

## 4. Procedures

### 4.1 Adding a New Agent

1. Create `docs/agents/<id>.yaml` manifest (copy existing, fill fields)
2. Add entry to `runtime/openclaw.json` `agents.list[]` (model, deny list)
3. Add job to `runtime/cron/jobs.json` `jobs[]` (schedule, payload, delivery)
4. Add RBAC entry to `brain/agent_capabilities.json`
5. Create `brain/<INSTRUCTION>.md` (or payload-only if simple)
6. `python scripts/validate_agents.py --agent <id>`
7. `python scripts/sync_runtime.py`
8. `sudo systemctl restart robothor-gateway`
9. Monitor first cron run via `jobs.json` state
10. Commit all files: `agent(<id>): add new agent`

### 4.2 Modifying an Agent

Edit manifest FIRST, then update configs to match.

| Change type | Files to update | Validation |
|---|---|---|
| Model change | manifest, openclaw.json, jobs.json payload alias | `validate --agent <id>` |
| Schedule change | manifest, jobs.json schedule | `validate --agent <id>` |
| Add/remove tool | manifest, agent_capabilities.json, possibly openclaw.json deny | `validate --agent <id>` |
| Behavior change | manifest changelog, jobs.json payload, brain/*.md | manual test |
| Delivery change | manifest, jobs.json delivery | `validate --agent <id>` |

### 4.3 Rolling Back an Agent

**Surgical (per-agent):**

1. `git log --oneline -- docs/agents/<id>.yaml`
2. `git checkout <commit> -- docs/agents/<id>.yaml`
3. AI reads restored manifest, updates configs to match
4. `python scripts/validate_agents.py --agent <id>`
5. `python scripts/sync_runtime.py && sudo systemctl restart robothor-gateway`

**Nuclear (all agents):**

1. `git log --oneline -- runtime/`
2. `git checkout <commit> -- runtime/ brain/agent_capabilities.json`
3. `python scripts/sync_runtime.py --restart`

### 4.4 Decommissioning an Agent

1. Set `jobs.json` state `enabled: false`
2. Wait for in-flight tasks to complete (`list_tasks`)
3. Remove from: openclaw.json, jobs.json, agent_capabilities.json
4. Archive instruction .md file (move to `docs/agents/archived/`)
5. Remove status file reference from HEARTBEAT.md
6. Validate, sync, commit

### 4.5 Changing a Shared Policy

1. Edit PLAYBOOK.md (this file) — e.g., change SLA thresholds
2. Identify affected agents (which manifests reference the changed policy)
3. Update affected manifests
4. Update configs to match manifests
5. Validate all affected agents
6. Commit: `policy: <what changed>`

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
| Commit: runtime infra | `infra(runtime): <what changed>` |
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
| Model selection | Sonnet for quality-critical (responder). Kimi K2.5 for all others. |
| Fallback chain | primary → fallback[0] → fallback[1] (typically kimi → sonnet/minimax → gemini) |
| Output limit | <500 chars, emoji prefix, `HEARTBEAT_OK` if nothing to report |
| Credentials | NEVER hardcode. Env vars via cron-wrapper sourcing `/run/robothor/secrets.env`. |
| Status files | MANDATORY every run. Supervisor considers >35min = stale. |
| Bootstrap budget | `bootstrapMaxChars=12000` per file, `bootstrapTotalMaxChars=30000` total |
| Reasoning | `reasoningDefault: "off"` in agents.defaults. Keep `reasoning: true` on Claude models. |

---

## 7. Debugging Guide

### Diagnostic Steps

| Step | Check | How |
|------|-------|-----|
| 1 | Is it running? | `jobs.json` → `state.lastStatus`, `state.consecutiveErrors` |
| 2 | Is it timing out? | `state.lastDurationMs` vs `payload.timeoutSeconds` |
| 3 | Getting 403s? | Check `agent_capabilities.json` RBAC + `openclaw.json` deny list |
| 4 | Producing output? | Check delivery config (`announce` vs `none`) |
| 5 | Routing correctly? | `list_tasks`, check tags and `assignedToAgent` |

### Common Failures

| Failure | Root Cause | Fix |
|---------|-----------|-----|
| Corrupted session | Orphaned tool_result without matching tool_use | Delete `~/.openclaw/agents/<id>/sessions/*.jsonl`, clear sessions.json entry, restart gateway |
| Stale status file | Agent errored mid-run, didn't reach status write step | Check `jobs.json` `state.lastStatus` for the truth |
| UUID parse error | LLM passed non-UUID to personId/companyId | Bridge returns 422 — agent should retry with valid UUID |
| Model reasoning leak | `reasoningDefault` not set to "off" | Check `agents.defaults.reasoningDefault` in openclaw.json |
| Gateway deny blocking RBAC | Tool in deny list never reaches Bridge | Remove from `tools.deny` in openclaw.json |
| 500 on task creation | Invalid field types (tags as string not array, etc.) | Check Bridge logs: `journalctl -u robothor-bridge --since "5 min ago"` |

### Live State Checks

```bash
# Cron job status
cat ~/.openclaw/cron/jobs.json | python3 -c "import json,sys; [print(f'{j[\"agentId\"]:25s} {j[\"state\"][\"lastStatus\"]:6s} {j[\"state\"][\"consecutiveErrors\"]}err {j[\"state\"].get(\"lastDurationMs\",0)//1000}s') for j in json.load(sys.stdin)['jobs']]"

# Bridge health
curl -s http://localhost:9100/health | python3 -m json.tool

# Gateway logs
journalctl -u robothor-gateway --since "5 min ago" --no-pager | tail -20

# Agent tasks
curl -s http://localhost:9100/api/tasks/agent/<agent-id> | python3 -m json.tool
```

---

Updated: 2026-02-24
