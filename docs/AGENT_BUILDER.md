# Robothor — Agent Builder Reference

You are a Claude Code session that knows how to build agents for the Robothor system. This file teaches the **unit agent + workflow** paradigm: focused agents composed into pipelines.

---

## 1. Unit Agent Philosophy

Every agent is a **focused unit** — one config (YAML manifest) + one prompt (instruction Markdown).

Units perform a **singular task**: classify, analyze, respond, monitor, check links, generate PRs. Not "handle emails" — that's a pipeline of 3 units (classify → analyze → respond).

Units are composed into workflows, not run in isolation. The system's power comes from composition:

- A **unit** is simple enough to test, debug, and reason about in isolation
- A **pipeline** chains units via CRM tasks, event hooks, or workflow YAML
- The **main agent** can dynamically spawn units as sub-programs at runtime

The CRM is the central coordination layer. Tasks are the inter-unit message bus. Memory blocks are shared state. Status files are peer awareness.

---

## 2. The Two Layers

### Unit Layer — Focused Workers

Each unit has precise tool access, runs with `delivery: none` (silent), and is triggered by workflows or events.

| Unit | Does exactly one thing |
|------|----------------------|
| `email-classifier` | Reads inbox, classifies emails, creates tasks for downstream units |
| `email-analyst` | Analyzes complex emails flagged by classifier, creates response tasks |
| `email-responder` | Drafts and sends replies for tasks in its queue |
| `link-checker` | Validates URLs, flags broken links |
| `vision-monitor` | Detects people via camera, creates alerts |
| `failure-analyzer` | Queries agent run failures, classifies root causes |
| `overnight-pr` | Implements fixes as draft PRs from tasks created by analyzers |

### Supervisor Layer — Main Agent

The main agent handles interactive requests and delegates complex work:

- **Simple requests** → handle directly (no spawning)
- **Moderate complexity** → `spawn_agent` one focused unit
- **Complex tasks** → `spawn_agents` multiple units in parallel, synthesize results

The main agent has `v2.can_spawn_agents: true` and access to all tools. Units have narrow tool access and cannot spawn sub-agents (unless explicitly configured).

---

## 3. Orchestration Patterns

### Pattern A: Event-Driven Pipeline

Python cron script publishes to Redis → hook triggers unit → unit creates CRM task → next unit picks it up.

```
email_sync.py (*/10 cron)
  → publishes "email.new" to Redis Stream
    → hook fires email-classifier
      → classifier creates task(assignedToAgent="email-responder", tags=["reply-needed"])
        → responder picks up task on next run
```

This is the **primary** trigger mechanism. Crons on individual agents are safety nets at relaxed frequencies (every 2-6h).

### Pattern B: Workflow Chain

YAML workflow defines: trigger → step → condition → step. The workflow engine runs steps sequentially with conditional branching.

```yaml
# docs/workflows/nightwatch.yaml
id: nightwatch
name: Nightwatch Pipeline
triggers:
  - type: cron
    cron: "0 2 * * *"
    timezone: America/New_York
timeout_seconds: 1800

steps:
  - id: analyze
    type: agent
    agent: improvement-analyst
    on_failure: abort

  - id: check_tasks
    type: condition
    condition: "has_tasks(assignedToAgent='overnight-pr', status='TODO')"
    if_true: create_prs
    if_false: done

  - id: create_prs
    type: agent
    agent: overnight-pr
    on_failure: skip

  - id: done
    type: noop
```

### Pattern C: Dynamic Sub-Agent Dispatch

The main agent spawns units at runtime based on the request:

```
User: "Research competitor pricing and summarize"
Main agent:
  → spawn_agents(["web-researcher", "price-analyzer"])
  → receives structured results from both
  → synthesizes into a single response
```

Sub-agents inherit budget constraints from the parent. Delivery is forced to `none` on children.

### Pattern D: Cron Safety Net

Python crons fetch data and publish events. Unit agents process the data. Crons are NOT the primary trigger — they catch anything the event hooks missed.

```yaml
# Agent runs every 6h as a safety net — primary trigger is the hook
hooks:
  - stream: email
    event_type: email.new
    message: "New email received. Check triage inbox and classify."
schedule:
  cron: "0 6-22/6 * * *"    # safety net every 6h
```

---

## 4. Building a Unit Agent

### Step 1: Scaffold

```bash
robothor agent scaffold <agent-id> --description "One-line purpose"
```

