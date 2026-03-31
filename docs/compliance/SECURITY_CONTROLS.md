# Genus OS Security Controls Inventory

Version: 1.0
Last updated: 2026-03-30
Scope: Platform-level controls shipped with every Genus OS deployment

---

## Overview

This document inventories the security controls built into the Genus OS platform.
Each control has a unique identifier, category, description, implementation details,
and evidence guidance for auditors.

Deployers inherit these controls automatically. Where deployer-specific action is
required, it is noted in the **Deployer Action** column.

---

## Control Categories

| Prefix | Category              | Count |
|--------|-----------------------|-------|
| AC     | Access Control        | 5     |
| DP     | Data Protection       | 4     |
| MA     | Monitoring & Audit    | 4     |
| CM     | Change Management     | 3     |
| ES     | Execution Safety      | 5     |
| IR     | Incident Response     | 3     |

**Total: 24 controls**

---

## AC -- Access Control

### AC-01: Cloudflare Zero Trust

| Field           | Detail |
|-----------------|--------|
| **ID**          | AC-01 |
| **Category**    | Access Control |
| **Description** | All external-facing services are proxied through Cloudflare Tunnel with Zero Trust Access policies. Authentication is enforced at the network edge before traffic reaches the origin server. |
| **Implementation** | Cloudflare Tunnel routes are defined in the tunnel configuration (`~/.cloudflared/config.yml`). Access policies are managed in the Cloudflare Zero Trust dashboard. Protected applications include: Helm dashboard, webcam feed, Engine API, voice server, status page, Bridge API, and NATS management. Public-facing endpoints (status, voice webhook, privacy policy) are explicitly exempted. Authentication method is email OTP restricted to authorized admin email addresses. |
| **Evidence** | Cloudflare Access audit logs (Dashboard > Logs > Access); tunnel route configuration; `SERVICES.md` external access table listing protected vs. public endpoints. |
| **Deployer Action** | Configure Cloudflare account, define authorized email addresses for OTP, and review Access policies during initial deployment. |

---

### AC-02: API Key Authentication

| Field           | Detail |
|-----------------|--------|
| **ID**          | AC-02 |
| **Category**    | Access Control |
| **Description** | Programmatic access to the Engine API requires a Bearer token in the `Authorization` header. Requests without a valid token receive HTTP 401. |
| **Implementation** | The Engine daemon validates the `Authorization: Bearer <token>` header on all `/api/*` endpoints. The token is stored in the SOPS-encrypted secrets file and loaded into the process environment at runtime. Token rotation is performed by updating the SOPS file and restarting the service. |
| **Evidence** | Engine daemon source (`robothor/engine/daemon.py`) -- authentication middleware; SOPS-encrypted secrets file; HTTP 401 responses on unauthenticated requests. |
| **Deployer Action** | Generate a strong API key during deployment. Rotate on a schedule appropriate to the deployment's risk profile. |

---

### AC-03: RBAC Middleware -- Agent Identity Validation

| Field           | Detail |
|-----------------|--------|
| **ID**          | AC-03 |
| **Category**    | Access Control |
| **Description** | Internal API calls carry an `X-Agent-Id` header identifying the calling agent. Middleware validates this header against the agent registry and enforces per-agent access boundaries. |
| **Implementation** | The Bridge service and Engine API both validate `X-Agent-Id` on incoming requests. The agent ID must correspond to a registered agent in the manifest registry. Unrecognized agent IDs are rejected. Agent-specific permissions (e.g., which CRM records an agent can modify) are derived from the agent's manifest configuration. |
| **Evidence** | Bridge middleware source (`crm/bridge/`); Engine request handling; agent manifest `tools_allowed` fields; audit log entries showing agent identity on each action. |
| **Deployer Action** | None -- enforced automatically for all registered agents. |

---

### AC-04: Tool Permission Layering

