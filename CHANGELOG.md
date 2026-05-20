# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Behavior change** — Forward thread planner (`thread_planner.py`) is now **on by default**. Previously gated by `ROBOTHOR_PLANNER_ENABLED=1`; from the task-system stabilization, the variable defaults to `"1"` and only `ROBOTHOR_PLANNER_ENABLED=0` disables it. Operators who want the old off-by-default behavior must set the env explicitly.
- `crm_tasks.autonomy_budget` is now validated at write time via `robothor.engine.autonomy.validate_budget`. Malformed budgets (negative caps, unknown verdicts, extra top-level keys) cause `create_task` / `update_task` to return `{"error": reason}` instead of silently degrading the planner.
- `approve_task` and `reject_task` now reset `crm_tasks.escalation_count` to 0 — operator engagement closes the escalation tally rather than letting it grow forever.
- Rebranded project from "Robothor" to "Genus OS". Robothor remains the name of Philip's personal AI instance. Python package name (`robothor`), directory structure, env vars, and systemd services are unchanged.

### Added
- `robothor.engine.autonomy.validate_budget(budget)` — pure validator for the JSONB `autonomy_budget` shape. Used by the CRM DAL.
- `docs/TASK_HISTORY_KIND.md` — canonical enum of `crm_task_history.metadata.kind` values; backed by a `NOT VALID` CHECK constraint in migration 067 and a meta-test that fails CI on drift.
- `crm/migrations/067_task_history_kind_schema.sql` — adds `question_resolved_at` / `question_resolved_by` columns on `crm_tasks` and the metadata-kind CHECK constraint on `crm_task_history`.
- `scripts/audit_autonomy_budgets.py` — read-only diagnostic that flags pre-existing tasks whose `autonomy_budget` would fail the new validator. Run before promoting migration 067's constraint from `NOT VALID` to validated.
- Prometheus metrics for the thread planner: `robothor_planner_actions_total{action,tenant}` and `robothor_planner_run_duration_seconds{tenant}`. See `docs/PLANNER_OBSERVABILITY.md`.
- Structured log events `planner.run_complete` (INFO, per-beat) and `planner.action.refused` (WARNING, per refused plan). All instrumentation wrapped in `contextlib.suppress(Exception)` so observability never breaks the lifecycle.
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
