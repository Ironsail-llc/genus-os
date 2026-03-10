<p align="center">
  <img src="docs/images/genus-os-logo.png" width="400" alt="Genus OS">
</p>

<h1 align="center">Genus OS</h1>
<p align="center"><b>The enterprise AI operating system you deploy on your own infrastructure.</b></p>

<p align="center">
Define agents in YAML. Wire them into governed pipelines with audit trails and guardrails.
Federate across sites, teams, and subsidiaries with cryptographic trust and scoped permissions.
<br><br>
Enterprise security. Enterprise scalability. Enterprise governance.<br>
Your infrastructure. Your data. Your rules.
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/tests-2%2C000%2B%20passing-brightgreen.svg" alt="Tests">
</p>

---

## Why Genus OS

Most AI platforms force a choice: cloud-hosted convenience or self-hosted complexity. Genus OS eliminates that trade-off. Deploy on your infrastructure — on-prem, private cloud, or air-gapped — and get an enterprise-grade AI operating system with the security, governance, and scalability your organization demands.

| Enterprise Need | How Genus OS Delivers |
|---|---|
| **Data sovereignty** | Everything runs on your infrastructure. No data leaves your network. No third-party API required (local LLMs via Ollama). |
| **Security** | SOPS + age encrypted secrets, per-agent tool allow/deny lists, 7 guardrail policies, secret scanning pre-commit hooks, RBAC on event streams, Ed25519 cryptographic identity for federation. |
| **Governance** | Full audit trails on every agent run, tool call, and decision. Task state machines with SLA tracking. Review workflows with human-in-the-loop approval. OTel-compatible distributed tracing. |
| **Scalability** | Federate across sites, teams, and subsidiaries. Each instance is autonomous; federation adds scoped connectivity without single points of failure. |
| **Multi-tenancy** | Every CRM table scoped by `tenant_id`. Bridge middleware enforces tenant isolation. Separate agent fleets per business unit. |
| **Compliance** | Air-gap compatible. No internet dependency for core operations. Secrets never touch disk unencrypted. Systemd services with watchdog health checks. |

## Highlights

**Governed Agent Platform** — 17 agents defined by declarative YAML manifests with full audit trails. A workflow engine with conditional branching. 106 registered tools with per-agent allow/deny lists — agents see only what they're authorized to use. 7 guardrail policies enforce security at runtime: destructive write prevention, external HTTP blocking, branch protection, rate limiting, secret scanning, exec allowlists, and write path restrictions. OTel-compatible tracing captures every decision for compliance review.

**Enterprise Federation** — Connect Genus OS instances across sites, subsidiaries, and partners into a peer-to-peer mesh. Ed25519 signed invite tokens establish cryptographic trust. Each connection has scoped exports/imports — no implicit access, no transitive trust. Three-channel sync (critical/bulk/media) with Hybrid Logical Clocks for causal ordering across distributed instances. NATS JetStream transport with leaf-node topology handles unreliable networks gracefully. Every instance runs autonomously; federation adds connectivity, not dependency.

**The Helm (Control Plane)** — Not a dashboard — an enterprise control plane. Next.js 16 + Dockview with 45 lazy-loaded components. Chat with agents, manage tasks on a Kanban board, watch event streams in real time, monitor fleet health across federated instances. Fully extensible component registry for custom operational views.

**Intelligence Layer** — Two-tier memory: working context and long-term facts with hybrid search (HNSW vectors + BM25 keyword matching, fused by Reciprocal Rank Fusion). Knowledge graph that grows autonomously. Fully local RAG stack — embeddings, reranking, and generation never leave your network. Facts carry confidence scores, categories, and lifecycle states with quality gates that prevent knowledge degradation.

**Physical Security** — YOLOv8 nano + InsightFace ArcFace for real-time object detection and face recognition. Three runtime modes: disarmed, basic (motion-triggered smart detection), armed (per-frame tracking). Any RTSP camera. Sub-2-second unknown person alerts. Scene analysis via vision LLM. All processing local — no cloud, no external API calls, no video data leaving your premises.

