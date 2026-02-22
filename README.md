# Robothor

**An autonomous AI entity — persistent memory, semantic search, knowledge graph, vision, CRM, and self-healing infrastructure.**

Not an assistant. Not a framework. An AI *brain* that runs 24/7 on your hardware — managing email, calendar, CRM, security cameras, and voice calls autonomously. Zero cloud dependency for intelligence.

[![Tests: 816+](https://img.shields.io/badge/tests-816%2B%20passing-brightgreen.svg)]()
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Fork Ready](https://img.shields.io/badge/status-fork%20ready-green.svg)]()

## What This Is

Robothor is a complete AI operating system running on a single machine. It processes real emails, real meetings, real security alerts — and responds to them autonomously. The system has been battle-tested in daily production use since February 2026.

**Hardware:** NVIDIA Grace Blackwell GB10, 128 GB unified memory, 20 ARM cores

## System Architecture

```
Layer 1: Data Collection (Python crons)
  email_sync, calendar_sync, jira_sync, garmin_sync, meet_transcript_sync
  vision_service (YOLO + InsightFace, always-on)

Layer 1 Hook: Email Pipeline (event-driven, ~60s email-to-reply)
  email_sync → email_hook → triage → classify → analyze → respond

Layer 1.5: Intelligence Pipeline (3 tiers, all local LLM + pgvector)
  Tier 1: continuous_ingest (*/10 min) — incremental dedup
  Tier 2: periodic_analysis (4x daily) — meeting prep, memory blocks, entities
  Tier 3: intelligence_pipeline (daily) — relationships, patterns, quality

Layer 2: Agent Orchestration (OpenClaw + Kimi K2.5)
  Email Classifier, Calendar Monitor, Email Analyst, Email Responder
  Supervisor Heartbeat, Vision Monitor, CRM Steward, Conversation Resolver

Layer 3: Delivery
  Telegram (primary), Google Chat, Voice (Twilio), Web (the Helm)
```

## Core Modules

### Memory System
Three-tier architecture with structured facts and entity graph:
- **Working memory** — context window (current session)
- **Short-term** — PostgreSQL, 48h TTL, auto-decays
- **Long-term** — PostgreSQL + pgvector, permanent, importance-scored
- **Knowledge graph** — entities, relationships, auto-extracted from all inputs
- **Conflict resolution** — newer facts supersede older ones with confidence scoring

### RAG Pipeline
Fully local retrieval-augmented generation:
- Qwen3-Embedding (dense vectors) → pgvector → Qwen3-Reranker → LLM generation
- All local. No API keys needed for search.

### Event Bus (Redis Streams)
Publish-subscribe replacing polling:
- 7 streams: email, calendar, CRM, vision, health, agent, system
- Standard envelope format with correlation IDs
- Dual-write: Redis Streams + JSON files (fallback)
- SSE endpoint for real-time events in the Helm dashboard

### Agent RBAC
Per-agent capability isolation:
- Capability manifest for 11 agents (tools, streams, endpoints)
- Bridge middleware enforces via `X-Agent-Id` header
- Unauthorized = 403 + audit trail. Missing header = full access (backward compatible)

### Service Registry
Self-describing infrastructure:
- `robothor-services.json` — 20 services with ports, health endpoints, dependencies
- Boot orchestrator starts services in topological dependency order
- Health-gated: waits for each service before starting dependents
- Environment variable overrides for all service URLs

### Vision System
Always-on with event-triggered smart detection:
- **basic mode**: motion → YOLO → InsightFace → instant Telegram photo → async VLM
- Unknown person → Telegram alert in <2 seconds
- Models: YOLOv8-nano (6 MB), InsightFace buffalo_l (300 MB), llama3.2-vision (7.8 GB)

### CRM Stack
Native PostgreSQL tables with cross-channel identity:
- `crm_people`, `crm_companies`, `crm_notes`, `crm_tasks`, `crm_conversations`, `crm_messages`
- Bridge service (port 9100): contact resolution, webhooks, merge operations
- Fuzzy name matching for cross-channel identity resolution

### The Helm (app.robothor.ai)
Agent-driven live dashboard:
- Next.js 16 + Dockview, HTML-first rendering (iframe srcdoc)
- Gemini Flash generates dashboards, Canvas Action Protocol for two-way interaction
- Custom SSE chat via OpenClaw Gateway WebSocket bridge
- Session persistence across reloads

### Audit & Observability
Structured audit trail across all operations:
- Typed events: crm.create/update/delete/merge, service.health, ipc.webhook, auth.denied
- Time-series telemetry table for service metrics
- Query APIs: `/api/audit`, `/api/audit/stats`, `/api/telemetry`

## Services & Ports

| Service | Port | Description |
|---------|------|-------------|
| Bridge | 9100 | CRM glue service, contact resolution, webhooks |
| Orchestrator | 9099 | RAG endpoints, vision proxy |
| Helm | 3004 | Live dashboard (app.robothor.ai) |
| Gateway | 18789 | OpenClaw messaging |
| Vision | 8600 | YOLO + InsightFace detection |
| Voice | 8765 | Twilio ConversationRelay |
| SMS | 8766 | Twilio SMS webhooks |
| TTS | 8880 | Kokoro local voice synthesis |
| Webcam | 8554/8890 | RTSP + HLS stream |
| Uptime Kuma | 3010 | Service monitoring |
| Vaultwarden | 8222 | Password vault |

All services managed by systemd with auto-restart. Cloudflare tunnel for external access with Zero Trust policies.

## Infrastructure

| Component | Technology |
|-----------|-----------|
| Database | PostgreSQL 16 + pgvector 0.6 |
| Cache | Redis 7 (2 GB, shared) |
| Embeddings | Qwen3-Embedding 0.6B (local, Ollama) |
| Reranking | Qwen3-Reranker 0.6B (local, Ollama) |
| Generation | Qwen3-Next 80B (on-demand, local) |
| Vision | YOLOv8-nano + InsightFace + llama3.2-vision |
| TTS | Kokoro (local, CPU, ~3x realtime) |
| Tunnel | Cloudflare Tunnel with Access policies |
| Secrets | SOPS + age (encrypted at rest, tmpfs at runtime) |
| Monitoring | Uptime Kuma + structured audit trail |
| Backup | LUKS-encrypted SSD, daily at 4:30 AM |

## Testing

```bash
# Full suite
bash run_tests.sh

# By module
cd crm/bridge && python -m pytest tests/ -v    # 107 tests
cd app && pnpm test                              # 311 tests
cd brain/memory_system && bash run_tests.sh      # 398 tests
```

**816+ tests** across Python (pytest) and TypeScript (vitest). Zero failures.

## Fork Readiness

The system passes all 6 fork criteria (verified by `scripts/verify-fork-readiness.py`):

1. Services discoverable via registry
2. Agent permissions in capability manifest
3. Helm handles interactive actions
4. Event bus connects components
5. System self-describes at runtime
6. Boot is health-gated

## Repository Layout

| Path | Purpose |
|------|---------|
| `brain/` → `~/clawd/` | Core: memory system, scripts, voice, vision, dashboards |
| `comms/` → `~/moltbot/` | OpenClaw messaging framework |
| `runtime/` → `~/.openclaw/` | OpenClaw runtime: agents, cron jobs, credentials |
| `app/` | The Helm — Next.js 16 live dashboard |
| `crm/` | CRM stack: Bridge service, migrations, Docker Compose |
| `scripts/` | Boot orchestrator, backup, verification |
| `health/` → `~/garmin-sync/` | Garmin health data sync |
| `docs/` | Data flow, cron map, testing strategy |

## License

MIT License. See [LICENSE](LICENSE) (coming soon).

## Related

- **[robothor (pip package)](https://github.com/Ironsail-llc/robothor)** — Extracted open-source Python package of the core intelligence layer
