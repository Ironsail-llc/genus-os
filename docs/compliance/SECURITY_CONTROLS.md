# Genus OS security controls inventory

Version: 1.1
Last updated: 2026-07-13
Scope: repository capabilities and required deployment controls

## Important use boundary

This is a control inventory and evidence guide, not a certification,
attestation, penetration-test report, or statement that every listed control is
operating in a particular environment. A control is effective only when its
required configuration, external service, organizational procedure, monitoring,
and evidence are present in the deployed boundary.

Status meanings:

- **Implemented** — the named repository path enforces the core behavior.
- **Shared** — Genus supplies a mechanism and the deployer must configure and
  operate it.
- **Partial** — useful coverage exists, with a material documented gap.
- **External** — the repository cannot implement the control by itself.

## Access control

### AC-01 — Human authentication and session boundary

- **Status:** Shared.
- **Implementation:** Auth.js performs organization OIDC sign-in for the
  dashboard. The Bridge exchanges verified claims through an authenticated SSO
  endpoint and issues short-lived signed access tokens plus rotating hashed
  refresh tokens. The dashboard request proxy denies private pages/APIs by
  default. Sign-in requires an `email_verified=true` claim, an active account,
  and an explicit issuer/subject binding for an existing user; email matching
  does not silently link accounts, and just-in-time users do not receive a
  privileged role. A failed Bridge exchange or refresh clears dashboard
  authority instead of leaving a partial session.
- **Evidence:** `app/src/lib/auth.ts`, `app/src/proxy.ts`,
  `crm/bridge/routers/auth.py`, `robothor/auth/` and auth tests.
- **Limitations/actions:** The operator must configure the IdP, redirect URIs,
  secrets, issuer allowlist, role mapping, deprovisioning, break glass, and
  periodic access reviews. Cloudflare Access is optional defense in depth, not
  an inherited control.

### AC-02 — Signed API identity

- **Status:** Implemented for Bridge and Engine; Partial platform-wide.
- **Implementation:** Bridge private routes require an issuer/audience-bound
  JWT with expiry, issued-at time, unique token ID, tenant, role, type, and
  scopes. The Engine independently verifies signed audience, tenant, and
  `engine:*` scopes on every non-probe HTTP route and authenticates the IDE
  WebSocket before accepting it; channel webhooks retain their route-specific
  HMAC boundary. Bridge service tokens cannot be replayed as Engine authority.
  Invalid supplied credentials are rejected on protected surfaces. Legacy
  identity headers or a synthetic Engine identity are allowed only in explicit
  insecure development mode on a loopback bind outside production.
- **Evidence:** `robothor/auth/tokens.py`, `robothor/auth/deps.py`,
  `robothor/auth/runtime.py`, `robothor/engine/auth.py`, Engine auth tests, and
  `crm/bridge/middleware.py`.
- **Limitations/actions:** Orchestrator and vision do not yet independently
  verify the same identity contract. Keep them private ClusterIP/loopback
  services behind NetworkPolicy; direct exposure remains a production blocker.

### AC-03 — Tenant, role, scope, and agent binding

- **Status:** Implemented at the Bridge/Engine HTTP boundaries; Partial defense
  in depth.
- **Implementation:** Tenant and service-agent identity come from verified
  claims. Conflicting `X-Tenant-Id`/`X-Agent-Id` headers are rejected. Read/write
  scopes and administrative/audit roles are checked before sensitive Bridge
  routes. The Engine rejects tokens for a tenant other than its configured
  tenant and carries verified user/role context into tool dispatch. Empty roles
  are deny-by-default. Appliance-global installed-agent operations are limited
  to the primary tenant, and non-primary legacy memory ingestion fails closed.
- **Evidence:** Bridge/Engine auth middleware, permission tests, and
  `test_auth_hardening.py`.
- **Limitations/actions:** Core memory pipelines are not fully tenant-aware.
  Validate every DAL query and material route, and add PostgreSQL row-level
  security or an equivalent independent layer before hostile multi-tenant use.

### AC-04 — Agent tool and stream authorization

- **Status:** Shared.
- **Implementation:** Agent manifests constrain visible/dispatchable tools and
  declared streams; the runner applies allow/deny lists and guardrails.
- **Evidence:** manifest schema, runner/tool registry, event capabilities, and
  permission tests.