**Operations & CRM** — Built-in CRM with cross-channel identity resolution and multi-tenancy. Task state machine (TODO &rarr; IN_PROGRESS &rarr; REVIEW &rarr; DONE) with SLA tracking, agent notifications, and human-in-the-loop approval workflows. Fleet analytics with anomaly detection. Nightwatch: overnight self-improving pipeline that diagnoses failures and opens draft PRs. sd_notify watchdog with DB/Redis health pings, zombie run reaping, and stale session cleanup. MCP server exposes 64 tools over stdio. Encrypted secrets (SOPS + age), systemd services, Cloudflare tunnel.

## Getting Started

1. **Clone and install:**
   ```bash
   git clone https://github.com/Ironsail-llc/genus-os.git
   cd genus-os
   python3 -m venv venv && source venv/bin/activate
   pip install -e ".[all]"
   ```

2. **Activate the onboarding guide:**
   ```bash
   cp docs/ONBOARDING.md CLAUDE.md
   ```

3. **Open Claude Code and ask:** "Help me get started"
   The guide walks through prerequisites, API keys, identity, agents, and first run.

4. When done, delete `CLAUDE.md` or replace with your own project instructions.

To build custom agents later:
```bash
cp docs/AGENT_BUILDER.md .claude/AGENT_BUILDER.md
```

## Quick Start

```bash
git clone https://github.com/Ironsail-llc/genus-os.git
cd genus-os
pip install -e ".[all]"
robothor init       # Interactive setup: DB, Redis, Ollama, migrations
robothor serve      # Start orchestrator + engine
```

Or with Docker for dependencies:

```bash
robothor init --docker   # PostgreSQL+pgvector, Redis, Ollama in containers
robothor serve
```

Engine and TUI commands:

```bash
robothor engine status   # Engine health, scheduler, bot status
robothor engine run <id> # Run any agent manually
robothor tui             # Terminal dashboard for monitoring
```

## Build Your Agents

Every agent is defined by a YAML manifest and an optional instruction file. Scaffold one, or drop a manifest in `docs/agents/` yourself.

```bash
robothor agent scaffold support-triage --description "Classify incoming support tickets"
```

This creates `docs/agents/support-triage.yaml` (manifest) and `brain/SUPPORT_TRIAGE.md` (instruction file) from templates. For a guided experience, use the Agent Builder wizard (`robothor agent build`) — it captures your intent, generates the manifest and instructions, and scaffolds an eval framework. Edit the result to fit your needs:

```yaml
# docs/agents/support-triage.yaml
id: support-triage
name: Support Triage
description: Classify incoming support tickets and route to the right team
version: "2026-03-01"
department: operations

model:
  primary: openrouter/moonshotai/kimi-k2.5
  fallbacks:
    - openrouter/anthropic/claude-sonnet-4.6
    - gemini/gemini-2.5-pro

schedule:
  cron: "*/30 8-20 * * 1-5"
  timezone: America/New_York
  timeout_seconds: 300
  max_iterations: 15
  session_target: isolated

delivery:
  mode: none              # Silent worker — no user-facing output

# Event hooks — primary trigger path (cron serves as safety net)
hooks:
  - stream: support
    event_type: ticket.new
    message: "New support ticket received. Classify and route."

tools_allowed:
  - exec
  - read_file
  - write_file
  - search_memory
  - create_task
  - list_tasks
  - resolve_task
tools_denied:
  - delete_task

task_protocol: true       # Must follow: list_my_tasks → process → resolve
status_file: brain/memory/support-triage-status.md
instruction_file: brain/SUPPORT_TRIAGE.md
bootstrap_files:
  - brain/AGENTS.md
  - brain/TOOLS.md

downstream_agents:
  - support-engineer
  - account-manager
tags_produced: [support, routing, escalation]
```

