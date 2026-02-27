# Robothor

**An autonomous AI system with persistent memory, agent orchestration, vision, CRM, and a live control plane — running 24/7 on a single machine.**

Not another agent framework. A complete AI *system* — persistent memory that decays and strengthens, a knowledge graph that grows autonomously, 11 agents that read your email and watch through cameras, a CRM that tracks every contact across every channel, and a dashboard to monitor it all. One repo. One CLI. All on your hardware.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-1%2C400%2B%20passing-brightgreen.svg)]()

## Architecture

Three layers, one repo:

```
┌─────────────────────────────────────────────────────────┐
│  The Helm (Next.js 16)                                  │
│  Live dashboard: chat, task board, event streams,       │
│  agent status, CRM views, service health                │
│  Port 3004 · app.robothor.ai                            │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────┴────────────────────────────────┐
│  Agent Orchestration (OpenClaw gateway)                  │
│  11 autonomous agents · Telegram + Google Chat delivery  │
│  YAML-defined manifests · task-based coordination        │
│  Port 18789 · gateway.robothor.ai                        │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────┴────────────────────────────────┐
│  Intelligence Layer (robothor Python package)             │
│  Memory · RAG · CRM · Vision · Events · Audit            │
│                                                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌─────────────┐ │
│  │ MCP      │ │ Bridge   │ │ Orchest- │ │ Vision      │ │
│  │ Server   │ │ Service  │ │ rator    │ │ Service     │ │
│  │ 44 tools │ │ CRM API  │ │ RAG API  │ │ YOLO+Faces  │ │
│  │ (stdio)  │ │ :9100    │ │ :9099    │ │ :8600       │ │
│  └──────────┘ └──────────┘ └──────────┘ └─────────────┘ │
│                                                           │
│  PostgreSQL 16 + pgvector    Redis 7    Ollama (local)   │
└──────────────────────────────────────────────────────────┘
```

## What Makes This Different

| Feature | Typical Agent Frameworks | Robothor |
|---------|--------------------------|----------|
| Memory | Stateless (you build it) | Three-tier: working, short-term (48h TTL), long-term (permanent) |
| Knowledge | None | Entity graph with relationships, auto-extracted from all inputs |
| Memory Lifecycle | None | Facts decay, strengthen, supersede, and consolidate autonomously |
| Vision | None | YOLO + face recognition + scene analysis (all local) |
| CRM | None | Built-in contacts, companies, conversations — cross-channel identity resolution |
| Agent Coordination | Basic task lists | State machine (TODO → IN_PROGRESS → REVIEW → DONE) with SLA tracking |
| Multi-Tenancy | None | Tenant-scoped data isolation across all CRM tables |
| Event Bus | None | Redis Streams with RBAC and consumer groups |
| Dashboard | None | The Helm — live control plane with chat, event streams, and agent status |
| Self-Healing | None | Watchdogs, health-gated boot, auto-restart, structured audit trail |
| Infrastructure | Cloud-dependent | 19 systemd services on a single machine, Cloudflare tunnel, encrypted secrets |
| Cloud Dependency | Required (OpenAI, etc.) | Optional. Runs 100% local with Ollama |

## Quick Start

```bash
# Clone and install the Python package
git clone https://github.com/Ironsail-llc/robothor.git
cd robothor
pip install -e ".[all]"

# Interactive setup: DB, migrations, Redis, Ollama
robothor init

# Build the OpenClaw messaging gateway
robothor gateway build

# Start the system
robothor serve           # Orchestrator + gateway
```

### Full Stack (Docker)

```bash
robothor init --docker   # PostgreSQL+pgvector, Redis, Ollama in Docker
robothor gateway build
robothor serve
```

### CLI

```bash
robothor init            # Interactive setup wizard
robothor serve           # Start orchestrator + gateway
robothor status          # System health overview
robothor migrate         # Run database migrations
robothor mcp             # Start MCP server (44 tools, stdio transport)

# Gateway management
robothor gateway build   # Build the TypeScript gateway from source
robothor gateway status  # Version, build status, health
robothor gateway start   # Start the gateway process
robothor gateway stop    # Stop the gateway
robothor gateway restart # Restart the gateway
robothor gateway config  # Regenerate config from YAML agent manifests
robothor gateway sync    # Pull upstream OpenClaw updates
robothor gateway install-service  # Generate systemd unit
```