- **Limitations/actions:** Least-privilege manifests and domain-specific
  policies remain operator responsibilities. Tool authorization does not prove
  that every downstream provider applies equivalent authorization.

### AC-05 — Protected production change

- **Status:** Shared.
- **Implementation:** The `no_main_branch_push` guardrail blocks selected agent
  git operations to protected names; Nightwatch is designed to open draft PRs.
  Production release workflows run tests and smoke checks and roll back a failed
  post-deploy smoke test.
- **Evidence:** guardrail tests, Nightwatch manifests, CI/release workflows.
- **Limitations/actions:** Repository rulesets, required reviewers/CODEOWNERS,
  CI requirements, deploy credentials, and separation of duties must be
  enforced in GitHub and the deployment platform.

## Data protection

### DP-01 — Secret storage and rotation

- **Status:** Shared.
- **Implementation:** Helm materializes separate database, cache, signing,
  SSO/OIDC, dashboard, and provider paths through HashiCorp Vault Secrets
  Operator. Per-component references are deny-listed across trust boundaries;
  dashboard and migrations cannot request provider/signing or non-database
  classes respectively. Rotation restarts only consuming deployments. Dashboard
  model requests carry the verified user identity to the Engine; only the
  Engine selects the model and reads provider credentials. A systemd deployment
  can use SOPS + age/tmpfs.
- **Evidence:** Helm VaultStaticSecret templates/values, SOPS scripts, secret
  scanning configuration.
- **Limitations/actions:** Provisioning, KMS/unseal policy, access logs, backup,
  rotation, revocation, and break glass are external. No document may claim
  secrets never touch persistent media without verifying the deployed path,
  crash dumps, swap, logs, and backups.

### DP-02 — Payment-data minimization

- **Status:** Implemented domain boundary; no live adapter.
- **Implementation:** Customer payments accept opaque provider tokens only;
  operational spend accepts opaque virtual-card references only. Raw PAN and
  CVC/CVV-shaped fields are rejected. Spend decisions are deny-default and
  policy/authority bound. Organization ownership of a card does not weaken this
  boundary.
- **Evidence:** `robothor/entity/`, its tests, and `PAYMENT_DATA.md`.
- **Limitations/actions:** This does not establish PCI compliance. Durable
  storage, provider integration, webhook/reconciliation controls, an actual
  scope assessment, and operating evidence are still required.

### DP-03 — Sensitive-output detection

- **Status:** Partial.
- **Implementation:** The `no_sensitive_data` post-execution policy detects a
  bounded set of common credential patterns and blocks delivery when enabled.
- **Evidence:** `robothor/engine/guardrails.py` and guardrail tests.
- **Limitations/actions:** Pattern scanning is not DLP, data classification, or
  proof against encoded/contextual leakage. Add organization patterns,
  integration-level redaction, egress controls, and testing.

### DP-04 — Transport and network boundary

- **Status:** Shared.
- **Implementation:** Production/staging values use PostgreSQL
  `sslmode=verify-full`, private ingress, and default-deny ingress/egress
  NetworkPolicies with component/service selectors and destination-specific
  CIDR/port allowances. Unrestricted CIDRs fail Helm rendering, and dashboard
  external egress is limited to the identity-provider destination class.
- **Evidence:** Helm values/templates/tests and database TLS tests.
- **Limitations/actions:** Install a trusted database CA, provide exact managed
  service, Kubernetes API, IdP, and API/egress-proxy CIDRs in Layer 3, configure
  ingress TLS, and decide whether encrypted east-west traffic/service mesh is
  required. Internal service HTTP is not automatically encrypted.

### DP-05 — Encrypted recovery points

- **Status:** Implemented mechanism; Shared operation.
- **Implementation:** Snapshots stream an AES-256-GCM encrypted archive by
  default, derive keys with scrypt, checksum artifacts/files, reject symlinks,
  publish atomically, and require explicit confirmation for restore mutation.
- **Evidence:** `robothor/snapshot.py`, snapshot CLI/tests, and restore runbook.
- **Limitations/actions:** Schedule, off-site replication, object lock,
  passphrase custody, deletion, access logs, and restore drills are deployment
  responsibilities.

## Monitoring and audit

### MA-01 — Structured audit events