### Contracts

Agents are built against two strict contracts:

| Contract | File | Enforced by |
|----------|------|-------------|
| Manifest schema | `docs/agents/schema.yaml` | `validate_agents.py`, pre-commit hook, engine startup |
| Instruction format | `docs/agents/INSTRUCTION_CONTRACT.md` | Convention (AI-readable) |

Required manifest fields: `id` (kebab-case), `name`, `description`, `version` (YYYY-MM-DD), `department`.

### Manifest Fields

| Field | Purpose |
|-------|---------|
| `model.primary` / `fallbacks` | LLM with ordered fallback chain. Broken models auto-removed per run. |
| `schedule.cron` | APScheduler cron expression. Leave empty for hook-only agents. |
| `delivery.mode` | `announce` (delivers to user) or `none` (silent worker). |
| `tools_allowed` / `tools_denied` | Per-agent tool access. The engine strips tools not in the allow list before sending to the LLM. |
| `hooks` | Event triggers from Redis Streams. Primary fast path; cron as safety net. |
| `task_protocol` | Agent must check its inbox, process tasks, and resolve them. |
| `warmup.context_files` | Files pre-loaded into context before each run. |
| `streams.read` / `write` | Redis Streams the agent can subscribe to or publish on. |
| `instruction_file` | Markdown file loaded as the system prompt. |
| `bootstrap_files` | Shared context files appended after instructions. |
| `downstream_agents` | Agents this one creates tasks for. |
| `sla` | Response time targets by priority level. |
| `review_workflow` | If true, tasks go to REVIEW for supervisor approval. |

Full schema: [schema.yaml](docs/agents/schema.yaml) | Reference: [Agent Playbook](docs/agents/PLAYBOOK.md)

### Agent Lifecycle

```bash
robothor engine list           # See all scheduled agents
robothor engine run <id>       # Run one manually
robothor engine history        # Recent runs with status and duration
python scripts/validate_agents.py --agent <id>  # Validate manifest
```

The engine provides **101 tools** — CRM operations, memory search, file I/O, shell execution, web fetch, task coordination, git operations, voice calling, and more. Each agent sees only the tools in its `tools_allowed` list.

### Agent Engine v2

The engine's execution loop includes a full suite of runtime enhancements, all opt-in via the `v2:` manifest key:

- **Planning phase** — Generates an execution plan before acting, with dynamic replanning on new information
- **Working memory scratchpad** — Persistent scratch space across iterations for intermediate reasoning
- **Token and cost budgets** — Hard enforcement with graceful shutdown when limits are reached
- **Graduated escalation** — 3 consecutive errors → retry with feedback, 4 → checkpoint + replan, 5 → abort with diagnostics
- **Mid-run checkpoints** — Save and resume from any iteration via `POST /api/runs/{id}/resume`
- **Self-validation** — Post-execution verification step checks whether the agent's output satisfies the original goal
- **Difficulty-aware routing** — Routes simple tasks to smaller models (capped iterations), complex tasks to capable models

### Sub-Agents

Agents can spawn focused sub-tasks mid-run and receive structured results synchronously. Enable in the manifest:

```yaml
v2:
  can_spawn_agents: true
  max_nesting_depth: 3
  sub_agent_max_iterations: 10
  sub_agent_timeout_seconds: 120
```

Child agents inherit the parent's remaining budget (never exceed it), delivery is forced to `none`, and dedup keys are namespaced under the parent run. The `spawn_agents` tool runs multiple sub-agents concurrently (up to 3).

## Workflows

Multi-step pipelines defined in YAML. Triggered by events, backed by cron safety nets.

