# Genus OS HIPAA Security Rule capability mapping

Version: 1.1
Last updated: 2026-07-13
Scope: Generic platform capabilities mapped to 45 CFR Part 164, Subpart C

## Use boundary

This is a planning aid, not a compliance certification, risk analysis, legal
opinion, or representation that a deployment may process electronic protected
health information (ePHI). The covered entity/business associate must determine
scope, perform the required risk analysis, implement reasonable and appropriate
safeguards, maintain policies/evidence, and obtain advice appropriate to its
facts. See the HHS [Security Rule overview](https://www.hhs.gov/hipaa/for-professionals/security/index.html).

Status meanings:

- **Platform** — a repository mechanism implements the core behavior.
- **Shared** — the platform supplies a mechanism that must be configured and
  operated by the deployer.
- **Deployer** — primarily organizational, contractual, physical, or
  environment-specific.
- **Gap** — material platform work remains for the proposed use.

## Administrative safeguards — 164.308

| Standard/implementation specification | Status | Current capability and required action |
|---|---|---|
| Security management process — risk analysis | Deployer | Genus does not perform the required organization/deployment risk analysis. Map every ePHI input, store, model, tool, output, log, trace, backup, operator, and vendor; assess AI-specific prompt injection, hallucination, over-disclosure, and tool misuse. |
| Security management process — risk management | Shared | Tool authorization, guardrails, tenant claims, NetworkPolicy, secrets, release gates, snapshots, and authority policy are available. Configure them from the risk analysis and document residual risk. |
| Sanction policy | Deployer | Establish workforce sanctions and enforcement procedures. |
| Information-system activity review | Shared | Core auth/agent/guardrail/task activity emits structured evidence, but audit coverage is not universal. Define material events, test capture/failure, export immutably, review, and retain them. |
| Assigned security responsibility | Deployer | Name the responsible security official and service/control owners. An agent cannot serve as the legally accountable official. |
| Workforce authorization/supervision/clearance/termination | Shared | OIDC sessions, Bridge roles/scopes, revocation, and agent permissions support access control. The deployer must approve access, review it, supervise use, and promptly revoke IdP/Vault/provider privileges. |
| Information access management | Shared/Gap | Verified Bridge tenant/role/scope boundaries exist. Engine/orchestrator/vision per-request identity and database row-level defense in depth remain gaps; keep those services private and do not use hostile multi-tenancy until addressed. |
| Security awareness and training | Deployer | Train workforce on ePHI, AI prompts/outputs, phishing, incident reporting, approved tools/providers, and minimum necessary use. |
| Security incident procedures | Shared | Alerts, audit events, DLQ/escalation, snapshots, and deployment rollback assist response. Establish roles, containment, evidence, breach analysis/notification, communications, and exercises. |
| Contingency plan — data backup | Shared | Encrypted verified snapshots are available. Schedule and monitor them, keep independent/off-site copies, protect the passphrase, and cover every ePHI store/provider—not only Genus PostgreSQL/workspace. |
| Contingency plan — disaster recovery/emergency mode | Deployer | Define failover, safe degraded operation, dependencies, priorities, contacts, and manual procedures. One engine replica is an availability limitation. |
| Contingency plan — testing/revision | Shared | Restore is dry-run by default and supports isolated rehearsals. Prove the target 15-minute RPO/60-minute RTO, record results, correct failures, and repeat at least quarterly/material change. |
| Evaluation | Deployer | Perform periodic technical/nontechnical evaluations and reassess after environmental or operational changes. Repository tests are not a HIPAA evaluation. |
| Business associate contracts — 164.308(b) | Deployer | Determine which vendors are business associates and execute suitable agreements before ePHI flows. HHS provides specific guidance for [cloud service providers and BAAs](https://www.hhs.gov/hipaa/for-professionals/special-topics/health-information-technology/cloud-computing/index.html). |

## Physical safeguards — 164.310

| Standard | Status | Required action |
|---|---|---|
| Facility access controls | Deployer | Control and log physical/data-center access; establish contingency access and maintenance records. |
| Workstation use/security | Deployer | Define authorized locations/functions and protect workstations, browser sessions, terminals, clipboard, screenshots, downloads, and local caches. |
| Device and media controls | Deployer | Inventory media, control movement/reuse, and implement verified disposal and data backup before movement. Include disks, snapshots, logs, developer machines, camera/voice media, and removable storage. |

## Technical safeguards — 164.312

| Standard/implementation specification | Status | Current capability and required action |
|---|---|---|
| Unique user identification | Shared | Dashboard OIDC and signed Bridge subjects provide unique human identity; signed service tokens can identify agents. Do not treat caller-supplied headers as identity outside the explicit loopback dev mode. Close internal-service identity gaps before direct exposure. |
| Emergency access procedure | Deployer | Design, approve, log, test, and periodically review break-glass access. Store recovery credentials independently and prevent silent bypass. |
| Automatic logoff | Shared | Configure IdP/Auth.js session lifetime, refresh/revocation, terminal timeout, and provider sessions according to the risk analysis. |
| Encryption/decryption | Shared | Vault/SOPS mechanisms, database TLS verify-full, ingress TLS, and encrypted snapshots are available. Verify KMS/key custody, database CA, at-rest encryption, swap/dumps, backup storage, and whether east-west encryption is required. |
| Audit controls | Shared/Gap | Structured evidence exists for core paths. Build an ePHI event catalog and prove access/read/export/delete, admin, auth, model/tool, configuration, backup/restore, and incident events are captured without recording unnecessary ePHI. |
| Integrity | Shared | Canonical migration checksums, transactions, snapshot hashes/authentication, idempotency, and append-only treasury contracts support integrity. Add application validation, reconciliation, immutable retention, and source-specific integrity controls. |
| Person or entity authentication | Shared/Gap | Human OIDC and Bridge JWT validation are implemented. Independent workload identity across all services and websockets is P1 work. Network location alone is not person/entity authentication. |
| Transmission security | Shared | Production ingress and database connections can use verified TLS; NetworkPolicy limits flows. Internal service HTTP is not automatically encrypted. Configure TLS/service mesh/VPN as indicated and validate every external provider/channel. |

## ePHI data-flow rules

Before an agent can handle ePHI:

1. Document the legal role/purpose, minimum necessary fields, source,
   recipients, retention, deletion, and incident owner.
2. Select only models, search, communications, telemetry, storage, backup, and
   support vendors approved for that flow and execute any required BAA.
3. Do not rely on “self-hosted” or “local” marketing language: inspect the
   actual configured model fallbacks, web search, messaging, voice, camera,
   crash reporting, traces, and operator tools.
4. Restrict the agent's tools, streams, workspace paths, delivery channels,
   external HTTP, budget, authority, and tenant. Use explicit allowlists and
   human approval for material disclosure/change.
5. Minimize ePHI in prompts, logs, traces, memory facts, task bodies, screenshots,
   snapshots, benchmarks, and test fixtures. Synthetic tests should not copy
   production ePHI.
6. Test authorization, cross-tenant denial, output redaction, audit coverage,
   backup/restore, provider failure, revocation, and incident procedures.
7. Prevent Genus/Nightwatch from using ePHI in autonomous code changes,
   benchmarks, external issue/PR bodies, or unapproved learning datasets.

## Current release blockers for an ePHI production decision

- complete the environment-specific risk analysis and data-flow inventory;
- close or accept the documented internal-service authentication and tenant
  defense-in-depth gaps;
- configure/verify IdP, Vault, TLS, NetworkPolicy, monitoring, retention,
  immutable audit export, incident response, and access reviews;
- execute required BAAs/vendor assessments;
- prove scheduled independent backups and measured restoration within the
  organization's objectives;
- validate every agent/provider/channel intended to touch ePHI;
- obtain qualified security/legal review and retain operating evidence.

## Revision history

| Date | Version | Change |
|---|---:|---|
| 2026-03-30 | 1.0 | Initial capability mapping |
| 2026-07-13 | 1.1 | Reconciled claims with current controls, gaps, recovery objectives, vendors, and AI/ePHI data-flow responsibilities |
