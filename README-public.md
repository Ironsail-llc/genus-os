# Robothor

**An AI brain with persistent memory, semantic search, knowledge graph, vision, and self-healing infrastructure.**

Not another agent framework. An AI *brain* — persistent memory that decays and strengthens, a knowledge graph that grows autonomously, agents that see through cameras and read your email — all on your hardware with zero cloud dependency for intelligence.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-411%20passing-brightgreen.svg)]()

## What Makes This Different

| Feature | LangChain / CrewAI / AutoGen | Robothor |
|---------|------------------------------|----------|
| Memory | Stateless (you build it) | Three-tier: working, short-term (48h TTL), long-term (permanent) |
| Knowledge | None | Entity graph with relationships, auto-extracted from all inputs |
| Memory Lifecycle | None | Facts decay, strengthen, supersede, and consolidate autonomously |
| Conflict Resolution | None | Newer facts supersede older ones with confidence scoring |
| Vision | None | YOLO + face recognition + scene analysis (all local) |
| CRM | None | Built-in contact management with cross-channel identity resolution |
| Event Bus | None | Redis Streams with RBAC and consumer groups |
| Agent RBAC | None | Per-agent capability manifests — tools, streams, endpoints |
| Self-Healing | None | Watchdogs, health-gated boot, auto-restart, structured audit trail |
| Service Registry | None | Self-describing topology with dependency-ordered orchestration |
| Cloud Dependency | Required (OpenAI, etc.) | Optional. Runs 100% local with Ollama |

## Quick Start

```bash
pip install robothor

# Configure
export ROBOTHOR_DB_HOST=localhost
export ROBOTHOR_DB_NAME=robothor_memory

# Run migrations
robothor migrate

# Check status
robothor status
```

### Full Stack (Docker)

```bash
git clone https://github.com/robothor-ai/robothor.git
cd robothor
docker-compose up -d   # PostgreSQL+pgvector, Redis, Ollama
robothor migrate
robothor serve
```

## Package Structure

```
robothor/
├── config.py              # Env-based configuration with validation
├── cli.py                 # Command-line interface (migrate, status, serve)
├── db/
│   └── connection.py      # PostgreSQL connection factory with pooling
├── memory/
│   ├── facts.py           # Fact storage with confidence, categories, lifecycle
│   ├── entities.py        # Knowledge graph — entities with types and aliases
│   ├── conflicts.py       # Conflict resolution — supersession and confidence scoring
│   ├── lifecycle.py       # Autonomous decay, strengthening, consolidation
│   ├── tiers.py           # Three-tier memory management (working/short/long)
│   ├── contact_matching.py # Fuzzy name matching for cross-channel identity
│   ├── ingestion.py       # Content ingestion with fact extraction
│   └── ingest_state.py    # Deduplication via content hashing and watermarks
├── rag/
│   ├── search.py          # Semantic search over pgvector embeddings
│   ├── reranker.py        # Cross-encoder reranking (Qwen3-Reranker)
│   ├── pipeline.py        # Full RAG pipeline: embed → search → rerank → generate
│   ├── context.py         # Context assembly for LLM generation
│   ├── profiles.py        # Query profiles for different retrieval strategies
│   └── web_search.py      # SearXNG integration for web-augmented RAG
├── events/
│   ├── bus.py             # Redis Streams — publish, subscribe, ack, consumer groups
│   ├── capabilities.py    # Agent RBAC — capability manifests and access control
│   └── consumers/         # Event-driven consumer workers
│       ├── base.py        # BaseConsumer with signal handling and error recovery
│       ├── email.py       # Email pipeline consumer
│       ├── calendar.py    # Calendar event consumer
│       ├── health.py      # Health alert consumer with escalation
│       └── vision.py      # Vision event consumer
├── audit/
│   └── logger.py          # Structured audit logging with typed events
├── llm/
│   └── ollama.py          # Ollama client — chat, embeddings, model management
├── services/
│   └── registry.py        # Service registry with topology sort and health checks
├── vision/
│   ├── detector.py        # YOLO object detection + motion detection
│   ├── face.py            # InsightFace recognition — enroll, match, persist
│   ├── alerts.py          # Pluggable alert backends (Telegram, webhook)
│   └── service.py         # VisionService — camera loop, mode switching, HTTP API
├── crm/
│   ├── dal.py             # Data access layer — CRUD for people, companies, notes, tasks
│   ├── models.py          # Pydantic models for all CRM entities
│   └── validation.py      # Input validation, blocklists, email normalization
└── api/
    ├── orchestrator.py    # FastAPI RAG orchestrator with vision endpoints
    └── mcp.py             # MCP server — 35 tools for memory, CRM, vision
```

