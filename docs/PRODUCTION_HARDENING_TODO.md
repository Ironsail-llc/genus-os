# Genus OS production hardening TODO

Last updated: 2026-07-13

This is the source-of-truth delivery checklist for the production-hardening
work. A checked implementation item means the control exists in the current
hardening workspace and has focused test evidence. It does **not** mean the
clean PR branch has passed its final gate, that an operator control is present,
or that a deployed environment is certified.

## Delivery status

- **Workspace:** the reviewed hardening path set is on a clean branch based on
  current `origin/main`; the complete local gate is green.
- **Pull request:** not opened yet. The clean draft branch is ready, but the
  local GitHub CLI and HTTPS Git credentials returned `401 Bad credentials` on
  2026-07-13; SSH has no authorized key. Reauthentication with repository write
  access is required before the branch can be pushed and the draft PR opened.
- **Production:** blocked by every unchecked P0 item below.
- **Authorization:** prepare and submit the draft PR for review. Do not merge,
  deploy, promote an image, or change a production environment as part of this
  work.

Priority meanings:

- **P0:** required before a production go-live.
- **P1:** required to meet the stated 99.9% availability target or materially
  reduce operational/security risk after the first controlled deployment.
- **P2:** expands the Entity Kernel without weakening its authority boundaries.

The 99.9% availability, 15-minute RPO, and 60-minute RTO figures are targets
that require measured deployment evidence. They are not current service claims.

## P0 implementation completed in the hardening workspace

- [x] Replace partial/ambiguous database setup with one canonical,
  checksum-verified, advisory-locked migration chain.
  - Acceptance evidence: a fresh database applies all 83 migrations; rerun is
    a no-op; unknown or drifted history fails closed; the packaged wheel contains
    the same chain used by CLI and application startup.
- [x] Preserve upgrade data at the two destructive legacy cutovers.
  - Migration 023 renames legacy short/long memory tables to explicit archives
    instead of dropping them.
  - Migration 035 refuses a populated cutover without 30 distinct days of the
    replacement score and archives complete source rows before dropping legacy
    columns.
  - This code safeguard does not replace the production-clone rehearsal below.
- [x] Separate process liveness from dependency-aware readiness for the engine,
  Bridge, orchestrator, and dashboard.
- [x] Make the production agent workspace persistent and require a valid `main`
  fleet before the engine becomes ready.
- [x] Harden Kubernetes workload defaults with non-root/read-only containers,
  scratch volumes, resource limits, disruption budgets/anti-affinity where
  safe, and default-deny network-policy foundations.
- [x] Require dashboard SSO and signed Bridge JWTs by default; bind tenant,
  role, scope, audience, and service-agent identity to verified claims.
- [x] Make dashboard session validation cryptographic rather than accepting the
  presence of an opaque cookie; private pages and APIs deny by default.
- [x] Harden the SSO account lifecycle.
  - Require `email_verified=true` and an active local account.
  - Match an existing account by exact OIDC issuer plus subject; never auto-link
    an existing account by email alone.
  - Keep just-in-time accounts non-privileged, consume refresh tokens atomically,
    and clear session authority when Bridge exchange or refresh fails.
- [x] Apply route-specific Bridge authorization and tenant binding for vault,
  integration, installed-agent, memory-administration, and task-approval
  operations. Appliance-global agent operations are restricted to the primary
  tenant, and unsafe non-primary memory operations fail closed until the core
  store is tenant-aware.
- [x] Remove trusted identity-header behavior except in an explicit,
  loopback-only, non-production development mode.
- [x] Fail closed when required production authentication/runtime secrets are
  absent; isolate database, cache, signing, SSO/OIDC, dashboard, and provider
  trust classes into independently rotatable Vault paths.
- [x] Enforce per-workload Vault projections and ServiceAccounts.
  - Dashboard may mount only `dashboard-auth` and `bridge-sso`; migrations may
    mount only `database`; Helm rendering rejects a privileged cross-reference.
  - The migration Job, dashboard, and Python application containers receive no
    Kubernetes API token. Separate Python ServiceAccounts receive only `get`
    on the exact release migration Job, via short-lived tokens projected solely
    into their watcher init containers.
