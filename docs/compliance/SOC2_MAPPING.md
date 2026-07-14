# Genus OS SOC 2 readiness mapping

Version: 1.1
Last updated: 2026-07-13
Reference: 2017 Trust Services Criteria with revised points of focus (2022)

## Use boundary

This is an internal readiness aid, not a SOC 2 report, attestation, assertion of
control design/operating effectiveness, or authorization to use the SOC logo.
SOC 2 is an examination of a service organization's description of its system
and controls relevant to selected Trust Services Categories. The system
boundary, commitments, risks, control owners, evidence period, complementary
user-entity controls, subservice organizations, and actual operation must be
defined with the independent practitioner. See the AICPA's
[SOC suite and current guidance](https://www.aicpa-cima.com/topic/audit-assurance/audit-and-assurance-greater-than-soc-2).

The statuses below mean:

- **Mechanism** — repository code can support a control.
- **Shared** — technical plus deployment/organizational operation is required.
- **Gap** — material design or evidence work remains.

## Common criteria readiness

| Criteria area | Status | Current support | Required before an examination |
|---|---|---|---|
| CC1 — Control environment | Gap | Entity authority tiers and human approval express some technical accountability boundaries. | Establish governance, integrity/ethics, board/management oversight, organization structure, competence, HR lifecycle, control owners, accountability, and evidence. Genus cannot be its own accountable management. |
| CC2 — Communication and information | Shared | Structured agent/auth/guardrail/task events, dashboards, health, PRs, and runbooks communicate selected operational information. | Define internal/external communication duties, incident/customer notices, policy distribution, evidence quality, and reporting of control deficiencies. Validate audit completeness. |
| CC3 — Risk assessment | Gap | Guardrails, threat-relevant tests, deny-default treasury decisions, and production TODO identify specific risks. | Perform and approve a formal risk/fraud/change assessment for the service, AI behavior, vendors, data, infrastructure, objectives, and material changes; track treatments and residual risk. |
| CC4 — Monitoring activities | Shared | Liveness/readiness, telemetry, error escalation, release gates, snapshot verification, and planned alerts support monitoring. | Operate independent monitoring over an evidence period, define thresholds/owners, evaluate deficiencies, track remediation, and retain review evidence. Readiness is not an availability history. |
| CC5 — Control activities | Shared | Authorization, tool policy, migrations, CI, snapshots, NetworkPolicy, and approval contracts are implemented mechanisms. | Map risks to precise controls/frequency/owner/evidence, test them, control overrides, and document complementary user-entity controls. |
| CC6 — Logical and physical access | Shared/Gap | Dashboard OIDC, Bridge JWT/tenant/scope enforcement, Vault/SOPS options, non-root pods, and NetworkPolicy exist. | Provision/review/revoke access, protect break glass and keys, add workload identity to internal services, add tenant defense in depth, and implement physical/environmental controls. |
| CC7 — System operations | Shared | Health probes, alertable telemetry, DLQ/escalation patterns, critical-CVE blocking, snapshots, and deployment rollback assist operations. | Operate vulnerability/patch/log/incident processes; tune and evidence alerts; exercise containment, recovery, communications, forensics, and lessons learned. |
| CC8 — Change management | Shared | Versioned manifests/migrations, full release gates, human-reviewed PR boundary, and smoke rollback support controlled change. | Require branch rules/reviewers, tickets/risk assessment, segregation, emergency change procedure, artifact provenance, approvals, deployment evidence, and post-change review. Schema changes are forward-only. |
| CC9 — Risk mitigation/vendor management | Gap | Provider boundaries and egress configuration make dependencies identifiable. | Inventory/assess/contract/monitor all cloud, LLM, identity, messaging, payment, support, and infrastructure vendors; define subservice treatment, exit, and incident obligations. |

## Additional Trust Services Categories

### Availability (A)

- **Status:** Gap for a 99.9% commitment.
- Multi-replica stateless services, readiness, PDBs, snapshots, and rollback are
  useful mechanisms.
- The engine is deliberately single-writer and lacks leader election/tested
  failover. The 15-minute RPO and 60-minute RTO are targets until scheduled
  independent backups and measured drills prove them.
- Define commitments, capacity, dependency/failure models, SLIs/SLOs, alerting,
  continuity, PITR, failover, exercise frequency, and evidence period.

### Confidentiality (C)

- **Status:** Shared.
- Vault/SOPS options, TLS configuration, NetworkPolicy, tenant claims, output
  scanning, payment-data minimization, and encrypted snapshots support
  confidentiality.
- The deployer must classify confidential information, minimize collection,
  authorize uses, approve vendors/egress, manage keys, establish retention/legal
  hold/disposal, prevent sensitive telemetry, and test leakage paths. A tokenized
  payment boundary is not a PCI attestation.

### Processing integrity (PI)

- **Status:** Shared/Gap for high-impact autonomous outcomes.
- Canonical checksummed migrations, transactions, idempotency, validation,
  snapshot authentication, workflow state, and treasury policy support complete
  and authorized processing.
- LLM output is probabilistic. Define input/output quality criteria, source
  validation, review/approval thresholds, reconciliation, exception handling,
  timeliness, correction, and customer commitments for each service workflow.
  A guardrail or self-evaluation alone does not prove accuracy.

### Privacy (P)

- **Status:** Gap until an organization/deployment privacy program exists.
- Tenant scoping, access controls, minimization models, audit mechanisms, and
  retention-capable stores can support a program.
- Define notices, legal bases/consent, purpose limitation, collection, use,
  disclosure, quality, access/correction/deletion requests, retention, disposal,
  children/biometric/health rules, cross-border transfers, complaints,
  monitoring, and breach notification. Map every prompt, model, connector, log,
  trace, screenshot, voice/camera artifact, memory fact, and backup.

## Evidence package to build

- management-approved system description, service commitments, scope, asset and
  data-flow inventory, subservice organizations, and complementary controls;
- risk/control matrix with owner, frequency, population, evidence, test method,
  exception handling, and remediation tracking;
- workforce/vendor/access lifecycle records and periodic reviews;
- branch/release/migration/deployment/rollback records, SBOM/vulnerability
  results, and eventually signed provenance;
- Vault/KMS/TLS/network/configuration evidence and change/rotation records;
- immutable audit/monitoring evidence, alert response, incident exercises, and
  control-review records;
- backup age/verification/off-site-copy evidence and measured restore/failover
  exercises;
- privacy, retention/deletion, customer request, vendor, contract, and
  communication records appropriate to the selected categories.

## Current priority gaps

1. Formal service boundary, commitments, risk assessment, and control ownership.
2. Engine high availability and measured 99.9%/RPO/RTO evidence.
3. Independent service identity and stronger tenant defense in depth.
4. Immutable audit export plus tested event-coverage/retention controls.
5. Vendor/subservice, privacy, data lifecycle, and incident programs.
6. Signed artifacts/provenance and admission enforcement.
7. An evidence period demonstrating that approved controls actually operated.

See `SECURITY_CONTROLS.md` and `../PRODUCTION_HARDENING_TODO.md` for the current
implementation and delivery checklist.

## Revision history

| Date | Version | Change |
|---|---:|---|
| 2026-03-30 | 1.0 | Initial criteria mapping |
| 2026-07-13 | 1.1 | Reframed as readiness mapping; removed unsupported coverage claims; added current technical, organizational, availability, privacy, and evidence gaps |