| Field           | Detail |
|-----------------|--------|
| **ID**          | AC-04 |
| **Category**    | Access Control |
| **Description** | Each agent can only invoke tools explicitly listed in its manifest `tools_allowed` field. Tool calls not on the allowlist are rejected before execution. |
| **Implementation** | Agent manifests (`docs/agents/*.yaml`) define a `tools_allowed` list. The engine runner checks every tool call against this list before dispatching. Additionally, an `exec_allowlist` field restricts which shell commands an agent may execute via the `exec` tool. Tools not present in either list are blocked with a structured error returned to the LLM. |
| **Evidence** | Agent manifest files; engine tool dispatcher source (`robothor/engine/tools/`); test suite (`test_sub_agents.py`, tool permission tests); guardrail event logs for blocked tool calls. |
| **Deployer Action** | Review and customize `tools_allowed` per agent when creating or modifying agent manifests. Follow the principle of least privilege. |

---

### AC-05: Protected Branch Safety

| Field           | Detail |
|-----------------|--------|
| **ID**          | AC-05 |
| **Category**    | Access Control |
| **Description** | A guardrail policy prevents any agent from pushing commits directly to protected branches (main, master). All agent-authored changes must go through pull requests. |
| **Implementation** | The `no_main_branch_push` guardrail in `robothor/engine/guardrails.py` intercepts `git_push` and `git_branch` tool calls targeting protected branches. The `PROTECTED_BRANCHES` constant defines the blocked branch names. The Overnight PR agent is explicitly configured with this guardrail and creates draft PRs that require human merge approval. |
| **Evidence** | Guardrail source code; `agent_guardrail_events` table entries; Overnight PR agent manifest (`overnight-pr.yaml`) showing `no_main_branch_push` in guardrails list; GitHub PR history showing draft PRs from the Nightwatch system. |
| **Deployer Action** | None -- enforced automatically. Deployers may extend `PROTECTED_BRANCHES` to include additional branch names. |

---

## DP -- Data Protection

### DP-01: SOPS Encryption for Secrets

| Field           | Detail |
|-----------------|--------|
| **ID**          | DP-01 |
| **Category**    | Data Protection |
| **Description** | All secrets (API keys, database credentials, service tokens) are stored in a SOPS-encrypted file using age-based encryption. Secrets are never stored in plaintext on disk. |
| **Implementation** | The canonical secrets file is `/etc/robothor/secrets.enc.json`, encrypted with SOPS using an age recipient key. At service startup, a `decrypt-secrets.sh` script decrypts secrets to a tmpfs mount (`/run/robothor/secrets.env`), which is memory-backed and never written to persistent storage. Systemd services reference this file via `EnvironmentFile=`. Python code accesses secrets through `os.getenv()`. |
| **Evidence** | SOPS-encrypted file on disk (verify with `sops --decrypt --output-type json`); systemd unit files showing `EnvironmentFile=/run/robothor/secrets.env`; `mount` output confirming tmpfs at `/run/robothor/`; pre-commit gitleaks hook preventing accidental commits. |
| **Deployer Action** | Generate an age keypair during deployment. Encrypt all secrets with SOPS before first use. Establish a key rotation schedule. |

---

### DP-02: Vault Credential Storage

| Field           | Detail |
|-----------------|--------|
| **ID**          | DP-02 |
| **Category**    | Data Protection |
| **Description** | The Vault module provides an encrypted credential store with scoped access, allowing agents to retrieve credentials for specific integrations without exposing the full secrets file. |
| **Implementation** | The `robothor/vault/` package implements credential storage with encryption at rest. Credentials are scoped by service name and agent identity. Access is mediated through the Vault API, which validates the requesting agent's identity before returning credentials. Credentials are returned as short-lived references, not raw values. |
| **Evidence** | Vault package source (`robothor/vault/`); access logs showing credential retrieval by agent ID; test suite. |
| **Deployer Action** | Populate Vault with integration credentials during deployment. Review scoping rules to ensure least-privilege access. |

---

### DP-03: No-Sensitive-Data Guardrail