- [x] Replace blanket external egress with a default-deny component graph and
  destination-specific CIDR/port policies. DNS and Traefik are pod-selector
  scoped; unrestricted IPv4/IPv6 CIDRs fail Helm rendering; production and
  staging require exact Layer-3 managed-service/API or egress-proxy CIDRs.
- [x] Move blocking Bridge DAL/filesystem/subprocess handlers into FastAPI's
  worker threadpool and add an event-loop regression test.
- [x] Fix database-pool acquisition timeout cleanup and add a connection
  timeout.
- [x] Constrain model-generated dashboards to read-only presentation.
  - Treat conversation/data context as bounded untrusted input.
  - Reject scripts, event handlers, network access, forms, frames, action
    channels, and mutation APIs; sanitize before first render and apply a
    deny-default CSP inside a sandboxed, non-same-origin frame.
  - Native, reviewed application actions remain a separate server-side
    allowlisted path; generated HTML has no access to that path.
- [x] Contain agent bundle installation and require content integrity.
  - Enforce strict identifiers and canonical contained paths, reject symlinked
    path components and unsafe archive members, and bound archive size/count.
  - Remote installs and updates fail before download unless trusted Hub metadata
    supplies the exact lowercase SHA-256 digest; downloaded bytes are verified
    before extraction.
- [x] Remove the stale-test quarantine and repair the quarantined tests.
- [x] Add a snapshot/restore CLI with encrypted, checksummed archives,
  verification, dry-run restore, explicit destructive confirmation, and
  retention controls.
- [x] Add the Entity Kernel treasury boundary: customer payment tokens only,
  Genus virtual-card tokens/references only, deny-default spend policy,
  authority tiers, idempotency, redaction, and a tamper-evident local ledger.
  Raw PAN, full track data, PIN data, and CVC/CVV are outside both contracts.
- [x] Make release deployment a post-build promotion: semantic release updates
  metadata, both versioned images must build and pass blocking scans, and only
  then may a separate GitOps commit promote production values. Smoke failure
  reverts that promotion commit, with an emergency Argo rollback fallback.
- [x] Block critical container vulnerabilities, make Kubernetes lint
  release-blocking, add browser E2E tests, and add production smoke-test
  rollback.
- [x] Synchronize product version `1.10.0` across Python, Helm, dashboard,
  runtime health responses, and release automation.

## P0 implementation/integration completed

- [x] Complete independent Engine authentication and authorization for private
  HTTP and WebSocket entry points, propagate verified caller/tenant context to
  the runner, and prove an empty or missing role/scope denies rather than
  allowing tool dispatch.
- [x] Route dashboard AI generation/welcome calls through an authenticated
  backend service. The hardened dashboard intentionally receives no
  `OPENROUTER_API_KEY` or other model-provider secret; those BFF handlers now
  call the authenticated Engine completion boundary with verified caller
  context.
- [x] Reconcile the final architecture, security-control, Helm, and operating
  documentation against the integrated implementation and rendered chart.

There are no open repository-implementation P0 items. The clean-branch gate,
CI image builds, and deployment/operator P0 controls below remain deliberately
open and block a production go-live.

## Verification required before the draft PR is marked ready for review

- [x] Run the full Python unit suite with coverage at or above the current 60%
  floor after every hardening slice is integrated.
  - Integrated-workspace result: 4,680 passed, 7 skipped, 184 deselected;
    64.64% coverage.
- [x] Run the full Bridge suite, including account-lifecycle, route-scope,
  tenant-isolation, bundle-integrity, and concurrency tests.
  - Integrated-workspace result: 235 passed, 2 skipped.