```yaml
# docs/workflows/support-pipeline.yaml
id: support-pipeline
name: Support Pipeline
description: Triage tickets, then route to engineer or account manager

triggers:
  - type: hook
    stream: support
    event_type: ticket.new
  - type: cron
    cron: "0 9-17/4 * * 1-5"

steps:
  - id: triage
    type: agent
    agent_id: support-triage
    message: "New ticket received. Classify and route."
    on_failure: abort

  - id: check_priority
    type: condition
    input: "{{ steps.triage.output_text }}"
    branches:
      - when: "'escalation' in value.lower()"
        goto: escalate
      - when: "'technical' in value.lower()"
        goto: engineer
      - otherwise: true
        goto: done

  - id: escalate
    type: agent
    agent_id: account-manager
    message: "High-priority ticket escalated. Review and respond."

  - id: engineer
    type: agent
    agent_id: support-engineer
    message: "Technical ticket assigned. Investigate and resolve."

  - id: done
    type: noop
```

**Event hooks** on Redis Streams are the primary trigger. Cron schedules serve as safety nets at relaxed frequencies. The workflow engine handles conditional branching, failure modes (`abort` / `skip`), and step chaining.

```bash
robothor engine workflow list      # List loaded workflows
robothor engine workflow run <id>  # Execute manually
```

## Nightwatch

A self-improving pipeline that runs overnight via Claude Code CLI in isolated git worktrees — no engine agent loop involved. Three specialized scripts:

1. **nightwatch-heal.py** (nightly, 3 AM) — Self-healing: detects failures, diagnoses root causes, and applies fixes in an isolated worktree. Opens draft PRs on feature branches.
2. **nightwatch-build.py** (Monday, 3 AM) — Feature builds: picks up approved improvement proposals and implements them end-to-end, including tests.
3. **nightwatch-research.py** (Sunday, 1 AM) — Competitive research: surveys the landscape, evaluates new tools and techniques, and writes structured reports.

All three run in isolated git worktrees with branch protection. Draft PRs are labeled `nightwatch` for easy filtering. A **Failure Analyzer** agent (every 2h) classifies recent failures and creates CRM tasks that feed into the heal pipeline.

## The Helm

Not a dashboard — a control plane. Built with Next.js 16 and Dockview for a paneled, IDE-like layout.

<p align="center">
  <img src="docs/images/helm-dashboard.png" width="800" alt="The Helm — control plane">
</p>

- **Chat** — Talk to agents through the Engine via SSE streaming
- **Task Board** — Kanban with drag-and-drop, approve/reject workflow
- **Event Streams** — Real-time feed from all Redis Streams
- **Agent Status** — Live health, run history, and error tracking
- **CRM Views** — Contacts, companies, conversations, notes
- **Service Health** — System topology with status indicators
- **Component Registry** — 45 lazy-loaded components, add your own

## The CRM

How agents coordinate. Native PostgreSQL tables — no external CRM dependency.

- **Task state machine** — TODO &rarr; IN_PROGRESS &rarr; REVIEW &rarr; DONE with full audit trail and SLA tracking
- **Agent notifications** — Typed messages between agents (task assigned, review requested, blocked, errors)
- **Cross-channel identity** — A single contact resolved across email, Telegram, voice, web, and API
- **Multi-tenancy** — Every table scoped by `tenant_id`. Bridge middleware enforces isolation.
- **Merge tools** — Deduplicate contacts and companies. Keeper absorbs loser's data, re-links all records.

## Memory

Two tiers of persistent memory, all local:

| Tier | Storage | Lifetime | Purpose |
|------|---------|----------|---------|
| Working | Context window + memory blocks | Session | Current conversation state, persona, user profile |
| Long-term | PostgreSQL + pgvector | Permanent | Importance-scored facts with hybrid search |

**Hybrid search:** HNSW vector index (m=16, ef=200) for semantic similarity, BM25 keyword matching via tsvector for exact terms, fused by Reciprocal Rank Fusion (`1/(60+rank)`). Top results pass through a cross-encoder reranker before delivery.