| Field           | Detail |
|-----------------|--------|
| **ID**          | DP-03 |
| **Category**    | Data Protection |
| **Description** | A post-execution guardrail scans agent output for sensitive data patterns (AWS access keys, API tokens, private keys, connection strings) and blocks delivery if detected. |
| **Implementation** | The `no_sensitive_data` guardrail in `robothor/engine/guardrails.py` runs regex pattern matching against tool call results and agent output text. Patterns include: AWS key prefixes (`AKIA`), generic API token formats, PEM-encoded private keys, and database connection strings with embedded credentials. Matched content triggers a BLOCK action, preventing the output from reaching the delivery channel. |
| **Evidence** | Guardrail source code with pattern definitions; `agent_guardrail_events` table entries with `policy=no_sensitive_data`; test cases in guardrail test suite. |
| **Deployer Action** | Review default patterns and add organization-specific patterns (e.g., internal token prefixes) as needed. |

---

### DP-04: Tenant Isolation

| Field           | Detail |
|-----------------|--------|
| **ID**          | DP-04 |
| **Category**    | Data Protection |
| **Description** | Multi-tenant deployments enforce isolation at the database level and through API middleware, preventing cross-tenant data access. |
| **Implementation** | Database tables include tenant-scoping columns. API middleware validates tenant context on every request and injects tenant filters into all queries. The Bridge service enforces tenant boundaries on CRM data access. Federation peers are authenticated with Ed25519 identity keys and Consul-style tokens, ensuring cross-instance communication is authorized and scoped. |
| **Evidence** | Database schema showing tenant columns; Bridge middleware source; Federation authentication code (`robothor/federation/`); integration tests validating cross-tenant query rejection. |
| **Deployer Action** | Configure tenant identifiers during deployment. For federated deployments, establish peer trust relationships using the federation CLI. |

---

## MA -- Monitoring & Audit

### MA-01: Audit Logging

| Field           | Detail |
|-----------------|--------|
| **ID**          | MA-01 |
| **Category**    | Monitoring & Audit |
| **Description** | All significant system events are recorded as structured entries in the `audit_log` database table. Events span CRM operations, agent actions, authentication attempts, and system lifecycle events. |
| **Implementation** | The audit logging subsystem captures events with the following fields: timestamp, event type, actor (agent ID or user), action, resource, detail (JSON), and outcome (success/failure). Event categories include: `crm.*` (contact/company/task CRUD), `agent.*` (run start/complete/fail, tool calls), `auth.*` (login attempts, token validation), and `system.*` (service start/stop, config changes). Logs are written synchronously within the same database transaction as the action they record. |
| **Evidence** | `audit_log` table schema and contents; Bridge router source showing audit calls on CRM operations; Engine runner source showing audit calls on agent lifecycle events. |
| **Deployer Action** | Establish a retention policy for audit logs. Configure log export to a SIEM or log aggregation service if required by organizational policy. |

---

### MA-02: Guardrail Event Logging

| Field           | Detail |
|-----------------|--------|
| **ID**          | MA-02 |
| **Category**    | Monitoring & Audit |
| **Description** | Every guardrail evaluation (block, warn, or allow) is recorded in the `agent_guardrail_events` database table, providing a complete audit trail of policy enforcement decisions. |
| **Implementation** | The guardrail engine (`robothor/engine/guardrails.py`) writes a record for each policy evaluation during an agent run. Fields include: run ID, policy name, action (BLOCK/WARN/ALLOW), trigger details (the tool call or output that triggered evaluation), and timestamp. The `/api/v2/stats` endpoint exposes aggregated guardrail event metrics. |
| **Evidence** | `agent_guardrail_events` table schema and contents; guardrail engine source; v2 stats API response showing guardrail summaries. |
| **Deployer Action** | Monitor guardrail event trends for anomalies. A spike in BLOCK events may indicate a misconfigured agent or an attempted policy violation. |

---

### MA-03: Agent Telemetry