- [x] Apply/check the canonical migration chain and run all integration tests
  against a clean real PostgreSQL + pgvector database.
  - Integrated-workspace result: all 83 migrations applied without drift, all
    19 required tables present, and 11 integration tests passed. A built wheel
    contains the canonical 83 SQL files plus its manifest.
- [x] Run the legacy-to-current migration fixture and verify the migration 023
  and 035 archive tables and refusal conditions against real PostgreSQL.
  - Covered by the real-PostgreSQL upgrade-safety integration lane above.
- [x] Run dashboard lint, TypeScript, all Vitest tests, a production standalone
  build, and all Playwright flows against that exact standalone artifact.
  - Integrated-workspace result: lint had 0 errors (14 existing warnings),
    TypeScript passed, 408 Vitest tests passed, the production build passed,
    and 23 Playwright tests passed against an isolated standalone server.
- [x] Run Helm lint, all chart assertions, production rendering, schema
  validation where available, and kube-linter with zero findings.
  - Integrated-workspace result: strict lint passed for default, staging,
    production, and local values; 11 suites/100 assertions passed; all 114
    rendered resources passed strict kubeconform; kube-linter found 0 issues.
- [x] Parse/action-lint all workflows, validate version consistency, run
  gitleaks, and inspect the final diff for accidental credentials or owner
  files.
  - Clean-branch result: all 9 workflow files parsed and passed actionlint;
    every external action is pinned to a full commit SHA; product version
    `1.10.0` is consistent; Gitleaks scanned 1.03 GB with no findings; the
    initial 247-path transfer matched the reviewed allowlist; the final path set
    adds only the narrow pre-commit metadata allowlist correction, and the
    owner-path denylist remained empty.
- [x] Exercise snapshot create/list/verify/restore-dry-run/prune against a
  representative database and workspace; record duration and archive size.
  - Integrated-workspace result: an AES-256-GCM snapshot completed in 0.35s at
    52.5 KiB; list/verify and database/workspace restore dry-runs passed;
    retention dry-run/confirmation and unsafe-boundary refusals passed.
- [x] Re-run this entire gate on the clean PR branch, not only in the integration
  workspace.
  - Clean-branch result: 4,496 Python tests passed, 27 skipped, 184 deselected,
    with 64.30% coverage; 235 Bridge tests passed and 2 skipped; all 11 real
    PostgreSQL integration tests passed; 83 migrations and 19 required tables
    checked without drift; Ruff, mypy (604 files), and the zero-exception
    dependency audit passed; 408 Vitest and 23 isolated standalone Playwright
    tests passed; the production frontend build passed; all 100 Helm assertions,
    114 strict schema validations, and kube-linter passed; the encrypted
    snapshot drill completed in 0.40s at 52.5 KiB with verify/restore dry-runs,
    retention, and unsafe-boundary checks passing.
- [ ] Confirm both Docker images build in CI. Local Docker validation is not
  available in the current shell because it cannot access the Docker socket.

## Deployment/operator P0 go-live blockers

The deployment operator owns these controls unless the draft PR assigns a named
owner. None can be converted into a compliance or reliability claim by checking
repository tests alone.

- [ ] Pre-provision the managed PostgreSQL extensions required by the canonical
  chain: `vector`, `uuid-ossp`, `citext`, and `pgcrypto`. Confirm the production
  database role may use them without granting the runtime unnecessary extension
  or superuser authority.
- [ ] Populate every rendered component-specific Vault path with exactly the
  keys required by that workload. At minimum, provision the database settings,
  `GENUS_AUTH_SIGNING_KEY` (32+ bytes), `GENUS_BRIDGE_SSO_SECRET`,
  `GENUS_OIDC_ISSUERS`, `AUTH_SECRET`, and the OIDC client settings.
- [ ] Update the environment Layer-3 chart values to the new named Vault paths
  and per-component ServiceAccounts before applying this chart; remove the
  legacy chart-wide `credentials`/`runtime` Secret assumptions.