Facts are extracted from every input — email, calendar, conversations, vision events. Each fact carries a confidence score, category, entities, and lifecycle state. **Quality gates** reject vague or generic extractions. A knowledge graph of entities and relationships grows autonomously. **Lifecycle management** handles decay, consolidation (merging related facts), and forgetting (pruning low-quality facts that were never accessed).

```python
from robothor.memory.facts import store_fact, search_facts

# Store with confidence, category, and entity links
fact_id = await store_fact(
    fact={"fact_text": "Acme renewed for 2 years", "category": "deal",
          "confidence": 0.95, "entities": ["Acme Corp"]},
    source_content="email from sales",
    source_type="email",
)

# Hybrid search (vector + BM25 + reranker)
results = await search_facts("Acme contract status", limit=5)
```

**RAG stack:** Qwen3-Embedding &rarr; pgvector (HNSW) + BM25 &rarr; RRF &rarr; Qwen3-Reranker &rarr; LLM generation. Fully local.

## Vision

Always-on camera monitoring with runtime mode switching:

| Mode | Behavior |
|------|----------|
| Disarmed | Idle — no processing |
| Basic | Motion &rarr; YOLO &rarr; InsightFace &rarr; instant alerts + async VLM analysis |
| Armed | Per-frame tracking with full detection pipeline |

**Pipeline:** Motion detection &rarr; YOLOv8 nano (6 MB) &rarr; InsightFace ArcFace (300 MB) &rarr; pluggable alerts. Unknown persons trigger a snapshot to your chosen channel in under 2 seconds. Scene analysis via vision LLM (Ollama). Any RTSP camera source. Mode switch at runtime, no restart.

## Federation

Enterprise AI doesn't live on a single server. Genus OS federation connects autonomous instances across offices, data centers, subsidiaries, and partner organizations into a peer-to-peer mesh — without sacrificing data sovereignty or creating single points of failure.

### Use Cases

| Scenario | Topology |
|---|---|
| **Multi-site enterprise** | HQ hub with branch office leaf nodes. HQ pushes config and knowledge; branches report health, escalate tasks, and sync CRM data. |
| **Subsidiary governance** | Parent company connects to subsidiary instances as "parent." Scoped exports push compliance policies; scoped imports surface subsidiary health and alerts without exposing operational data. |
| **Partner integration** | Two organizations connect as "peers" with explicitly negotiated exports/imports. Share only what's agreed — no implicit access, no transitive trust. |
| **Dev/staging/production** | Federate staging instances to production for config sync and telemetry aggregation. Staging pushes test results; production pushes config templates. |
| **Disaster recovery** | Peer-federated instances in separate regions. Each runs autonomously; federation keeps knowledge graphs synchronized. If one site goes down, the other continues with no interruption. |

### How It Works

```bash
# On the parent instance:
robothor federation init              # Generate Ed25519 identity
robothor federation invite --relationship child --ttl 48
# → prints a one-time signed token

# On the new instance:
git clone https://github.com/Ironsail-llc/genus-os.git
cd genus-os && pip install -e ".[all]"
robothor init
robothor federation init
robothor federation connect <token>   # Establishes bilateral connection
robothor engine start
```

### Architecture

Each connection has a **relationship** (parent, child, or peer) that sets default capability templates, **exports/imports** for scoped data sharing, and a **state machine** (pending → active → limited/suspended).

| Relationship | Parent Exports | Child Exports |
|---|---|---|
| Parent ↔ Child | Memory search, CRM read, config push | Health, agent runs, sensor data, alerts, escalation |
| Peer ↔ Peer | Explicitly negotiated | Explicitly negotiated |

### Security Model

- **Ed25519 cryptographic identity** — every instance has a unique keypair; invite tokens are signed to prevent tampering
- **One-time tokens** — each invite generates a unique connection secret (SHA-256 hashed), single use
- **Scoped permissions** — exports and imports are explicit per connection; no blanket access
- **No transitive trust** — A↔B and B↔C does NOT mean A↔C; every link is bilateral and independently authorized
- **Private key isolation** — keys stored with `0600` permissions, never transmitted
- **NATS account isolation** — each connection gets its own subject namespace