| Field           | Detail |
|-----------------|--------|
| **ID**          | MA-03 |
| **Category**    | Monitoring & Audit |
| **Description** | Every agent run produces a structured telemetry trace with spans covering planning, tool execution, LLM calls, and delivery. Cost and token usage are tracked per run. |
| **Implementation** | The telemetry module (`robothor/engine/telemetry.py`) implements OpenTelemetry-compatible trace contexts. Each run gets a unique `trace_id`. Spans are created for: planning phase, each tool call (with input/output), each LLM interaction (with token counts), verification phase, and delivery. Sub-agent runs inherit the parent's `trace_id` for distributed tracing. Cost is calculated per run and stored on the `agent_runs` record. The `/api/runs/{id}` endpoint returns full trace data. |
| **Evidence** | `agent_runs` table with cost and token columns; telemetry module source; API responses showing trace/span data; sub-agent runs showing `parent_trace_id` linkage. |
| **Deployer Action** | Optionally export traces to an external observability platform (Jaeger, Datadog, etc.) by configuring an OTLP exporter. |

---

### MA-04: Fleet Health Monitoring

| Field           | Detail |
|-----------------|--------|
| **ID**          | MA-04 |
| **Category**    | Monitoring & Audit |
| **Description** | The pool manager tracks agent fleet concurrency, enforces hourly cost caps, and exposes real-time health metrics for operational monitoring. |
| **Implementation** | The pool manager (`robothor/engine/pool.py`) maintains a concurrency semaphore limiting simultaneous agent runs. Hourly cost is aggregated across all runs and checked against a configurable cap. When the cap is reached, new runs are queued until the next hour window. Fleet health metrics are available via the `/api/v2/stats` endpoint, including: active runs, queued runs, hourly cost, error rates, and per-agent statistics. The analytics module (`robothor/engine/analytics.py`) provides anomaly detection using rolling baseline comparison with 2-sigma flagging. |
| **Evidence** | Pool manager source; analytics module source and tests; `/api/v2/stats` API response; fleet health dashboard (Helm). |
| **Deployer Action** | Configure the hourly cost cap appropriate to the deployment budget. Set up alerting on the fleet health endpoint. |

---

## CM -- Change Management

### CM-01: Pre-Commit Hooks

| Field           | Detail |
|-----------------|--------|
| **ID**          | CM-01 |
| **Category**    | Change Management |
| **Description** | Pre-commit hooks prevent secrets from being committed to the repository. GitHub push protection provides a second layer of defense on the remote. |
| **Implementation** | The repository is configured with a gitleaks pre-commit hook that scans staged changes for secret patterns (API keys, tokens, passwords, private keys) before allowing a commit. The hook runs automatically on every `git commit`. If a secret pattern is detected, the commit is blocked with a descriptive error message. On the remote side, GitHub push protection scans pushes for known secret formats and blocks them at the server level. |
| **Evidence** | `.pre-commit-config.yaml` or git hooks directory showing gitleaks configuration; test by staging a file containing a dummy secret pattern and verifying the commit is blocked; GitHub repository settings showing push protection enabled. |
| **Deployer Action** | Ensure pre-commit hooks are installed (`pre-commit install`) on all developer workstations. Enable GitHub push protection on the remote repository. |

---

### CM-02: Agent Manifest Validation

| Field           | Detail |
|-----------------|--------|
| **ID**          | CM-02 |
| **Category**    | Change Management |
| **Description** | A validation script checks agent manifests against the canonical schema before deployment, catching configuration errors, invalid tool references, and missing secret references. |
| **Implementation** | The `scripts/validate_agents.py` script validates all agent manifests in `docs/agents/` against the schema defined in `docs/agents/schema.yaml`. Checks include: required fields present, valid tool names in `tools_allowed`, valid guardrail names, secret references resolvable, cron expression syntax, model references valid, and cross-references between agents (e.g., `reports_to` pointing to a real agent). The script exits non-zero on any validation failure. |
| **Evidence** | Validation script source; schema definition (`docs/agents/schema.yaml`); CI pipeline configuration showing validation as a required check; script output showing pass/fail for each agent. |
| **Deployer Action** | Run `python scripts/validate_agents.py` after any manifest change and before restarting the engine. Integrate into CI/CD pipeline. |

---

### CM-03: Nightwatch Code Review