This creates both files (manifest YAML + instruction Markdown) with the correct structure.

### Step 2: Edit the Manifest

Required fields:

```yaml
id: my-agent                    # kebab-case, unique
name: My Agent                  # human-readable
description: One-line purpose   # what this agent does
version: "2026-03-04"           # YYYY-MM-DD of last change
department: custom              # email|calendar|operations|security|communications|crm|briefings|core|custom
```

Schedule and session:

```yaml
schedule:
  cron: "0 6-22/4 * * *"       # APScheduler cron expression
  timezone: America/New_York
  timeout_seconds: 300          # max run time
  max_iterations: 10            # max LLM round-trips
  session_target: isolated      # isolated (fresh each run) or persistent (keeps history)
```

Delivery — units are silent:

```yaml
delivery:
  mode: none                    # ALWAYS none for unit agents
```

**Only 3 agents talk to the user:** main (via heartbeat), morning briefing, evening wind-down. All other agents use `delivery: none`.

Coordination — who this unit connects to:

```yaml
reports_to: main
escalates_to: main
creates_tasks_for: [email-responder]
receives_tasks_from: [email-classifier]
task_protocol: true
```

Event hooks — primary triggers:

```yaml
hooks:
  - stream: email
    event_type: email.new
    message: "New email received. Check triage inbox and classify."
```

Warmup — pre-loaded context:

```yaml
warmup:
  memory_blocks: [operational_findings, contacts_summary]
  context_files:
    - brain/memory/my-agent-status.md
  peer_agents: [related-agent]
```

Status file — written at end of every run:

```yaml
status_file: brain/memory/my-agent-status.md
```

v2 engine features (all optional):

```yaml
v2:
  error_feedback: true          # inject error analysis when tools fail (default ON)
  cost_budget_usd: 0.50         # max cost per run
  planning_enabled: false       # pre-execution planning phase
  guardrails: []                # no_destructive_writes, no_external_http, no_main_branch_push
  can_spawn_agents: false       # allow spawning sub-agents
```

### Step 3: Write the Instruction File

Every instruction file follows this contract:

```markdown
# Agent Name

You are **Agent Name**, an autonomous agent in the {{ai_name}} system.

## Your Role

2-3 sentences. What you DO and what you DON'T do. Be specific about boundaries.

## Tasks

Numbered list — what to do each run:
1. What inputs to read (task inbox, files, memory blocks)
2. What processing to perform
3. What outputs to produce (status file, tasks, notifications)

## Output

Write to `brain/memory/<agent-id>-status.md`:
- One-line summary + ISO 8601 timestamp
- Example: "Processed 3 emails, created 2 tasks. — 2026-03-04T14:00:00Z"
```

Add conditional sections when the manifest enables them:

| If manifest has... | Add section... |
|---------------------|----------------|
| `task_protocol: true` | **Task Protocol** — `list_my_tasks()` → set IN_PROGRESS → process → `resolve_task()` |
| `review_workflow: true` | **Review Workflow** — set tasks to REVIEW (not DONE) when approval needed |
| `hooks:` (event-driven) | **Trigger Context** — explain what event data is available |
| `v2.can_spawn_agents: true` | **Sub-agents** — when/how to spawn helpers |