### Sync Protocol

Three prioritized channels with Hybrid Logical Clocks for causal ordering:

| Channel | Contents | Priority |
|---|---|---|
| Critical | Tasks, config, memory facts, alerts | Sync first |
| Bulk | Agent runs, tool calls, telemetry | When bandwidth allows |
| Media | Images, audio, documents | Background |

Conflict resolution uses monotonic lattices (task states only move forward), additive merges (memory facts), and authoritative sources (config from exporting instance).

**Transport:** NATS with JetStream for reliable, store-and-forward messaging. Hub instances run a full NATS server; leaf instances connect via NATS leaf nodes. Designed for unreliable networks — JetStream buffers messages during disconnections and replays on reconnect.

Full architecture: [docs/FEDERATION.md](docs/FEDERATION.md)

## Enterprise Security

Security is not a feature — it's the foundation. Genus OS is designed for environments where data breaches, unauthorized access, and uncontrolled AI behavior are existential risks.

| Layer | Controls |
|---|---|
| **Secrets management** | SOPS + age encryption. Secrets decrypted to tmpfs at runtime — never touch persistent disk unencrypted. Pre-commit gitleaks hook blocks accidental secret commits. |
| **Agent sandboxing** | Per-agent `tools_allowed` / `tools_denied` lists enforced at the engine level. Agents cannot discover or invoke tools outside their authorization scope. 7 runtime guardrail policies block destructive writes, unauthorized HTTP calls, branch manipulation, rate limit violations, secret exposure, unapproved exec commands, and writes to restricted paths. |
| **Event bus RBAC** | Redis Streams with consumer groups. Agents can only subscribe to and publish on streams declared in their manifest. |
| **Network isolation** | All processing runs locally. No data sent to external APIs unless explicitly configured. Air-gap compatible for classified or regulated environments. Cloudflare Access with email OTP protects external-facing services. |
| **Federation security** | Ed25519 cryptographic identity per instance. Signed one-time invite tokens. Scoped exports/imports with no transitive trust. NATS account isolation per connection. Private keys stored with `0600` permissions, never transmitted. |
| **Infrastructure** | Systemd services with `Restart=always`, sd_notify watchdog with health pings, zombie run reaping. Cloudflare tunnel for TLS termination. |

## Enterprise Governance

Every action in Genus OS is tracked, auditable, and controllable.