- [ ] Configure the organization IdP, redirect URIs, issuer allowlist,
  deprovisioning, break-glass flow, session revocation, and an access-review
  owner. Pre-bind every privileged account using the exact trusted OIDC
  **issuer plus subject**; verified email, groups, or JIT creation alone must
  not establish privileged authority.
- [ ] Seed and validate the persistent workspace, including the `main` manifest
  and every referenced instruction/bootstrap file.
- [ ] Configure the trusted Agent Hub metadata source to publish a lowercase
  64-hex-character SHA-256 digest for every downloadable bundle. Missing,
  uppercase, malformed, or mismatched digests intentionally block install and
  update.
- [ ] Install the trusted PostgreSQL CA chain and prove
  `sslmode=verify-full` succeeds against the production endpoint.
- [ ] Supply exact destination CIDRs for external database, Redis, model,
  identity, messaging, payment, and other enabled providers, plus the exact
  `kubernetes.default` Service IP used by migration watchers, or route dynamic
  domain-based traffic through an authenticated, monitored egress proxy. Do
  not approve `0.0.0.0/0` or `::/0` as a production shortcut.
- [ ] Configure private ingress/TLS and alerting for `/ready`, snapshot age,
  migration failure, auth denial spikes, queue depth, error rate, latency, and
  resource saturation.
- [ ] Create a production-equivalent database backup, restore it into an
  isolated production clone, run the complete upgrade there, and compare
  critical-table row counts/checksums plus the migration 023/035 archives
  before authorizing the real upgrade. Rehearse and time rollback/restore from
  that same backup.
- [ ] Schedule encrypted snapshots at least every 15 minutes, replicate them to
  a separate account/failure domain with versioning or object lock, and alert
  before the 15-minute RPO target is breached.
- [ ] Complete an isolated restore drill in 60 minutes or less and record the
  measured RPO/RTO. Repeat at least quarterly and after material schema or
  restore changes.
- [ ] Configure protected branches and require the CI, integration, frontend,
  Helm, security, and release-gate checks before merge or deploy.
- [ ] Complete data-flow, threat-model, vendor-risk, retention/deletion,
  incident-response, and legal/compliance reviews for the actual deployment.
- [ ] Execute required DPAs/BAAs and determine PCI scope with qualified advisors
  for every external model, communication, cloud, and payment vendor.

## Known gaps after the first controlled release

- [ ] P1 — Add independent application-layer identity verification to the
  orchestrator and vision services before permitting direct exposure; private
  ClusterIP/loopback plus NetworkPolicy is the current compensating boundary.
- [ ] P1 — Replace the shared symmetric service-token signing key with
  asymmetric issuer/verification keys, key IDs, overlap rotation, and a tested
  revocation/compromise runbook to reduce verifier blast radius.
- [ ] P1 — Make CI install and test the exact reviewed lock across supported
  Python versions, and emit an SBOM for each release artifact, so dependency
  audit evidence and production image contents are reproducible.
- [ ] P1 — Remove the single-writer engine availability limit with tested leader
  election or active/passive failover. One engine replica cannot substantiate a
  99.9% service commitment.
- [ ] P1 — Make the core memory ingestion and pipeline tenant-aware, then replace
  the current fail-closed restriction for non-primary tenants with tested
  isolation.
- [ ] P1 — Add PostgreSQL point-in-time recovery, automated restore rehearsals,
  and failure-injection/chaos tests in addition to application snapshots.
- [ ] P1 — Raise the coverage floor in measured increments (70%, then 80%) and
  add risk-weighted tests for security, migrations, recovery, and treasury.
- [ ] P1 — Sign release images and publish verifiable build provenance; enforce
  signature policy at admission.
- [ ] P1 — Add immutable/off-platform audit export, retention controls, and
  coverage tests proving which material actions are recorded.
- [ ] P1 — Add database row-level security or an equivalent independently tested
  defense-in-depth layer for tenant isolation.
- [ ] P1 — Replace the local Entity Kernel decision store/ledger with durable,
  transactional, tenant-scoped persistence and reconciliation.