| Field           | Detail |
|-----------------|--------|
| **ID**          | CM-03 |
| **Category**    | Change Management |
| **Description** | The Nightwatch system generates improvement PRs overnight. All PRs are created as drafts and require human review and merge approval, ensuring no autonomous code changes reach production without oversight. |
| **Implementation** | The Overnight PR agent (`docs/agents/overnight-pr.yaml`) runs on a nightly schedule. It analyzes failure patterns and improvement opportunities identified by the Failure Analyzer and Improvement Analyst agents. PRs are created with `gh pr create --draft --label nightwatch`, ensuring they appear as drafts in the GitHub UI. The agent has a `$2` cost cap per run and a maximum of 3 PRs per night. The `no_main_branch_push` guardrail (AC-05) prevents direct pushes. An auto-disable mechanism triggers after 3 consecutive PR rejections. |
| **Evidence** | Overnight PR agent manifest; GitHub PR list filtered by `nightwatch` label; PR history showing draft status and human merge actions; guardrail event logs. |
| **Deployer Action** | Assign a human reviewer for Nightwatch PRs. Establish a review SLA (e.g., reviewed within 1 business day). |

---

## ES -- Execution Safety

### ES-01: Guardrail Policy Engine (8 Policies)

| Field           | Detail |
|-----------------|--------|
| **ID**          | ES-01 |
| **Category**    | Execution Safety |
| **Description** | The guardrail engine enforces 8 distinct safety policies that can be assigned to any agent via its manifest. Policies cover destructive writes, network access, sensitive data, rate limiting, branch protection, command allowlisting, path restrictions, and desktop safety. |
| **Implementation** | Policies are implemented in `robothor/engine/guardrails.py`. Each policy is evaluated before tool execution (pre-hook) or after (post-hook). The 8 policies are: |

| Policy | Type | Behavior |
|--------|------|----------|
| `no_destructive_writes` | Pre-hook | Blocks file deletion, truncation, and overwrite of critical system files. |
| `no_external_http` | Pre-hook | Blocks HTTP requests to external domains not on an explicit allowlist. |
| `no_sensitive_data` | Post-hook | Scans output for secret patterns and blocks delivery if found (see DP-03). |
| `rate_limit` | Pre-hook | Enforces per-agent, per-tool call rate limits within a sliding window. |
| `no_main_branch_push` | Pre-hook | Blocks git operations targeting protected branches (see AC-05). |
| `exec_allowlist` | Pre-hook | Only permits shell commands matching the agent's `exec_allowlist` patterns. |
| `write_path_restrict` | Pre-hook | Restricts file write operations to approved directory paths. |
| `desktop_safety` | Pre-hook | Restricts computer-use actions to approved application windows and regions. |

| **Evidence** | Guardrail source code; `agent_guardrail_events` table; agent manifests showing guardrail assignments; test suite (`test_guardrails.py`). |
| **Deployer Action** | Assign appropriate guardrails to each agent manifest. All guardrails are opt-in per agent. Review the default policy set and adjust for organizational requirements. |

---

### ES-02: Docker Sandbox Isolation

| Field           | Detail |
|-----------------|--------|
| **ID**          | ES-02 |
| **Category**    | Execution Safety |
| **Description** | Agents with computer-use capabilities run inside ephemeral Docker containers, isolating their desktop interactions from the host system. |
| **Implementation** | Computer-use agent runs are executed inside Docker containers with: no network access (unless explicitly granted), read-only filesystem mounts for instruction files, a dedicated X11 display, and automatic cleanup on run completion. Container images are pre-built with the minimum required tooling. The `desktop_safety` guardrail (ES-01) provides an additional layer within the container. |
| **Evidence** | Docker configuration for computer-use agents; container lifecycle logs; `brain/agents/COMPUTER_USE.md` documentation; test suite. |
| **Deployer Action** | Ensure Docker is installed and configured. Review container resource limits (CPU, memory) for the deployment environment. |

---

### ES-03: Lifecycle Hooks