- **Status:** Partial.
- **Implementation:** Authentication, CRM/task, agent-run, tool/guardrail, and
  selected system paths emit structured database events with actor/correlation
  context.
- **Evidence:** audit logger, database schema, route/runner calls, and tests.
- **Limitations/actions:** Do not claim all actions are captured. Define the
  material-event catalog, add negative/coverage tests for each workflow, export
  to immutable storage, monitor write failure, and set retention.

### MA-02 — Guardrail evidence

- **Status:** Implemented where the engine evaluates a configured policy.
- **Implementation:** Allow/block/warn results can be persisted with policy and
  run context.
- **Limitations/actions:** Unknown policies are logged but can leave the
  intended control unenforced; manifest validation and alerts must treat that as
  a deployment error. Custom/downstream side effects need separate evidence.

### MA-03 — Agent telemetry

- **Status:** Partial.
- **Implementation:** Agent runs carry trace/correlation IDs, timing, model,
  token/cost, step, and outcome metadata through core execution paths.
- **Limitations/actions:** Verify exporter configuration, sampling, redaction,
  availability, and workflow coverage. Traces can contain sensitive data and
  need classification/retention controls.

### MA-04 — Health and release observability

- **Status:** Implemented mechanism; Shared operation.
- **Implementation:** Services separate process liveness from dependency-aware
  readiness. Production deploy waits for Argo health, smokes live/ready, and
  rolls back failed smoke checks.
- **Limitations/actions:** Configure external SLI collection and paging. A
  green readiness response is not historical availability evidence.

## Change and execution safety

### CM-01 — Repository/release gates

- **Status:** Shared.
- **Implementation:** CI/release workflows run lint, type checking, unit and
  Bridge tests, real-PostgreSQL migrations/integration, frontend unit/build/E2E,
  Helm tests/policy checks, secret/dependency scans, container critical-CVE
  blocking, version consistency, and deployment smoke rollback.
- **Limitations/actions:** Protect the branch and require the checks. Action and
  dependency pinning, signed images, provenance, and admission verification are
  P1 work.

### CM-02 — Canonical schema change

- **Status:** Implemented.
- **Implementation:** One runner executes the 83-entry manifest-ordered
  migration chain, locks concurrent execution, records full IDs/checksums,
  refuses drift or unknown history, and supports verification-only status.
  Migration 023 renames legacy memory tables into explicit archives rather than
  dropping them. Migration 035 requires 30 populated achievement-score days
  and archives complete legacy rows before dropping superseded columns.
- **Evidence:** `robothor/db/migrate.py`, migration manifest, package build and
  real-database tests.
- **Limitations/actions:** Changes are forward-only. Managed PostgreSQL must
  pre-provision or permit `vector`, `uuid-ossp`, `citext`, and `pgcrypto`.
  Back up, test the full chain against a production clone, compare material row
  counts, and rehearse restore before destructive/large migrations; monitor
  runtime and locks.

### CM-03 — Autonomous code boundary

- **Status:** Implemented policy contract; Shared enforcement.
- **Implementation:** Entity authority makes production change,
  self-modification, and permission expansion non-delegable. Agent-generated
  code is staged/tested through draft PRs; opening a PR does not authorize merge
  or deployment.
- **Limitations/actions:** Human review, branch rules, deploy separation, and
  credential scope must make bypass impossible outside the agent prompt.

### ES-01 — Runtime guardrail engine

- **Status:** Shared.
- **Implementation:** Twelve known policies cover destructive writes, external
  HTTP, branches, rate limits, command/path restriction, desktop actions,
  sensitive output, approvals, inbound-only communication, meeting proposals,
  and recent-config reversal. Three baseline policies apply unless explicitly
  opted out.
- **Limitations/actions:** Guardrails are scoped code controls, not a security
  sandbox. Assign policies by threat model and test bypasses at each tool and
  provider boundary.

### ES-02 — Process/container isolation

- **Status:** Partial.
- **Implementation:** Kubernetes workloads run non-root with read-only roots,
  dropped capabilities, seccomp, resource bounds, and constrained volumes.
  Selected computer-use workflows can run in containers/virtual displays.
- **Limitations/actions:** Not every tool call runs in a fresh sandbox. Host
  Docker/systemd permissions, mounts, device access, and outbound networking
  must be reviewed separately.

### ES-03 — Budgets, watchdogs, and escalation