- [ ] P1 — If generated dashboards are ever allowed to request mutations, use a
  schema-constrained declarative action model with server-side authorization,
  exact resource binding, explicit human confirmation, idempotency, audit, and
  replay protection. Never restore arbitrary model-authored script or action
  execution.

## Entity Kernel and payment roadmap

The implemented boundary deliberately treats two cases separately:

- **Client payments:** Genus may receive and persist only an opaque token from a
  compliant hosted payment flow. Client PAN/CVV must never traverse Genus APIs,
  prompts, logs, queues, databases, or backups.
- **Genus operational cards:** Genus may receive and persist only an opaque
  issuer token or virtual-card reference. The fact that the organization owns
  the card does not make raw PAN/CVV handling safe or remove PCI/security scope.

- [ ] P2 — Select and threat-model a payment/issuing provider that supports
  hosted client tokenization and opaque operational virtual-card references.
- [ ] P2 — Build separate client-payment and Genus-treasury adapters with signed
  webhook verification, replay protection, idempotent state machines,
  reconciliation, refunds/disputes, and incident runbooks.
- [ ] P2 — Add durable approval identity, separation of duties, per-vendor and
  per-category budgets, reserved-spend accounting, velocity controls, card
  freeze/rotation, and operator-visible statements.
- [ ] P2 — Permit autonomous spend only inside an organization-owned policy;
  permission expansion, production changes, code self-modification, and policy
  changes remain human-approved.
- [ ] P2 — Let reversible low-risk learning run automatically, stage/test code
  improvements in a branch, and require review plus production gates before
  deployment.

## Proper draft PR delivery checklist

- [x] Fetch current `origin/main`, then create a clean worktree and branch such
  as `feat/production-hardening-foundation`. Do not base the PR on the local
  integration branch's unrelated commits.
- [x] Transfer only hardening changes. Explicitly exclude the owner's existing
  changes under `agents/skills/*/meta.json`, `docs/benchmarks/`, `.superpowers/`,
  `agents/skills/fix-broken-typecheck/`, and `brain/docs/`.
- [x] Regenerate `uv.lock` on the clean base so unrelated integration-branch
  dependencies do not enter the PR.
- [x] Organize the PR into reviewable conventional commits:
  1. `fix(db): harden migrations and connection lifecycle`;
  2. `fix(runtime): restore configuration defaults and regression coverage`;
  3. `feat(security): enforce scoped service and dashboard authentication`;
  4. `fix(platform): harden readiness and Kubernetes deployment`;
  5. `feat(backup): add encrypted snapshot and restore workflows`;
  6. `feat(entity): enforce treasury and payment authority boundaries`;
  7. `ci(release): enforce production gates and align versions`;
  8. `docs(ops): document production and compliance controls`.
- [x] Re-run the full gate on the clean PR branch and attach exact command/result
  evidence to the PR.
- [ ] Open a **draft** umbrella PR titled
  `feat(platform): harden Genus OS production foundation`.
- [ ] Include architecture/security notes, migration behavior, deployment
  prerequisites, test evidence, rollout sequence, rollback procedure, known
  residual risks, AI-assistance disclosure, and a link to this checklist in the
  PR body.
- [ ] Request CODEOWNERS, security, and operations review. Leave the PR draft
  until every required check and reviewer is green.
- [ ] Do **not** apply a deployment label, auto-merge, merge manually, deploy,
  or promote production as part of submitting this PR.

## Definition of done

The hardening PR is ready to leave draft only when its clean branch passes every
code gate, the diff is reviewable and excludes owner work, and every unchecked
P0 item is either completed or explicitly assigned as a go-live blocker with an
owner and due date. Production go-live additionally requires the operator P0s
and measured recovery/deployment evidence.

“Production-grade,” “99.9%,” “15-minute RPO,” “60-minute RTO,” “PCI compliant,”
“HIPAA compliant,” and “SOC 2 compliant” are not claims this repository can
make without the applicable deployment evidence, organizational controls,
contracts, scope assessment, and independent review.
