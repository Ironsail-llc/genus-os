# Roadmap

Robothor's path from AI brain to AI operating system.

## v0.1 -- Memory & RAG (current)

The foundation. Three-tier memory with lifecycle management, fact extraction, conflict resolution, knowledge graph, semantic search, and RAG pipeline.

- [x] Config system with env-based validation
- [x] Database connection factory with pooling
- [x] Fact store with LLM extraction, confidence scoring, and categories
- [x] Conflict resolution (duplicate, update, contradiction detection)
- [x] Memory lifecycle (decay, importance scoring, consolidation)
- [x] Three-tier memory (working/short-term/long-term) with auto-archival
- [x] Knowledge graph (entities + relations, auto-extracted)
- [x] Ingestion pipeline with dedup (content hashing + watermarks)
- [x] Contact matching (fuzzy names, nickname canonicalization)
- [x] RAG pipeline (embed, search, rerank, generate)
- [x] LLM client (Ollama: chat, embeddings, model management)
- [x] CLI (`robothor migrate`, `robothor status`, `robothor serve`)

## v0.2 -- CRM & Vision

Built-in contact management and always-on camera monitoring.

- [x] CRM module (people, companies, notes, tasks, validation, blocklists)
- [x] Cross-channel identity resolution (contact_identifiers table)
- [x] Merge operations (people, companies -- fills gaps, re-links records)
- [x] Vision module (YOLO detection, InsightFace recognition, pluggable alerts)
- [x] Vision service loop with mode switching (disarmed/basic/armed)
- [ ] Migration runner (`robothor migrate` executes SQL files)
- [ ] CRM import/export (CSV, vCard)

## v0.3 -- Events & Infrastructure

Event-driven architecture and self-describing infrastructure.

- [x] Event bus (7 Redis Streams, standard envelopes, consumer groups)
- [x] Agent RBAC (capability manifests, tool/stream/endpoint access control)
- [x] Event consumers (email, calendar, health, vision) with graceful shutdown
- [x] Service registry (topology sort, health checks, env overrides)
- [x] Audit logging with typed events and telemetry table
- [x] API layer (FastAPI orchestrator, MCP server with 35 tools)
- [x] Infrastructure templates (Docker Compose, systemd, env config)
- [ ] Documentation site (MkDocs or similar)
- [ ] PyPI release

## v0.4 -- Process Model

Agent lifecycle management. Agents become first-class citizens with defined states.

- [ ] Agent registry (declare, discover, inspect agents)
- [ ] Lifecycle states: `idle` -> `starting` -> `running` -> `stopping` -> `stopped`
- [ ] Supervised execution (restart policies, failure budgets)
- [ ] Agent health reporting (heartbeat protocol)
- [ ] Process isolation (each agent gets its own working memory scope)

## v0.5 -- Capabilities

Fine-grained, runtime-enforced permissions.

- [ ] Per-agent rate limiting (tool calls/minute, tokens/hour)
- [ ] Data scoping (agent X can only see its own facts, not all facts)
- [ ] Resource quotas (memory blocks, stream throughput)
- [ ] Capability negotiation (agent requests capabilities, system grants/denies)
- [ ] Audit trail for all capability checks (who asked for what, when)

## v0.6 -- Scheduler

Unified scheduling -- cron and event-driven in one system.

- [ ] Declarative job definitions (replace crontab entries with config)
- [ ] Event triggers (run job when stream event matches pattern)
- [ ] Time triggers (cron expressions, intervals, one-shot)
- [ ] Job dependencies (job B runs after job A completes)
- [ ] Backpressure (skip if previous run still active)
- [ ] Job history and failure tracking

## v0.7 -- Channel Drivers

Messaging abstraction. OpenClaw becomes one implementation among many.

- [ ] Channel driver interface (send, receive, status, capabilities)
- [ ] Built-in drivers: email (IMAP/SMTP), Telegram, webhook
- [ ] OpenClaw adapter (wraps existing OpenClaw gateway)
- [ ] Channel routing (rules for which agent handles which channel)
- [ ] Message normalization (all channels produce the same event format)

## v0.8 -- Device Abstraction

Hardware as first-class resources.

- [ ] Device registry (cameras, microphones, sensors, GPIO)
- [ ] Device capabilities and status
- [ ] Hot-plug support (detect new devices, auto-configure)
- [ ] Device sharing (multiple agents can share a camera with priority)
- [ ] Remote device access (devices on other machines via network)

## v0.9 -- The Helm as Shell

The dashboard becomes the operating system's shell.

- [ ] Process manager panel (start, stop, inspect agents)
- [ ] File system browser (memory facts, entities, blocks)
- [ ] Log viewer (real-time streaming from all agents)
- [ ] Resource monitor (memory usage, token consumption, stream throughput)
- [ ] Agent console (interactive chat with any running agent)
- [ ] Plugin system for custom panels

## v1.0 -- Unified Syscall Interface

The complete operating system. Every capability accessible through one interface.

- [ ] Syscall-style API (process, memory, device, channel, schedule, capability)
- [ ] Agent app store (install, update, remove agent packages)
- [ ] Multi-node support (distribute agents across machines)
- [ ] Snapshot and restore (full system state backup)
- [ ] SDK for building agent apps against the Robothor API
- [ ] Comprehensive documentation and tutorials

---

**Current status:** v0.1-0.3 implemented (411 tests passing). The intelligence layer is solid. Next: extracting the process model from the production system into the open-source package.