## Project Structure

```
robothor/
├── robothor/               # Python intelligence layer
│   ├── memory/             # Three-tier memory, facts, entities, lifecycle, conflicts
│   ├── rag/                # Semantic search, reranking, context assembly, web search
│   ├── crm/                # Contact validation, models, blocklists
│   ├── vision/             # YOLO detection, InsightFace recognition, alerts
│   ├── events/             # Redis Streams, RBAC, consumer workers
│   ├── gateway/            # Gateway manager: build, process, config gen, migrate
│   ├── api/                # MCP server (44 tools), RAG orchestrator
│   ├── db/                 # PostgreSQL connection factory with pooling
│   ├── llm/                # Ollama client (chat, embeddings, model management)
│   ├── audit/              # Structured audit logging with typed events
│   ├── services/           # Service registry with topology sort and health checks
│   ├── config.py           # Env-based configuration with validation
│   └── cli.py              # CLI entry point
│
├── gateway/                # OpenClaw messaging gateway (git subtree, TypeScript)
│
├── app/                    # The Helm — live dashboard (Next.js 16, React 19)
│   └── src/
│       ├── app/            # Pages + API routes
│       ├── components/     # 40+ lazy-loaded components
│       └── lib/            # Component registry, config
│
├── crm/                    # CRM stack
│   ├── bridge/             # Bridge service (FastAPI, 9 routers, RBAC, multi-tenancy)
│   ├── migrations/         # SQL schema (native PostgreSQL, no ORM)
│   └── docker-compose.yml  # Vaultwarden, Kokoro TTS, Uptime Kuma
│
├── runtime/                # Gateway runtime config (git-tracked, secrets scrubbed)
│   ├── openclaw.json       # Gateway configuration
│   └── cron/jobs.json      # Agent cron schedules
│
├── docs/                   # Documentation + agent manifests
│   └── agents/             # 11 YAML agent manifests + PLAYBOOK.md
│
├── brain/                  # → ~/clawd/ (symlink) — scripts, voice, vision, identity
├── health/                 # → ~/garmin-sync/ (symlink) — Garmin health data
├── infra/                  # Docker Compose, migrations, systemd templates
├── examples/               # 4 usage examples
├── scripts/                # Backup, utilities, validation
└── templates/              # Bootstrap templates for new instances
```

## The Intelligence Layer

### Three-Tier Memory

1. **Working Memory** — Current context window (managed by the agent framework)
2. **Short-Term Memory** — PostgreSQL, 48-hour TTL, auto-decays based on access patterns
3. **Long-Term Memory** — PostgreSQL + pgvector, permanent, importance-scored with semantic search

Facts are extracted from all inputs (email, calendar, conversations, vision events) and stored with confidence scores, categories, and lifecycle states. The knowledge graph grows autonomously as entities and relationships are discovered.

```python
import asyncio
from robothor.memory.facts import store_fact, search_facts
from robothor.memory.conflicts import resolve_and_store

# Store a fact
fact_id = asyncio.run(store_fact(
    fact={"fact_text": "Philip prefers Neovim for Python development",
          "category": "preference", "confidence": 0.9,
          "entities": ["Philip"]},
    source_content="conversation about editors",
    source_type="conversation",
))

# Conflicting fact arrives — auto-detects and supersedes
result = asyncio.run(resolve_and_store(
    fact={"fact_text": "Philip switched to VS Code with Copilot",
          "category": "preference", "confidence": 0.95,
          "entities": ["Philip"]},
    source_content="conversation about editors",
    source_type="conversation",
    similarity_threshold=0.7,
))
# result["action"] = "superseded" — old fact linked to new one
```

### RAG Pipeline

Fully local retrieval-augmented generation:

```python
import asyncio
from robothor.rag.pipeline import run_pipeline

result = asyncio.run(run_pipeline(
    query="What did Philip decide about the deployment?",
    profile="factual",  # or "conversational", "analytical"
))
```

**Stack:** Qwen3-Embedding (dense vectors) → pgvector → Qwen3-Reranker (cross-encoder) → LLM generation. All local. No API keys needed.

### Vision

Always-on camera monitoring with three modes:

```python
from robothor.vision.service import VisionService

service = VisionService(
    rtsp_url="rtsp://localhost:8554/webcam",
    default_mode="basic",
)
# Modes: disarmed (idle), basic (motion → detect → identify → alert), armed (per-frame)
service.set_mode("basic")
await service.process_frame_basic(frame)
```