| Field           | Detail |
|-----------------|--------|
| **ID**          | ES-03 |
| **Category**    | Execution Safety |
| **Description** | Blocking lifecycle hooks run before and after tool execution, allowing external validation logic to prevent or modify tool calls. |
| **Implementation** | The hook system (`docs/hooks/`) supports `pre_tool_use` and `post_tool_use` hooks. Hooks are defined as external scripts or internal Python functions. `pre_tool_use` hooks receive the tool name and arguments and can return a BLOCK verdict to prevent execution. `post_tool_use` hooks receive the tool result and can modify or redact it before returning to the agent. Hooks are registered per agent in the manifest configuration. |
| **Evidence** | Hook documentation (`docs/hooks/`); hook execution traces in agent telemetry; test suite. |
| **Deployer Action** | Implement custom hooks for organization-specific validation requirements (e.g., PII redaction, compliance checks). |

---

### ES-04: Budget Enforcement

| Field           | Detail |
|-----------------|--------|
| **ID**          | ES-04 |
| **Category**    | Execution Safety |
| **Description** | Agent runs are subject to soft token budget caps. When a budget is exhausted, the engine strips tool schemas to force the agent toward completion rather than allowing unbounded execution. |
| **Implementation** | Budget configuration is defined in the agent manifest under the `v2:` block, with fields for `max_tokens` and `max_cost`. The runner tracks cumulative token usage and estimated cost throughout a run. When the soft budget is reached, the engine removes tool definitions from subsequent LLM calls, effectively forcing the agent to produce a final response without additional tool use. Budget metrics are recorded on the `agent_runs` record. The fleet-level hourly cost cap (MA-04) provides an additional aggregate limit. |
| **Evidence** | Agent manifest `v2:` budget fields; `agent_runs` table cost/token columns; runner source showing budget check logic; `/api/v2/stats` showing budget exhaustion counts. |
| **Deployer Action** | Set per-agent budgets in manifests appropriate to the agent's expected workload. Configure the fleet-level hourly cost cap in the pool manager. |

---

### ES-05: Stall Watchdog

| Field           | Detail |
|-----------------|--------|
| **ID**          | ES-05 |
| **Category**    | Execution Safety |
| **Description** | A watchdog monitors agent runs for inactivity and terminates runs that show no progress after a configurable timeout, preventing resource exhaustion from hung agents. |
| **Implementation** | The stall watchdog tracks the last activity timestamp for each active run. Activity is defined as: a tool call, an LLM response, or a checkpoint write. If no activity is detected within the configured timeout (default varies by agent), the watchdog cancels the run and records a stall event. Watchdog state is persisted in `brain/memory/engine-watchdog-state.json`. The heartbeat system also monitors for fleet-wide stalls and can trigger recovery actions. |
| **Evidence** | Watchdog state file; `agent_runs` table showing stall-terminated runs; runner source showing activity timestamp updates; heartbeat instruction file. |
| **Deployer Action** | Review default stall timeout values. Adjust per agent based on expected execution duration (e.g., longer timeouts for research-heavy agents). |

---

## IR -- Incident Response

### IR-01: Dead Letter Queue

| Field           | Detail |
|-----------------|--------|
| **ID**          | IR-01 |
| **Category**    | Incident Response |
| **Description** | Failed events that cannot be processed after multiple retries are moved to a dead letter queue (DLQ) for manual investigation, preventing event loss and enabling recovery. |
| **Implementation** | The event processing pipeline implements a retry policy: failed events are retried up to 3 times with exponential backoff. Events that exhaust all retries are moved to a DLQ stream. DLQ entries include the original event payload, all error messages from retry attempts, and timestamps. Operators can inspect and replay DLQ entries through the API or CLI. |
| **Evidence** | DLQ stream contents; retry logic in event processing source; API endpoints for DLQ inspection; monitoring dashboards showing DLQ depth. |
| **Deployer Action** | Monitor DLQ depth as a key operational metric. Establish a runbook for investigating and replaying DLQ entries. |

---

### IR-02: Graduated Error Escalation

| Field           | Detail |
|-----------------|--------|
| **ID**          | IR-02 |
| **Category**    | Incident Response |
| **Description** | The escalation engine implements graduated recovery actions as error counts increase within a single agent run, preventing cascading failures while maximizing recovery opportunities. |
| **Implementation** | The escalation module (`robothor/engine/escalation.py`) defines three escalation tiers based on consecutive error count within a run. At 3 errors: the engine injects error context into the next LLM prompt and suggests an alternative approach. At 4 errors: the engine strips non-essential tools and constrains the agent to core operations. At 5 errors: the run is terminated and a failure record is created for the Failure Analyzer (IR-03). Each escalation tier is logged as a structured event. |
| **Evidence** | Escalation module source and tests; agent run records showing escalation events; telemetry spans for escalation actions. |
| **Deployer Action** | Review escalation thresholds and adjust if needed. Ensure the Failure Analyzer agent is active to process terminated runs. |