- **Status:** Partial.
- **Implementation:** Per-run/fleet budgets, timeouts, stall detection,
  checkpoints, error escalation, and failure-analysis paths bound common
  failures.
- **Limitations/actions:** Provider-side caps and infrastructure quotas are
  required. Some budgets guide graceful completion rather than guaranteeing a
  hard billing ceiling.

### ES-04 — Treasury and authority policy

- **Status:** Implemented domain contract; no live money movement.
- **Implementation:** Ownership, allowlists, caps, approvals, idempotency,
  authority tiers, redaction, and a hash-chained local ledger deny unauthorized
  proposals.
- **Limitations/actions:** The local ledger/decision store is not durable or
  sufficient for production finance. See the production TODO.

### ES-05 — Model-generated dashboard isolation

- **Status:** Implemented read-only boundary.
- **Implementation:** The model produces bounded static HTML for display only.
  Server and client sanitizers reject scripts, frames, external resources,
  links, forms, interactive controls, event handlers, and action channels. The
  document runs in a deny-default CSP/sandbox without same-origin authority.
  Dashboard model calls go only through the authenticated, same-tenant Engine
  completion endpoint with server-controlled provider parameters; the Next.js
  process has no provider credentials or model selection.
- **Evidence:** `app/src/lib/dashboard/`, `app/src/components/canvas/`,
  `app/src/lib/engine/server-client.ts`,
  `robothor/engine/dashboards/completions.py`, and security tests.
- **Limitations/actions:** Prompt/output controls are defense in depth, not a
  reason to send unrestricted sensitive data to a model provider. Keep generated
  mutations disabled until a typed, allowlisted, confirmed, idempotent action
  contract has its own authorization and audit tests.

## Incident response and availability

### IR-01 — Failure containment and replay

- **Status:** Partial.
- **Implementation:** Event retries/DLQ patterns, graduated agent escalation,
  checkpoints, and failure-analysis tasks support diagnosis and selected replay.
- **Limitations/actions:** Define incident roles, severity, communications,
  evidence preservation, legal notification, and tested replay procedures.

### IR-02 — Backup and restore

- **Status:** Shared.
- **Implementation:** The snapshot contract captures PostgreSQL and allowlisted
  entity workspace state, verifies compatibility, and defaults restore to a
  non-mutating plan.
- **Limitations/actions:** The stated 15-minute RPO and 60-minute RTO are targets
  until scheduled off-site backups and a measured drill prove them.

### IR-03 — Deployment rollback

- **Status:** Partial.
- **Implementation:** A failed production live/ready smoke first reverts the
  GitOps promotion commit and syncs ArgoCD, with an emergency Argo rollback as a
  fallback. Migrations fail closed and application pods wait for completion.
- **Limitations/actions:** A code rollback cannot automatically reverse a
  forward-only schema migration or external side effect. Every release needs a
  compatible rollback/forward-fix plan.

### AV-01 — Availability architecture

- **Status:** Partial.
- **Implementation:** Stateless services can run multiple replicas with
  anti-affinity/PDBs. Readiness removes unhealthy pods from service.
- **Limitations/actions:** The engine intentionally remains one replica to avoid
  duplicate scheduling. Without leader election or tested active/passive
  failover, the repository cannot substantiate 99.9% availability.

## Required environment evidence

At minimum, retain:

- approved architecture/data-flow/threat models and asset classification;
- IdP, access-review, Vault/KMS, TLS, NetworkPolicy, ingress, and firewall
  configuration evidence;
- required CI/reviewer results, image/SBOM/vulnerability records, migration
  ledger, deployment and rollback records;
- snapshot-age monitoring, verification logs, off-site-copy evidence, and
  measured restore drills;
- audit/alert retention and incident exercises;
- vendor assessments, DPAs/BAAs, payment scope determination, and other legal
  records applicable to the deployed data flow.

See `PRODUCTION_HARDENING_TODO.md`, `PAYMENT_DATA.md`, the HIPAA/SOC 2 mappings,
and the snapshot runbook. Those documents remain planning aids rather than
certification evidence on their own.

## Revision history

| Date | Version | Change |
|---|---:|---|
| 2026-03-30 | 1.0 | Initial inventory |
| 2026-07-13 | 1.1 | Reconciled controls with implemented scope, gaps, recovery, payment, and deployment responsibilities |
