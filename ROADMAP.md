# Genus OS roadmap

Genus OS 1.10 is an agent operating platform moving toward an
organization-owned, policy-bound software entity. “Self-functioning” means it
can observe, remember, plan, act, recover, and improve inside delegated limits.
It does not mean unlimited authority, hidden financial activity, autonomous
production deployment, or permission to rewrite its own governing policy.

The delivery checklist and go-live blockers live in
[`docs/PRODUCTION_HARDENING_TODO.md`](docs/PRODUCTION_HARDENING_TODO.md).

## 1.10 production foundation — current

Implemented in the current hardening work:

- canonical checksum-verified PostgreSQL migrations with drift detection and a
  packaged migration manifest;
- dependency-aware readiness, persistent fleet workspace, hardened containers,
  and production NetworkPolicies;
- fail-closed dashboard/Bridge SSO and JWT authorization with verified tenant,
  role, scope, audience, and agent identity;
- release-blocking Python, integration, frontend/browser, Helm, vulnerability,
  and post-deploy smoke gates with rollback;
- encrypted/checksummed snapshot and restore tooling with safe dry-run defaults;
- a first Entity Kernel treasury boundary with token/reference-only payment
  models, deny-default spend policy, idempotency, authority tiers, and redacted
  tamper-evident events;
- runtime/database reliability repairs and removal of stale test quarantine.

This is a release-candidate foundation, not a certification or an availability
claim. A real deployment still requires the operator controls and measured
recovery evidence in the production TODO.

## Phase 1 — measured reliability and zero-trust service identity

- add leader election or active/passive failover for the single-writer engine;
- require independently verified workload identity and service-specific
  audiences on engine, orchestrator, vision, webhook, and websocket surfaces;
- add PostgreSQL point-in-time recovery and automated isolated restore drills;
- publish SLI dashboards and alerts for availability, latency, errors,
  saturation, queue depth, auth failures, migration state, and snapshot age;
- run failure-injection tests for database, Redis, model, node, secret rotation,
  and partial deployment failures;
- ratchet risk-weighted coverage to 70%, then 80%;
- sign images, publish build provenance, and enforce admission verification;
- export audit events to immutable storage with explicit retention and coverage
  tests.

Exit criterion: the operated deployment demonstrates at least 99.9%
availability, a maximum 15-minute recovery point, and a maximum 60-minute
recovery time over an agreed measurement window. Repository features alone do
not satisfy that criterion.

## Phase 2 — durable Entity Kernel treasury

- implement transactional, tenant-scoped decision, reservation, idempotency,
  and append-only ledger storage;
- integrate a reviewed provider through two separate adapters: hosted/tokenized
  customer payments and Genus-owned virtual-card references;
- verify signed webhooks, prevent replay, reconcile authorizations/settlements,
  and model refunds, disputes, failures, card freeze, and rotation;
- enforce vendor/category/currency allowlists, velocity limits, daily/monthly
  budgets, approval thresholds, and separation of duties;
- build operator-visible statements, exception queues, and incident controls;
- complete the actual PCI scope assessment and provider/vendor review.

Raw PAN, full track data, PIN data, and CVC/CVV remain outside Genus OS for both
customer and organization-owned cards. Card ownership is not an exemption from
PCI or sound credential handling.

## Phase 3 — governed self-functioning entity

- maintain a signed organization charter, objectives, delegated authorities,
  risk appetite, and explicit non-delegable actions;
- continuously build a self-model from health, outcomes, costs, obligations,
  and operator feedback;
- let reversible, low-risk learning and operational tuning execute
  automatically with bounded rollback;
- stage code changes on branches, run benchmarks/security checks, and open
  reviewable draft PRs; never deploy code or expand permissions without human
  approval;
- require plans and approval for production mutation, external publication,
  high-impact communication, regulated-data expansion, and controlled spend;
- expose every material decision, reason, evidence, policy version, approval,
  and outcome to the operator;
- detect goal conflict, policy ambiguity, degraded confidence, and anomalous
  behavior and fail closed or escalate.

Exit criterion: Genus can sustain routine operations and recovery inside a
formally delegated envelope while an operator can inspect, interrupt, revoke,
and reconstruct every material action.

## Phase 4 — enterprise and regulated-operation evidence

- formalize data classification, retention/deletion, consent, legal hold,
  incident response, access review, vendor risk, and privacy workflows;
- add tenant defense in depth such as PostgreSQL row-level security;
- produce environment-specific control evidence packages rather than generic
  compliance claims;
- validate accessibility, localization, administrative delegation, audit
  export, and multi-region/federated recovery;
- pursue independent assessments only after the deployed technical and
  organizational controls are operating and evidenced.

HIPAA, PCI DSS, SOC 2, or other mappings in this repository are planning aids.
They are not legal advice, attestations, certifications, or proof that a
particular deployment is compliant.