---

### IR-03: Failure Analyzer Agent

| Field           | Detail |
|-----------------|--------|
| **ID**          | IR-03 |
| **Category**    | Incident Response |
| **Description** | A dedicated agent runs every 2 hours to analyze recent failures, classify root causes, and create remediation tasks, enabling systematic incident follow-up. |
| **Implementation** | The Failure Analyzer agent (`docs/agents/failure-analyzer.yaml`) queries the analytics module for recent failures, classifies each as transient, configuration, code, or unknown, and creates CRM tasks assigned to the Overnight PR agent for automated remediation or to a human operator for manual investigation. The agent uses `delivery: none` -- it operates silently and communicates only through the task system. Failure patterns are clustered by agent and error type using the `get_failure_patterns()` analytics function. |
| **Evidence** | Failure Analyzer manifest and instruction file; CRM tasks created by the analyzer; analytics module source (`robothor/engine/analytics.py`); agent run history showing 2-hour execution cadence. |
| **Deployer Action** | Ensure the Failure Analyzer agent is enabled. Review generated tasks regularly to identify systemic issues. |

---

## Control Summary Matrix

| ID    | Control Name                    | Category           | Type       | Automated |
|-------|---------------------------------|--------------------|------------|-----------|
| AC-01 | Cloudflare Zero Trust           | Access Control     | Preventive | Yes       |
| AC-02 | API Key Authentication          | Access Control     | Preventive | Yes       |
| AC-03 | RBAC Middleware                  | Access Control     | Preventive | Yes       |
| AC-04 | Tool Permission Layering        | Access Control     | Preventive | Yes       |
| AC-05 | Protected Branch Safety          | Access Control     | Preventive | Yes       |
| DP-01 | SOPS Encryption                 | Data Protection    | Preventive | Yes       |
| DP-02 | Vault Credential Storage        | Data Protection    | Preventive | Yes       |
| DP-03 | No-Sensitive-Data Guardrail     | Data Protection    | Detective  | Yes       |
| DP-04 | Tenant Isolation                | Data Protection    | Preventive | Yes       |
| MA-01 | Audit Logging                   | Monitoring & Audit | Detective  | Yes       |
| MA-02 | Guardrail Event Logging         | Monitoring & Audit | Detective  | Yes       |
| MA-03 | Agent Telemetry                 | Monitoring & Audit | Detective  | Yes       |
| MA-04 | Fleet Health Monitoring         | Monitoring & Audit | Detective  | Yes       |
| CM-01 | Pre-Commit Hooks                | Change Management  | Preventive | Yes       |
| CM-02 | Agent Manifest Validation       | Change Management  | Preventive | Yes       |
| CM-03 | Nightwatch Code Review          | Change Management  | Detective  | Partial   |
| ES-01 | Guardrail Policy Engine         | Execution Safety   | Preventive | Yes       |
| ES-02 | Docker Sandbox Isolation        | Execution Safety   | Preventive | Yes       |
| ES-03 | Lifecycle Hooks                 | Execution Safety   | Preventive | Yes       |
| ES-04 | Budget Enforcement              | Execution Safety   | Preventive | Yes       |
| ES-05 | Stall Watchdog                  | Execution Safety   | Detective  | Yes       |
| IR-01 | Dead Letter Queue               | Incident Response  | Corrective | Yes       |
| IR-02 | Graduated Error Escalation      | Incident Response  | Corrective | Yes       |
| IR-03 | Failure Analyzer Agent          | Incident Response  | Corrective | Yes       |

---

## Revision History

| Date       | Version | Author   | Change |
|------------|---------|----------|--------|
| 2026-03-30 | 1.0     | Platform | Initial control inventory |
