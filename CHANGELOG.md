# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Gateway unification — OpenClaw source as git subtree with `robothor gateway` CLI
- Gateway manager package (`robothor/gateway/`) — build, process, config gen, migrate
- YAML-first agent manifests (`docs/agents/`) with `validate_agents.py`
- Agent task coordination — state machine (TODO → IN_PROGRESS → REVIEW → DONE) with SLA tracking
- Review workflow with approve/reject, history tracking, and agent notifications
- Multi-tenancy with tenant-scoped data isolation across all CRM tables
- Bridge service — CRM API with 9 routers, RBAC middleware, tenant isolation
- Event bus — 7 Redis Streams with standard envelopes, consumer groups, and RBAC
- Agent RBAC — per-agent capability manifests (tools, streams, endpoints)
- The Helm — Next.js 16 live dashboard with chat, task board, event streams
- Service registry with topology sort and health-gated boot orchestration
- Audit logging with typed events and telemetry table
- SOPS + age secrets management with cron/systemd wrappers
- Vision module — YOLO detection, InsightFace recognition, pluggable alerts
- CRM module — people, companies, notes, tasks, validation, blocklists, merge
- Memory system — facts, entities, blocks, lifecycle, conflicts, tiers, ingestion
- RAG pipeline — search, rerank, context assembly, web search, profiles
- MCP server with 44 tools for memory, CRM, vision
- Config system with env-based validation and interactive setup wizard
- Database connection factory with pooling
- CI pipeline with ruff, mypy, and pytest on Python 3.11/3.12/3.13