| Capability | Detail |
|---|---|
| **Audit trails** | Every agent run records: trigger type, model used, tool calls made, tokens consumed, cost incurred, duration, output, and success/failure status. Stored in PostgreSQL with full queryable history. |
| **Human-in-the-loop** | Task review workflows require human approval before agents can proceed. Configurable per agent via `review_workflow: true` in the manifest. |
| **SLA tracking** | Tasks carry priority-based SLA targets. The system tracks time-to-resolution and flags breaches. |
| **Cost controls** | Per-agent token and dollar budgets with hard enforcement. When a budget is exhausted, the agent shuts down gracefully — no runaway spending. |
| **Graduated escalation** | 3 consecutive errors → retry with feedback. 4 → checkpoint + replan. 5 → abort with full diagnostics. No silent failures. |
| **Distributed tracing** | OTel-compatible trace context propagated across agent runs, sub-agent spawns, and federated operations. Plug into Jaeger, Grafana Tempo, or any OTel collector. |
| **Fleet analytics** | Cross-agent performance metrics, anomaly detection (rolling baselines, >2σ flagging), and failure pattern clustering. |
| **Change management** | Agent manifests are declarative YAML checked into version control. Pre-commit validation hook enforces schema compliance. Changes are reviewed via standard PR workflows. |
| **Multi-tenancy** | Every CRM table scoped by `tenant_id`. Bridge middleware enforces tenant isolation at the API layer. Separate agent fleets and data boundaries per business unit. |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  The Helm                                                │
│  Control plane: chat, tasks, events, agents, CRM, health │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────┴────────────────────────────────┐
│  Agent Engine                                            │
│  YAML manifests · workflow pipelines · 101 tools         │
│  APScheduler · Redis Stream hooks · Telegram delivery    │
│  v2: guardrails · planning · checkpoints · telemetry    │
│  sub-agents · analytics · Nightwatch                     │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────┴────────────────────────────────┐
│  Intelligence Layer                                      │
│                                                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐│
│  │ Memory   │ │ CRM      │ │ Vision   │ │ Events     ││
│  │ Facts    │ │ Contacts │ │ YOLO     │ │ Redis      ││
│  │ Entities │ │ Tasks    │ │ Faces    │ │ Streams    ││
│  │ RAG      │ │ Identity │ │ VLM      │ │ RBAC       ││
│  └──────────┘ └──────────┘ └──────────┘ └────────────┘│
│  ┌────────────────────────────────────────────────────┐│
│  │ Voice: Twilio inbound/outbound · Gemini Live · TTS ││
│  └────────────────────────────────────────────────────┘│
│                                                          │
│  PostgreSQL 16 + pgvector  ·  Redis 7  ·  Ollama        │
└─────────────────────────┬────────────────────────────────┘
                          │
┌─────────────────────────┴────────────────────────────────┐
│  Federation                                               │
│  NATS JetStream · Ed25519 tokens · HLC sync · Leaf nodes │
│  Connects independent instances with scoped permissions   │
└──────────────────────────────────────────────────────────┘
```

## Project Structure

```
robothor/
├── robothor/               # Python package — the intelligence layer
│   ├── engine/             # Agent Engine: runner, tools, scheduler, hooks, workflows,
│   │                        #   analytics, guardrails, planner, telemetry, sub-agents
│   ├── memory/             # Two-tier memory, facts, entities, lifecycle
│   ├── rag/                # Semantic search, reranking, context assembly
│   ├── crm/                # Models, validation, blocklists
│   ├── vision/             # YOLO detection, InsightFace recognition, alerts
│   ├── events/             # Redis Streams, RBAC, consumer workers
│   ├── api/                # MCP server (64 tools), RAG orchestrator
│   ├── federation/          # Peer-to-peer instance networking (identity, sync, NATS)
│   ├── health/             # Garmin health data sync
│   └── cli.py              # CLI entry point
│
├── app/                    # The Helm (Next.js 16, React 19, Dockview)
├── crm/                    # CRM stack: Bridge service, migrations, Docker Compose
├── docs/
│   ├── agents/             # YAML agent manifests + PLAYBOOK.md
│   └── workflows/          # Declarative workflow pipelines
├── brain/                  # Scripts, voice, vision, agent instructions (symlink)
├── scripts/                # Backup, validation, utilities
└── templates/              # Bootstrap templates for new instances
```

## CLI Reference

| Command | Purpose |
|---------|---------|
| `robothor init` | Interactive setup wizard |
| `robothor serve` | Start orchestrator + engine |
| `robothor status` | System health overview |
| `robothor migrate` | Run database migrations |
| `robothor mcp` | Start MCP server (64 tools, stdio) |
| `robothor tui` | Terminal monitoring dashboard |
| `robothor agent scaffold <id>` | Scaffold a new agent (manifest + instruction file) |
| `robothor engine start` | Start the engine daemon |
| `robothor engine stop` | Stop the engine |
| `robothor engine status` | Engine health, scheduler, bot |
| `robothor engine run <id>` | Run an agent manually |
| `robothor engine list` | List all scheduled agents |
| `robothor engine history` | Recent agent run history |
| `robothor engine workflow list` | List loaded workflows |
| `robothor engine workflow run <id>` | Execute a workflow manually |
| `robothor federation init` | Generate instance identity (Ed25519 keypair) |
| `robothor federation invite` | Generate signed invite token for a peer |
| `robothor federation connect <token>` | Accept connection from a peer |
| `robothor federation status` | Show identity and all connections |

## Deployment Models

Genus OS runs wherever your security policy requires — no cloud dependency, no vendor lock-in.

| Deployment | Description |
|---|---|
| **Single server** | All services on one machine. Suitable for teams, departments, or small organizations. |
| **Federated multi-site** | Autonomous instances at each site, connected via federation. HQ aggregates health and pushes policy; branches operate independently. |
| **Air-gapped** | Fully offline with local LLMs (Ollama). No internet dependency for core operations. Federation over private NATS links. |
| **Hybrid cloud** | On-prem instances for sensitive workloads, cloud instances for scale-out. Federation bridges the gap with scoped permissions. |
| **Dev/staging/prod** | Separate instances per environment, federated for config sync and telemetry aggregation. |

### Hardware Requirements

| | Minimal | Recommended | Full Stack |
|--|---------|-------------|------------|
| **Use case** | Cloud APIs, no vision | Local small models, RAG, agents | Local 70B+ models, vision, all services |
| **RAM** | 8 GB | 32 GB | 128 GB (unified memory preferred) |
| **Storage** | 256 GB | 512 GB | 1 TB+ |
| **GPU** | None needed | Optional | Integrated or discrete |
| **CPU** | 4 cores | 8+ cores | 16+ cores |
| **Local models** | None (API only) | 7-13B quantized | Up to 80B on-demand |

## Configuration

All configuration via environment variables with sensible defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_WORKSPACE` | `~/robothor` | Working directory |
| `ROBOTHOR_DB_HOST` | `127.0.0.1` | PostgreSQL host |
| `ROBOTHOR_DB_NAME` | `robothor_memory` | Database name |
| `ROBOTHOR_REDIS_HOST` | `127.0.0.1` | Redis host |
| `ROBOTHOR_OLLAMA_HOST` | `127.0.0.1` | Ollama host |
| `EVENT_BUS_ENABLED` | `true` | Enable Redis Streams event bus |