## Architecture

```
Intelligence Layer (robothor.*)          Agent Orchestration
┌────────────────────────────┐          ┌──────────────────┐
│ memory/   - facts, entities│◄────────►│ OpenClaw or any  │
│ rag/      - search, rerank │          │ agent framework  │
│ crm/      - contacts, merge│          │                  │
│ vision/   - detect, faces  │          │ Bridge (HTTP     │
│ events/   - Redis Streams  │          │  adapter for     │
│ audit/    - event logging  │          │  non-Python      │
│ llm/      - provider layer │          │  orchestrators)  │
│ services/ - registry       │          └──────────────────┘
└────────────────────────────┘
        ▲               ▲
        │               │
   ┌────┘               └────┐
   │                         │
MCP Server              System Scripts
(direct import)         (direct import)
```

### Three-Tier Memory

1. **Working Memory** — Current context window (managed by the agent framework)
2. **Short-Term Memory** — PostgreSQL, 48-hour TTL, auto-decays based on access patterns
3. **Long-Term Memory** — PostgreSQL + pgvector, permanent, importance-scored with semantic search

Facts are extracted from all inputs (email, calendar, conversations, vision events) and stored with confidence scores, categories, and lifecycle states. The knowledge graph grows autonomously as entities and relationships are discovered.

### Memory Lifecycle

Facts aren't static. They have a lifecycle:
- **Active** — current, high-confidence facts
- **Decaying** — facts losing relevance over time (configurable TTL)
- **Superseded** — replaced by newer, conflicting information (old fact linked to new)
- **Consolidated** — merged with related facts during periodic analysis

```python
from robothor.memory.facts import store_fact, search_facts
from robothor.memory.conflicts import resolve_conflict

# Store a fact
fact_id = store_fact(
    text="Philip prefers Neovim for Python development",
    category="preference",
    confidence=0.9,
    source="conversation",
    entities=["Philip"],
)

# Later, a conflicting fact arrives
resolve_conflict(
    new_text="Philip switched back to VS Code with Copilot",
    existing_fact_id=fact_id,
    confidence=0.95,  # Higher confidence supersedes
)
```

### Event Bus

Redis Streams with standard envelopes, consumer groups, and RBAC:

```python
from robothor.events.bus import publish, subscribe

# Publish an event
publish("email", "email.received", {
    "from": "alice@example.com",
    "subject": "Meeting tomorrow",
}, source="email-sync")

# Subscribe with a consumer group
def handle_email(event):
    print(f"New email: {event['payload']['subject']}")

subscribe("email", "classifier-group", "worker-1", handler=handle_email)
```

### Agent RBAC

Declare what each agent can access:

```python
from robothor.events.capabilities import load_capabilities, check_access

caps = load_capabilities("agent_capabilities.json")

# Check before allowing a tool call
if check_access(caps, "vision-monitor", "list_people"):
    # Denied — vision agent can't access CRM
    pass
```

### Service Registry

Self-describing infrastructure with dependency-ordered boot:

```python
from robothor.services.registry import get_service_url, get_health_url

# No more hardcoded ports
bridge_url = get_service_url("bridge")        # http://127.0.0.1:9100
health_url = get_health_url("bridge")         # http://127.0.0.1:9100/health

# Environment overrides work too
# export BRIDGE_URL=http://remote-host:9100
```