**Stack:** Motion detection → YOLOv8 (objects) → InsightFace ArcFace (faces) → pluggable alerts (Telegram, webhook). Scene analysis via vision LLM. All local.

### CRM

Built-in contact management with cross-channel identity resolution, multi-tenancy, and task workflows:

```python
from robothor.crm.dal import create_person, list_people, merge_people

person_id = create_person("Jane", "Smith", email="jane@example.com")
results = list_people(search="Jane")
merge_people(keeper_id=person_id, loser_id=duplicate_id)  # Keeper absorbs loser's data
```

### Task Coordination

Tasks follow a strict state machine with SLA tracking, review workflow, and agent notifications:

```
TODO → IN_PROGRESS → REVIEW → DONE
                   ↗ (reject)
```

```python
from robothor.crm.dal import create_task, transition_task, approve_task

task_id = create_task(
    title="Analyze Q4 report",
    assigned_to_agent="email-analyst",
    priority="high",
    tags=["analytical", "email"],
)
transition_task(task_id, new_status="IN_PROGRESS", actor="email-analyst")
transition_task(task_id, new_status="REVIEW", actor="email-analyst")
approve_task(task_id, reviewer="supervisor", resolution="Analysis complete")
```

### Event Bus

Redis Streams with standard envelopes, consumer groups, and RBAC:

```python
from robothor.events.bus import publish, subscribe

publish("email", "email.received", {
    "from": "alice@example.com",
    "subject": "Meeting tomorrow",
}, source="email-sync")

subscribe("email", "classifier-group", "worker-1", handler=handle_email)
```

## The Agent Fleet

11 autonomous agents defined as YAML manifests in `docs/agents/`, with config generated via `robothor gateway config`:

| Agent | Model | Schedule | Purpose |
|-------|-------|----------|---------|
| Email Classifier | Kimi K2.5 | Every 2h | Classify emails, route or escalate |
| Email Analyst | Kimi K2.5 | Every 2h | Deep analysis of complex emails |
| Email Responder | Sonnet 4.6 | Every 4h | Compose and send replies |
| Calendar Monitor | Kimi K2.5 | Every 2h | Detect conflicts, cancellations |
| Supervisor | Kimi K2.5 | Every 2h | Audit logs, surface escalations, approve tasks |
| Vision Monitor | Kimi K2.5 | Hourly | Check motion events |
| Conv Inbox Monitor | Kimi K2.5 | Hourly | Check for urgent messages |
| Conv Resolver | Kimi K2.5 | 3x/day | Auto-resolve stale conversations |
| CRM Steward | Kimi K2.5 | Daily | Data hygiene + contact enrichment |
| Morning Briefing | Kimi K2.5 | 6:30 AM | Calendar, email, weather summary |
| Evening Wind-Down | Kimi K2.5 | 9:00 PM | Tomorrow preview, open items |

Agents communicate through tasks (not files). The supervisor reviews and approves. Event-driven email pipeline handles real-time (~60s email-to-reply); crons are the safety net.

### Agent Manifest Example

```yaml
# docs/agents/email-classifier.yaml
id: email-classifier
name: Email Classifier
model:
  primary: openrouter/moonshotai/kimi-k2.5
  fallback: openrouter/google/gemini-2.5-pro
schedule: "0 6-22/2 * * *"
timeout: 300
delivery: announce
tools:
  allow: [create_task, list_tasks, update_task, resolve_task, ...]
  deny: [delete_task, create_person, ...]
```

## The Helm

A live control plane dashboard at `app.robothor.ai` (Next.js 16 + Dockview):

- **Chat** — Direct conversation with agents via the gateway
- **Task Board** — Kanban view with approve/reject workflow
- **Event Streams** — Real-time SSE feed from all Redis Streams
- **Agent Status** — Live health and run status for all cron agents
- **CRM Views** — Contacts, companies, conversations
- **Service Health** — System topology and service status
- **Tenant Switching** — Switch between tenants for multi-tenant deployments

## Infrastructure

### Services

19 systemd services running 24/7, all system-level (`sudo systemctl`):