## Infrastructure

The system runs as systemd services behind a Cloudflare tunnel with encrypted secrets (SOPS + age). Internal services are protected by Cloudflare Access; public services are open.

| Service | Purpose |
|---------|---------|
| Agent Engine | LLM runner, scheduler, Telegram bot, event hooks |
| RAG Orchestrator | Semantic search and retrieval API |
| Bridge | CRM API, contact resolution, webhooks, multi-tenancy |
| Vision | YOLO + InsightFace detection loop |
| Voice Server | Twilio inbound/outbound calls + Gemini Live + Kokoro TTS |
| The Helm | Live control plane dashboard |
| NATS Server | Federation transport (JetStream, leaf nodes) |
| Cloudflare Tunnel | All `*.robothor.ai` routes with Access policies |

**Local models (Ollama):**

| Model | Size | Role |
|-------|------|------|
| qwen3:14b | 9.3 GB | Agent workloads |
| llama3.2-vision:11b | 7.8 GB | Vision analysis |
| qwen3-embedding:0.6b | 639 MB | Dense vector embeddings |
| Qwen3-Reranker-0.6B | 1.2 GB | Cross-encoder reranking |

## Testing

```bash
pip install -e ".[dev]"
pytest -m "not slow and not llm and not e2e"   # Fast unit tests
pytest                                          # Full suite
cd app && pnpm test                             # Helm tests
python scripts/validate_agents.py               # Agent manifest validation
```

**2,000+ tests** across Python and TypeScript. See [TESTING.md](docs/TESTING.md) for the full strategy, markers, and coverage plan.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding standards, PR process, and architecture details.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full plan — from AI brain to AI operating system.

## License

MIT License. See [LICENSE](LICENSE).