**Anti-patterns to avoid:**
- No localhost URLs (engine's `web_fetch` blocks loopback)
- No hardcoded chat IDs (use delivery config)
- No file paths outside workspace (use `brain/` relative paths)

### Step 4: Validate and Deploy

```bash
python scripts/validate_agents.py --agent <agent-id>
sudo systemctl restart robothor-engine
robothor engine run <agent-id>   # test manually first
```

---

## 5. Model Tiering Strategy

Models change frequently. Don't hardcode model names in templates — use **tier variables** from `_defaults.yaml` that resolve at install time.

| Tier | Use Case | Selection Criteria | Config Variable |
|------|----------|-------------------|-----------------|
| **T0: Router** | Classification, triage, simple extraction | Cheapest with reliable tool-calling | `{{ model_primary }}` |
| **T1: Worker** | Standard tool use, CRM writes, file ops | Good cost/quality balance, fast | `{{ model_primary }}` |
| **T2: Reasoning** | Analysis, composition, complex decisions | High quality, willing to pay more | `{{ model_quality }}` |
| **T3: Orchestrator** | Multi-agent coordination, synthesis, code generation | Best available for planning | `{{ model_quality }}` or override |

Current defaults (from `_defaults.yaml`):

```yaml
model_primary: "openrouter/moonshotai/kimi-k2.5"     # T0/T1
model_quality: "openrouter/anthropic/claude-sonnet-4.6"  # T2/T3
model_fallbacks:
  - "gemini/gemini-2.5-pro"
```

**Guidance:**
- Pick the cheapest tier that can do the job
- Track cost-per-run with `get_agent_stats()` — promote to a higher tier only when quality metrics (error rate, escalation rate) demand it
- Model names live in `_defaults.yaml` — update once there, all agents get the new model at next install/update
- Fallback chains are mandatory — models break, get deprecated, or change pricing

In manifest templates, use variables:

```yaml
model:
  primary: {{ model_primary }}
  fallbacks: {{ model_fallbacks }}
```

In concrete manifests (non-template), use the actual model string directly.

---

## 6. CRM-Centric Coordination

The CRM is how units talk to each other. No direct agent-to-agent calls outside of `spawn_agent`.

### Tasks as Inter-Unit Messages

Agent A creates a task for Agent B:
```
create_task(title="Analyze email from CEO", assignedToAgent="email-analyst", tags=["email", "analytical"])
```

Agent B picks it up:
```
list_my_tasks(assignedToAgent="email-analyst", status="TODO")
→ update_task(id, status="IN_PROGRESS")
→ ... work ...
→ resolve_task(id, resolution="Analysis complete: 3 action items identified")
```

**Tag vocabulary** for routing: `email`, `reply-needed`, `analytical`, `escalation`, `needs-philip`, `calendar`, `conflict`, `cancellation`, `vision`, `unknown-person`, `crm-hygiene`, `dedup`, `enrichment`, `nightwatch`, `self-improve`.

### Memory Blocks for Shared State

Persistent key-value blocks that multiple agents can read/write:

- `operational_findings` — cross-agent observations
- `contacts_summary` — CRM contact intelligence
- `performance_baselines` — agent performance metrics
- `nightwatch_log` — overnight improvement tracking
- `concierge_observations` — usage pattern analysis

### Status Files for Peer Awareness

Every agent writes a one-line status file at the end of each run:

```
Checked N URLs across M tasks. K broken. — 2026-03-04T14:00:00Z
```

The heartbeat checks for staleness. Peer agents read each other's status files via warmup context.

---

## 7. Tool Access Design

Each unit gets ONLY the tools it needs. The engine's `build_for_agent()` method strictly filters — if `tools_allowed` is set, ONLY those tools are available.

**CRITICAL:** If your agent does ANY file I/O, reads status files, or runs CLI commands, you MUST include `exec`, `read_file`, and `write_file`. Without these, agents silently fail — they hallucinate tool calls that don't exist.

### Tool Categories

| Category | Tools |
|----------|-------|
| **File I/O** | `exec`, `read_file`, `write_file`, `list_directory` |
| **Memory** | `search_memory`, `store_memory`, `get_entity`, `memory_block_read`, `memory_block_write`, `append_to_block`, `memory_block_list` |
| **CRM** | `create_task`, `update_task`, `get_task`, `list_tasks`, `list_my_tasks`, `resolve_task`, `create_note`, `list_notes`, `update_note`, `create_person`, `list_people`, `get_person`, `update_person`, `create_company`, `list_companies` |
| **Web** | `web_fetch`, `web_search` |
| **Communication** | `log_interaction`, `list_conversations`, `get_conversation`, `list_messages`, `create_message` |
| **Vision** | `look`, `who_is_here`, `enroll_face`, `set_vision_mode` |
| **Voice** | `make_call` |
| **Engine** | `list_agent_runs`, `get_agent_stats`, `get_fleet_health`, `detect_anomalies` |
| **Git** | `git_status`, `git_diff`, `git_branch`, `git_commit`, `git_push`, `create_pull_request` |
| **Sub-agents** | `spawn_agent`, `spawn_agents` |
| **Vault** | `vault_get`, `vault_set`, `vault_list` |

### Common Tool Profiles

**Read-only unit** (monitors, analyzers):
```yaml
tools_allowed: [read_file, search_memory, get_entity, memory_block_read, web_fetch, list_tasks, list_my_tasks]
```

**CRM worker** (task processors):
```yaml
tools_allowed: [exec, read_file, write_file, search_memory, store_memory, list_my_tasks, update_task, resolve_task, create_task]
```

**Action unit** (responders, callers):
```yaml
tools_allowed: [exec, read_file, write_file, web_fetch, create_message, log_interaction, list_my_tasks, update_task, resolve_task]
```

---

## 8. Template Packaging

When sharing agents as installable templates, each agent is a **5-file bundle**:

```
templates/agents/<department>/<agent-id>/
├── setup.yaml                    # Installation metadata + variable declarations
├── manifest.template.yaml        # Agent manifest with {{ variable }} placeholders
├── instructions.template.md      # Instruction file with {{ ai_name }} etc.
├── SKILL.md                      # Human-readable skill card (Agent Skills Standard)
└── programmatic.json             # Machine-readable metadata for web discovery
```

### setup.yaml — Installation Metadata

```yaml
agent_id: email-classifier
version: "2026-03-04"
instruction_file_path: brain/EMAIL_CLASSIFIER.md
variables:
  model_primary:
    type: string
    default: "openrouter/moonshotai/kimi-k2.5"
    description: "Primary LLM model"
  cron_expr:
    type: string
    default: "0 6-22/2 * * *"
    description: "Cron schedule"
    prompt: "How often should this agent run?"
```

### Variable Resolution

Variables resolve through a 5-layer priority chain (last wins):

1. `_defaults.yaml` — global fallbacks (`model_primary`, `model_quality`, `timezone`, etc.)
2. `setup.yaml` defaults — per-template values
3. `.robothor/config.yaml` — instance-wide defaults
4. `.robothor/overrides/<id>.yaml` — per-agent customization
5. CLI `--set key=value` — highest priority

**Install-time variables** use `{{ }}`: `{{ model_primary }}`, `{{ cron_expr }}`
**Runtime variables** use `${}`: `${ROBOTHOR_TELEGRAM_CHAT_ID}` — left unresolved for the engine

### SKILL.md — Agent Skills Standard

Human-readable card compatible with Claude Code, Codex, VS Code, and Copilot:

```markdown
---
name: Email Classifier
version: "2026-03-04"
description: Classifies incoming emails and routes to appropriate handlers
format: robothor-native/v1
department: email
---

# Email Classifier

Monitors the email inbox and classifies incoming messages...
```

### programmatic.json — Machine Discovery

```json
{
  "name": "Email Classifier",
  "id": "email-classifier",
  "version": "2026-03-04",
  "format": "robothor-native/v1",
  "department": "email",
  "description": "Classifies incoming emails and routes to handlers",
  "tags": ["email", "reply-needed", "analytical"]
}
```

### Installation

```bash
robothor agent install email-classifier              # from catalog
robothor agent install --preset standard              # install a preset group
robothor agent install ./path/to/bundle/              # from local directory
```

---

## 9. Complete Example: Email Pipeline

A 3-unit pipeline: **classifier** → **analyst** → **responder**, connected via CRM tasks and event hooks.

### Unit 1: Email Classifier

**Manifest** (`docs/agents/email-classifier.yaml`):

```yaml
id: email-classifier
name: Email Classifier
description: Triage incoming emails and route to appropriate handlers
version: "2026-03-04"
department: email

reports_to: main
escalates_to: main
creates_tasks_for: [email-analyst, email-responder]

model:
  primary: openrouter/moonshotai/kimi-k2.5     # T0: router — cheap, fast
  fallbacks:
    - openrouter/minimax/minimax-m2.5
    - gemini/gemini-2.5-pro

schedule:
  cron: "0 6-22/2 * * *"       # safety net every 2h
  timezone: America/New_York
  timeout_seconds: 300
  max_iterations: 10
  session_target: isolated

delivery:
  mode: none

hooks:
  - stream: email
    event_type: email.new
    message: "New email received. Check triage inbox and classify."

tools_allowed:
  - exec
  - read_file
  - write_file
  - search_memory
  - get_entity
  - store_memory
  - list_conversations
  - create_person
  - list_people
  - get_person
  - list_tasks
  - create_task
  - list_my_tasks
  - update_task
  - resolve_task

task_protocol: true
status_file: brain/memory/email-classifier-status.md
tags_produced: [email, reply-needed, analytical, escalation, needs-philip]

warmup:
  memory_blocks: [operational_findings, contacts_summary]
  context_files:
    - brain/memory/email-classifier-status.md
    - brain/memory/triage-inbox.json

v2:
  error_feedback: true
```

**Instruction** (`brain/EMAIL_CLASSIFIER.md`) — excerpt:

```markdown
# Email Classifier

You are **Email Classifier**, an autonomous agent in the {{ai_name}} system.

## Your Role

Read the email triage inbox and classify each message. Route to the right handler
via CRM tasks. You do NOT reply to emails — you create tasks for downstream units.

## Tasks

1. Read `brain/memory/triage-inbox.json` for unprocessed emails.
2. For each email, classify: reply-needed, analytical, escalation, or noise.
3. Create a CRM task for the appropriate handler:
   - `reply-needed` → assignedToAgent="email-responder"
   - `analytical` → assignedToAgent="email-analyst"
   - `escalation` / `needs-philip` → assignedToAgent="main"
4. Write status file.
```

### Unit 2: Email Analyst

**Manifest** (`docs/agents/email-analyst.yaml`):

```yaml
id: email-analyst
name: Email Analyst
description: Deep analysis of complex emails requiring research or context
version: "2026-03-04"
department: email

reports_to: main
escalates_to: main
receives_tasks_from: [email-classifier]
creates_tasks_for: [email-responder]

model:
  primary: openrouter/moonshotai/kimi-k2.5     # T1: worker
  fallbacks:
    - openrouter/minimax/minimax-m2.5

schedule:
  cron: "30 8-20/6 * * *"      # safety net every 6h
  timezone: America/New_York
  timeout_seconds: 300
  max_iterations: 10
  session_target: isolated

delivery:
  mode: none

tools_allowed:
  - exec
  - read_file
  - write_file
  - search_memory
  - store_memory
  - web_fetch
  - web_search
  - list_my_tasks
  - update_task
  - resolve_task
  - create_task

task_protocol: true
notification_inbox: true
status_file: brain/memory/email-analyst-status.md
tags_consumed: [email, analytical]

v2:
  error_feedback: true
```

### Unit 3: Email Responder

**Manifest** (`docs/agents/email-responder.yaml`):

```yaml
id: email-responder
name: Email Responder
description: Draft and send email replies for tasks in queue
version: "2026-03-04"
department: email

reports_to: main
escalates_to: main
receives_tasks_from: [email-classifier, email-analyst]

model:
  primary: openrouter/anthropic/claude-sonnet-4.6  # T2: reasoning — quality-critical
  fallbacks:
    - openrouter/moonshotai/kimi-k2.5
    - gemini/gemini-2.5-pro

schedule:
  cron: "0 8-20/2 * * *"
  timezone: America/New_York
  timeout_seconds: 600
  max_iterations: 15
  session_target: isolated

delivery:
  mode: none

tools_allowed:
  - exec
  - read_file
  - write_file
  - search_memory
  - store_memory
  - get_entity
  - web_fetch
  - list_my_tasks
  - update_task
  - resolve_task
  - create_task
  - log_interaction
  - list_conversations
  - get_conversation
  - list_messages
  - create_message

task_protocol: true
review_workflow: true
notification_inbox: true
status_file: brain/memory/email-responder-status.md
tags_consumed: [email, reply-needed]

warmup:
  memory_blocks: [operational_findings]

v2:
  error_feedback: true
```

### The Workflow (Event-Driven)

No workflow YAML needed for event-driven pipelines. The connection is implicit:

1. `email_sync.py` (cron `*/10`) fetches new emails → publishes `email.new` to Redis
2. Hook fires `email-classifier` → classifier reads inbox, creates tasks with tags
3. `email-analyst` picks up `analytical` tasks on its next run (hook or cron safety net)
4. `email-responder` picks up `reply-needed` tasks on its next run
5. Each unit resolves its tasks and writes its status file

For sequential dependencies (analyst MUST run before responder), use a workflow YAML like the nightwatch example in Section 3.

---

## Validation Checklist

Before deploying any unit agent:

- [ ] `python scripts/validate_agents.py --agent <id>` passes
- [ ] `tools_allowed` includes `exec`, `read_file`, `write_file` if agent does file I/O
- [ ] `delivery.mode` is `none` for all unit agents
- [ ] Instruction file exists at the path specified in the manifest
- [ ] Status file path is under `brain/memory/`
- [ ] If `task_protocol: true`, instruction file mentions `list_my_tasks`, `IN_PROGRESS`, `resolve_task`
- [ ] Model tier matches the unit's complexity (don't use T2 for classification)
- [ ] Version date is today's date
- [ ] Department matches the agent's function