| Service | Port | Purpose |
|---------|------|---------|
| robothor-gateway | 18789 | OpenClaw messaging gateway |
| robothor-orchestrator | 9099 | RAG + vision API |
| robothor-bridge | 9100 | CRM API, contact resolution, webhooks |
| robothor-vision | 8600 | YOLO + InsightFace detection loop |
| robothor-app | 3004 | The Helm dashboard |
| robothor-voice | 8765 | Twilio voice server |
| robothor-sms | 8766 | Twilio SMS webhooks |
| robothor-status | 3000 | robothor.ai homepage |
| robothor-crm | — | Docker: Vaultwarden, Kokoro TTS, Uptime Kuma |
| cloudflared | — | Cloudflare tunnel (all *.robothor.ai routes) |

All internal services are protected by Cloudflare Access (email OTP). Public services (status, voice) are unprotected.

### Hardware

The production system runs on a **Lenovo ThinkStation PGX** — NVIDIA Grace Blackwell GB10, 128 GB unified memory, ARM Cortex-X925 (20 cores). ~41% memory utilization with all services running.

### Local Models (Ollama)

| Model | Size | Role |
|-------|------|------|
| qwen3:14b | 9.3 GB | Agent workloads (calendar, vision, CRM) |
| llama3.2-vision:11b | 7.8 GB | Vision analysis, intelligence pipeline |
| qwen3-embedding:0.6b | 639 MB | Dense vector embeddings (1024-dim) |
| Qwen3-Reranker-0.6B | 1.2 GB | Cross-encoder reranking |

### Secrets

SOPS + age encryption. All credentials encrypted at rest in `/etc/robothor/secrets.enc.json`, decrypted to tmpfs at runtime. Gitleaks pre-commit hook blocks accidental leaks.

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
| `ROBOTHOR_DB_PORT` | `5432` | PostgreSQL port |
| `ROBOTHOR_DB_NAME` | `robothor_memory` | Database name |
| `ROBOTHOR_DB_USER` | `$USER` | Database user |
| `ROBOTHOR_DB_PASSWORD` | *(empty)* | Database password |
| `ROBOTHOR_REDIS_HOST` | `127.0.0.1` | Redis host |
| `ROBOTHOR_REDIS_PORT` | `6379` | Redis port |
| `ROBOTHOR_OLLAMA_HOST` | `127.0.0.1` | Ollama host |
| `ROBOTHOR_OLLAMA_PORT` | `11434` | Ollama port |
| `EVENT_BUS_ENABLED` | `true` | Enable/disable Redis Streams event bus |

## Testing

```bash
pip install -e ".[dev]"

# Fast unit tests (pre-commit)
pytest -m "not slow and not llm and not e2e"

# Full suite
pytest

# Helm tests
cd app && pnpm test

# Agent manifest validation
python scripts/validate_agents.py
```

**1,400+ tests** across Python and TypeScript:
- **483** package unit tests (memory, events, consumers, audit, services, CRM, RAG, vision, API)
- **203** Bridge integration tests (RBAC, multi-tenancy, task coordination, notifications, review workflow)
- **209** system script tests (email pipeline, cron jobs, task cleanup, data archival)
- **143** memory system tests (ingestion, analysis, vision)
- **39** gateway manager tests (config gen, build, process lifecycle)
- **354** Helm vitest tests (35 suites — components, hooks, API routes, event bus)

## Development

```bash
git clone https://github.com/Ironsail-llc/robothor.git
cd robothor
pip install -e ".[dev]"
pytest
ruff check .
mypy robothor/
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding standards, PR process, and architecture details.

## Roadmap

Robothor's path from AI brain to AI operating system. See [ROADMAP.md](ROADMAP.md) for the full plan.

- **v0.1-0.3** — Implemented. Memory, RAG, CRM, vision, events, RBAC, audit, service registry.
- **v0.4** — Process model. Agent lifecycle management with supervised execution.
- **v0.5** — Capabilities. Per-agent rate limiting, data scoping, resource quotas.
- **v0.6** — Scheduler. Unified cron + event-driven scheduling.
- **v0.7** — Channel drivers. Messaging abstraction beyond OpenClaw.
- **v0.8** — Device abstraction. Cameras, microphones, sensors as first-class resources.
- **v0.9** — The Helm as shell. Process manager, file browser, resource monitor.
- **v1.0** — Unified syscall interface. The complete AI operating system.

## License

MIT License. See [LICENSE](LICENSE).
