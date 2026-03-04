<p align="center">
  <img src="docs/images/robothor-logo.png" width="200" alt="Robothor">
</p>

<h1 align="center">Robothor</h1>
<p align="center"><b>An AI operating system you run on your own hardware.</b></p>

<p align="center">
Define agents in YAML. Wire them into pipelines. Manage everything from a live control plane.
Give your system eyes, memory, and a CRM that knows every contact across every channel.
<br><br>
One repo. One CLI. Your hardware.
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/tests-1%2C800%2B%20passing-brightgreen.svg" alt="Tests">
</p>

---

## Highlights

**Platform** — 16 agents defined by declarative YAML manifests. A workflow engine with conditional branching. 78 registered tools with per-agent allow/deny lists. Nested sub-agents (agents spawning focused sub-tasks mid-run). 5 guardrail policies (destructive writes, external HTTP, branch protection, rate limiting, secret scanning). OTel-compatible tracing. Redis Streams event bus with consumer groups and RBAC.

**The Helm** — Not a dashboard, a control plane. Next.js 16 + Dockview with 20 lazy-loaded components. Chat with agents, manage tasks on a Kanban board, watch event streams in real time, monitor service health. Fully extensible component registry.

**Intelligence** — Two-tier memory: working context and long-term facts with hybrid search (HNSW vectors + BM25 keyword matching, fused by Reciprocal Rank Fusion). Knowledge graph that grows autonomously. Local RAG stack with embeddings, reranking, and generation. Facts have confidence scores, categories, and lifecycle states — they decay, consolidate, and get pruned by quality gates. Interactive warmup with entity-aware context injection for conversational sessions.

**Physical** — YOLOv8 nano + InsightFace ArcFace, loaded once at startup. Three runtime modes: disarmed, basic (motion-triggered smart detection), armed (per-frame tracking). Any RTSP camera. Pluggable alert targets. Scene analysis via vision LLM. Telegram file handling — PDFs, images, and documents processed inline. All local, no cloud.

**Operations** — Outbound voice calling (Twilio + Gemini Live). Built-in CRM with cross-channel identity resolution and multi-tenancy. Task state machine (TODO &rarr; IN_PROGRESS &rarr; REVIEW &rarr; DONE) with SLA tracking and agent notifications. Fleet analytics with anomaly detection. Nightwatch: overnight self-improving pipeline (failure analysis &rarr; improvement proposals &rarr; draft PRs). MCP server exposes 65 tools over stdio. Encrypted secrets (SOPS + age), systemd services, Cloudflare tunnel, self-healing watchdogs.

## Quick Start

```bash
git clone https://github.com/Ironsail-llc/robothor.git
cd robothor
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

This creates `docs/agents/support-triage.yaml` (manifest) and `brain/SUPPORT_TRIAGE.md` (instruction file) from templates. Edit them to fit your needs:

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

The engine provides **78 tools** — CRM operations, memory search, file I/O, shell execution, web fetch, task coordination, git operations, voice calling, and more. Each agent sees only the tools in its `tools_allowed` list.

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

A self-improving pipeline that runs overnight. Agents analyze failures, propose improvements, and open draft PRs — all without human intervention.

1. **Failure Analyzer** (every 2h) — Queries recent failures, classifies each as transient, config, code, or unknown. Creates CRM tasks for actionable issues.
2. **Improvement Analyst** (2 AM) — Reviews fleet analytics, identifies recurring patterns, and writes improvement proposals.
3. **Overnight PR Agent** (3 AM, Sonnet 4.6) — Picks up proposals and creates draft PRs on feature branches. Protected by the `no_main_branch_push` guardrail, capped at $2/run, max 3 PRs per night.
4. **Feedback loop** — Tracks merge/reject/modify outcomes. Auto-disables after 3 consecutive rejections. Scope expands based on merge rate: <50% config only, 50–70% +instructions, >70% +code.

Workflow definition: [`docs/workflows/nightwatch.yaml`](docs/workflows/nightwatch.yaml)

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
- **Component Registry** — 20 lazy-loaded components, add your own

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

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  The Helm                                                │
│  Control plane: chat, tasks, events, agents, CRM, health │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────┴────────────────────────────────┐
│  Agent Engine                                            │
│  YAML manifests · workflow pipelines · 78 tools         │
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
│   ├── api/                # MCP server (65 tools), RAG orchestrator
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
| `robothor mcp` | Start MCP server (65 tools, stdio) |
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

## Requirements

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

**1,800+ tests** across Python and TypeScript. See [TESTING.md](docs/TESTING.md) for the full strategy, markers, and coverage plan.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding standards, PR process, and architecture details.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full plan — from AI brain to AI operating system.

## License

MIT License. See [LICENSE](LICENSE).