### RAG Pipeline

Fully local retrieval-augmented generation:

```python
from robothor.rag.pipeline import rag_query

result = rag_query(
    query="What did Philip decide about the deployment?",
    top_k=5,
    rerank=True,
)
# Returns: answer, sources, confidence
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

# Process a frame — detects objects, recognizes faces, sends alerts
await service.process_frame_basic(frame)
```

**Stack:** Motion detection → YOLOv8 (objects) → InsightFace ArcFace (faces) → pluggable alerts (Telegram, webhook). Scene analysis via vision LLM (optional). All local.

### CRM

Built-in contact management with cross-channel identity resolution:

```python
from robothor.crm.dal import create_person, list_people, merge_people

# Create contacts
person_id = create_person("Jane", "Smith", email="jane@example.com")

# Search
results = list_people(search="Jane")

# Merge duplicates (keeper absorbs loser's data)
merge_people(keeper_id=person_id, loser_id=duplicate_id)
```

## Requirements

- **Python 3.11+**
- **PostgreSQL 16+** with pgvector 0.6+ extension
- **Redis 7+**
- **Ollama** (for LLM features — embeddings, reranking, generation)

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
# Install dev dependencies
pip install -e ".[dev]"

# Fast unit tests (pre-commit)
pytest -m "not slow and not llm and not e2e"

# Full suite
pytest

# With coverage
pytest --cov=robothor

# Lint and type check
ruff check robothor/ tests/
mypy robothor/ --ignore-missing-imports
```

**411 tests** across 25 test modules covering config, database, memory, events, consumers, audit, services, contact matching, LLM client, RAG pipeline, CRM, vision, and API layers.

## Development

```bash
git clone https://github.com/robothor-ai/robothor.git
cd robothor
pip install -e ".[dev]"
pytest
ruff check .
mypy robothor/
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding standards, PR process, and architecture details.

## Origin

Robothor started as a personal AI system — an autonomous entity that manages email, calendar, CRM, vision security, and voice calls for its creator. After months of battle-testing in production (handling real emails, real meetings, real security alerts), the core intelligence layer is being extracted into this open-source package.

The production system runs 24/7 on a single machine (NVIDIA Grace Blackwell GB10, 128 GB unified memory) with:
- 1,100+ tests across Python and TypeScript
- 23 services managed by a self-healing boot orchestrator
- Three-tier intelligence pipeline processing data every 10 minutes
- Event-driven email pipeline with ~60-second end-to-end response time
- Always-on vision system with face recognition and instant alerts

This package is the brain. Bring your own body.

## License

MIT License. See [LICENSE](LICENSE).

## Status

**v0.1.0** — Alpha. Full intelligence layer extracted and tested.

**Implemented:**
- Config system with validation
- Database connection factory with pooling
- Service registry with topology sort and health checks
- Event bus (Redis Streams) with RBAC and consumer groups
- Event consumers (email, calendar, health, vision) with graceful shutdown
- Audit logging with typed events
- Memory system (facts, entities, lifecycle, conflicts, tiers, ingestion, dedup)
- Contact matching with fuzzy name resolution
- RAG pipeline (search, rerank, context assembly, web search, profiles)
- LLM client (Ollama — chat, embeddings, model management)
- CRM module (people, companies, notes, tasks, validation, blocklists)
- Vision module (YOLO detection, InsightFace recognition, pluggable alerts, service loop)
- API layer (FastAPI orchestrator, MCP server with 35 tools)
- CLI tool (`robothor migrate`, `robothor status`, `robothor serve`)
- Agent templates (6 agents, 7 skills, 11 cron jobs, plugin template)
- Infrastructure templates (Docker Compose, systemd services, env config)
- 4 usage examples (basic-memory, rag-chatbot, vision-sentry, full-stack)

**Coming:**
- Documentation site
- PyPI release
- More examples and tutorials
