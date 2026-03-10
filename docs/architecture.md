# Architecture

Genus OS is an AI intelligence layer -- a Python package (`robothor.*`) that provides persistent memory, semantic search, a knowledge graph, vision, CRM, and event-driven infrastructure. It is not an agent framework. It is the intelligence layer that any agent framework can build on.

## System Diagram

```
                              Agent Orchestration
                        ┌──────────────────────────┐
                        │  Agent Engine, LangChain, │
                        │  CrewAI, custom, or any   │
                        │  framework that can call  │
                        │  Python or HTTP            │
                        └─────────┬────────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              │                   │                    │
              v                   v                    v
       MCP Server           Bridge (HTTP)        Direct Import
    (robothor.api.mcp)    (robothor.api.*)     (from robothor.*)
    stdio transport        FastAPI on :9099      Python scripts,
    35 tools               REST + SSE            cron jobs, CLI
              │                   │                    │
              └───────────────────┼───────────────────┘
                                  │
                                  v
              ┌──────────────────────────────────────┐
              │    Intelligence Layer (robothor.*)    │
              │                                      │
              │  memory/    Facts, entities, tiers,  │
              │             lifecycle, conflicts,    │
              │             ingestion, dedup         │
              │                                      │
              │  rag/       Embed -> search ->       │
              │             rerank -> generate       │
              │                                      │
              │  events/    Redis Streams bus,       │
              │             RBAC, consumer workers   │
              │                                      │
              │  crm/       People, companies,       │
              │             notes, tasks, merge      │
              │                                      │
              │  vision/    YOLO detection,          │
              │             face recognition,        │
              │             alerting, service loop   │
              │                                      │
              │  services/  Registry, topology sort, │
              │             health checks            │
              │                                      │
              │  audit/     Structured event logging │
              │  llm/       Ollama client layer      │
              └──────────────┬───────────────────────┘
                             │
              ┌──────────────┼──────────────────┐
              v              v                   v
         PostgreSQL       Redis              Ollama
         + pgvector    (Streams +          (Embeddings,
         (facts,       cache)              reranking,
          entities,                        generation,
          CRM, audit)                      vision)
```

## Three-Tier Memory

| Tier | Storage | TTL | Purpose |
|------|---------|-----|---------|
| Working | Context window | Session | Current conversation state (managed by agent framework) |
| Short-term | PostgreSQL + pgvector | 48 hours | Recent observations, auto-decays based on access patterns |
| Long-term | PostgreSQL + pgvector | Permanent | Important facts, importance-scored, semantic search |

Short-term memories that are accessed frequently get archived to long-term before expiry. The maintenance job (`tiers.run_maintenance()`) handles this automatically.

## Fact Lifecycle

Facts are not static rows. Each has lifecycle state:

- **Active** -- current, high-confidence, searchable
- **Decaying** -- losing relevance (decay_score < 0.3), still searchable
- **Superseded** -- replaced by newer conflicting fact, `is_active=FALSE`, linked via `superseded_by`
- **Consolidated** -- merged with similar facts during periodic analysis

Decay formula: `score = max(importance_floor, recency) + access_boost + reinforcement_boost`

## Knowledge Graph

Entities (`memory_entities`) and relations (`memory_relations`) form an auto-growing graph. Entities are extracted from ingested content via LLM. Types: `person`, `project`, `organization`, `technology`, `location`, `event`.

Relations are simple verb phrases (`uses`, `works_at`, `manages`, `built_with`) with confidence scores. Upserts increment `mention_count` -- frequently mentioned entities naturally rise.

## Event Bus

Seven Redis Streams carry events between services:

| Stream | Events |
|--------|--------|
| `email` | `email.new`, `email.classified`, `email.responded` |
| `calendar` | `calendar.new`, `calendar.changed`, `calendar.conflict` |
| `crm` | `crm.create`, `crm.update`, `crm.merge`, `crm.delete` |
| `vision` | `vision.motion`, `vision.person`, `vision.unknown` |
| `health` | `health.check`, `health.alert`, `health.recovery` |
| `agent` | `agent.started`, `agent.completed`, `agent.error` |
| `system` | `system.boot`, `system.shutdown`, `system.error` |

Standard envelope format. Consumer groups for parallel processing. RBAC via `agent_capabilities.json`. Dual-write to JSON files as fallback when Redis is unavailable.

## Agent RBAC

Each agent declares its capabilities in `agent_capabilities.json`: which tools it can call, which streams it can read/write, which Bridge endpoints it can access. Unknown agents get full access (backward compatible).

## Service Registry

`robothor-services.json` declares all services with ports, health endpoints, dependencies, and systemd unit names. `topological_sort()` provides dependency-ordered boot. Environment variables override manifest defaults.

## Access Patterns

| Consumer | Access Method | Use Case |
|----------|--------------|----------|
| Python scripts/crons | `from robothor.memory import ...` | Direct import, fastest path |
| MCP clients (Claude Code, etc.) | `robothor mcp` (stdio) | 35 tools, direct DB access |
| Non-Python services | Bridge HTTP API on :9100 | REST endpoints for CRM, contacts, search |
| Dashboards | API server on :9099 | FastAPI with SSE for real-time events |
| Agent frameworks | Any of the above | Agent Engine calls DAL directly; LangChain can import directly |
