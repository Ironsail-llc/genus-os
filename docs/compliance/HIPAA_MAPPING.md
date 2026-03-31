# Genus OS -- HIPAA Security Rule Mapping

Version: 1.0
Last updated: 2026-03-30
Scope: Generic platform-level mapping to HIPAA Security Rule (45 CFR Part 164, Subpart C)

---

## Overview

This document maps Genus OS platform controls to the HIPAA Security Rule safeguards.
This is a **platform capability mapping**, not a compliance certification. HIPAA
compliance is the responsibility of the deploying covered entity or business associate.

The platform provides technical building blocks. Deployers must layer organizational
policies, workforce training, and BAAs on top to achieve full HIPAA compliance.

**This document does not constitute legal advice.** Deployers should engage qualified
HIPAA counsel for compliance determinations.

---

## Legend

| Symbol | Meaning |
|--------|---------|
| PLATFORM | Control is implemented at the platform level |
| DEPLOYER | Control must be implemented by the deployer |
| SHARED | Platform provides a mechanism; deployer must configure and operate it |

---

## Administrative Safeguards -- 164.308

### 164.308(a)(1) -- Security Management Process

*Implement policies and procedures to prevent, detect, contain, and correct security violations.*

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Risk analysis | -- | DEPLOYER | **The platform does not include a formal risk analysis.** Deployers must conduct a thorough risk analysis per 164.308(a)(1)(ii)(A) covering all ePHI touchpoints in their deployment. |
| Risk management | ES-01, ES-02, ES-03 | SHARED | Guardrail policies, Docker sandboxing, and lifecycle hooks provide risk mitigation mechanisms. Deployer must configure these for PHI-handling workflows. |
| Sanction policy | -- | DEPLOYER | Deployer must establish workforce sanction policies. |
| Information system activity review | MA-01, MA-02 | PLATFORM | Audit logging and guardrail event logging capture all system activity for review. |

---

### 164.308(a)(3) -- Workforce Security

*Implement policies and procedures to ensure appropriate access to ePHI by workforce members.*

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Authorization and supervision | AC-01, AC-03, AC-04 | SHARED | Cloudflare Zero Trust controls human access. RBAC middleware and tool permissions control agent access. Deployer must define authorization policies. |
| Workforce clearance procedure | -- | DEPLOYER | Deployer must implement background check and clearance procedures. |
| Termination procedures | AC-01 | SHARED | Cloudflare Access supports immediate revocation. Deployer must establish termination procedures. |

---

### 164.308(a)(4) -- Information Access Management

*Implement policies and procedures for authorizing access to ePHI.*

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Access authorization | AC-03, AC-04 | PLATFORM | Agent RBAC and tool permission layering enforce access boundaries programmatically. |
| Access establishment and modification | AC-01, AC-02 | SHARED | Cloudflare Zero Trust and API key auth provide access mechanisms. Deployer must define provisioning and modification procedures. |
| Isolating healthcare clearinghouse functions | DP-04 | PLATFORM | Tenant isolation provides database-level and API-level separation. |

---

### 164.308(a)(5) -- Security Awareness and Training

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Security reminders | -- | DEPLOYER | Deployer must implement ongoing security awareness. |
| Protection from malicious software | ES-01, ES-02 | PLATFORM | Guardrails and sandboxing protect against malicious agent behavior. |
| Log-in monitoring | MA-01 | PLATFORM | Audit logging captures authentication events. |
| Password management | AC-01, AC-02 | SHARED | Platform uses token-based and OTP authentication. Deployer must establish credential management policies. |

---

### 164.308(a)(6) -- Security Incident Procedures

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Response and reporting | IR-01, IR-02, IR-03 | SHARED | Dead letter queue, graduated escalation, and failure analyzer provide automated incident detection and response. Deployer must establish breach notification procedures per 164.408. |

---

### 164.308(a)(7) -- Contingency Plan

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Data backup plan | -- | DEPLOYER | Deployer must implement ePHI backup procedures. |
| Disaster recovery plan | IR-01 | SHARED | DLQ enables event recovery. Deployer must implement a comprehensive DR plan. |
| Emergency mode operation plan | -- | DEPLOYER | Deployer must define procedures for operating during an emergency. |
| Testing and revision | -- | DEPLOYER | Deployer must test contingency plans periodically. |

---

### 164.308(a)(8) -- Evaluation

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Periodic evaluation | CM-02, MA-04 | SHARED | Manifest validation and fleet health monitoring support ongoing evaluation. Deployer must conduct periodic formal evaluations. |

---

## Technical Safeguards -- 164.312

### 164.312(a)(1) -- Access Control

*Implement technical policies and procedures for electronic information systems that maintain ePHI.*

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Unique user identification | AC-02, AC-03 | PLATFORM | API key authentication and X-Agent-Id headers provide unique identification for all system actors. |
| Emergency access procedure | -- | DEPLOYER | Deployer must define emergency access procedures for ePHI systems. |
| Automatic logoff | AC-01 | PLATFORM | Cloudflare Access sessions have configurable timeout. |
| Encryption and decryption | DP-01, DP-02 | PLATFORM | SOPS encryption for secrets at rest. Vault for credential storage. |

---

### 164.312(b) -- Audit Controls

*Implement hardware, software, and procedural mechanisms that record and examine activity in systems that contain or use ePHI.*

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Audit logging | MA-01 | PLATFORM | Structured audit log table captures all system events with timestamp, actor, action, resource, and outcome. |
| Guardrail audit trail | MA-02 | PLATFORM | All policy enforcement decisions (block, warn, allow) are logged with full context. |
| Agent execution audit | MA-03 | PLATFORM | Per-run telemetry traces with spans for every tool call and LLM interaction. |

---

### 164.312(c)(1) -- Integrity

*Implement policies and procedures to protect ePHI from improper alteration or destruction.*

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Integrity controls | ES-01 | PLATFORM | `no_destructive_writes` guardrail prevents unauthorized file deletion and modification. `write_path_restrict` confines write operations. |
| Change management | CM-01 | PLATFORM | Pre-commit hooks with gitleaks prevent unauthorized changes containing sensitive data. |

---

### 164.312(d) -- Person or Entity Authentication

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Authentication | AC-01, AC-02, AC-03 | PLATFORM | Cloudflare Zero Trust (email OTP), API key Bearer tokens, and agent identity validation. |

---

### 164.312(e)(1) -- Transmission Security

*Implement technical security measures to guard against unauthorized access to ePHI being transmitted over a network.*

| Requirement | Controls | Responsibility | Notes |
|-------------|----------|----------------|-------|
| Encryption in transit | AC-01 | PLATFORM | All external traffic is routed through Cloudflare Tunnel, which enforces TLS encryption. Internal service communication uses localhost or encrypted tunnels. |
| Integrity controls | AC-01 | PLATFORM | Cloudflare provides TLS integrity verification on all proxied connections. |

---

## Physical Safeguards -- 164.310

Physical safeguards are primarily deployer responsibilities. The platform does not
control the physical environment of the server.

| Requirement (164.310) | Controls | Responsibility | Notes |
|------------------------|----------|----------------|-------|
| (a)(1) Facility access controls | -- | DEPLOYER | Deployer must implement physical access controls for server locations. |
| (b) Workstation use | -- | DEPLOYER | Deployer must define policies for workstations accessing ePHI. |
| (c) Workstation security | -- | DEPLOYER | Deployer must implement physical workstation protections. |
| (d)(1) Device and media controls | -- | DEPLOYER | Deployer must implement media disposal, reuse, and tracking procedures. |

---

## Deployer Responsibilities

The following are **required deployer actions** for any Genus OS deployment handling
ePHI. These cannot be addressed by platform controls alone.

### 1. Formal Risk Analysis -- 164.308(a)(1)(ii)(A)

Conduct a comprehensive risk analysis that identifies:
- All systems and workflows that create, receive, maintain, or transmit ePHI
- Threats and vulnerabilities specific to AI agent processing of ePHI
- Likelihood and impact ratings for each identified risk
- Risk mitigation measures (leveraging platform controls where applicable)

Document the risk analysis and review it at least annually or when significant
changes occur.

### 2. Physical Security of the Server

Implement physical safeguards appropriate to the server hosting environment:
- Data center access controls (if cloud-hosted, verify provider's SOC 2 / HITRUST)
- Server room access logging
- Environmental protections (fire, flood, temperature)
- Media disposal procedures for decommissioned storage

### 3. Workforce Training on PHI Handling

Establish and maintain a training program covering:
- HIPAA Privacy and Security Rule requirements
- Proper handling of ePHI in AI agent workflows
- Incident reporting procedures
- Agent configuration best practices for PHI-handling agents (e.g., guardrail
  assignment, tool restriction, delivery channel restrictions)

### 4. Business Associate Agreements (BAAs)

Execute BAAs with all third-party service providers that may access ePHI:
- **LLM providers** -- Verify the provider's HIPAA compliance posture and execute a
  BAA. Evaluate whether ePHI is transmitted to the LLM and whether the provider
  uses data for training.
- **Cloud infrastructure** -- BAA with hosting provider (if applicable)
- **Cloudflare** -- BAA for tunnel and access services
- **Communication providers** -- BAA with Twilio (voice), Telegram (messaging), or
  any other delivery channel carrying ePHI

### 5. Data Flow Assessment for Specific Integrations

For each integration that handles ePHI, document:
- What ePHI enters the system and through which channel
- Which agents process the data and what tools they use
- Where ePHI is stored (database tables, memory facts, log files)
- What guardrails are applied (ES-01 policies)
- How ePHI exits the system (delivery channels, API responses, reports)
- Retention periods and disposal procedures

### 6. Contingency Planning

Develop and test:
- Data backup procedures for all ePHI stores
- Disaster recovery plan with documented RTO/RPO
- Emergency mode operation procedures
- Annual contingency plan testing

### 7. Agent Configuration for PHI Workflows

When deploying agents that handle ePHI:
- Assign `no_sensitive_data` guardrail (DP-03) to prevent ePHI leakage in outputs
- Assign `no_external_http` guardrail to prevent ePHI transmission to unauthorized endpoints
- Assign `write_path_restrict` to confine ePHI to authorized storage locations
- Set `delivery: none` on worker agents to prevent ePHI from reaching unauthorized channels
- Use `exec_allowlist` to restrict shell commands available to PHI-handling agents
- Configure budget limits (ES-04) to bound processing scope

---

## Revision History

| Date       | Version | Author   | Change |
|------------|---------|----------|--------|
| 2026-03-30 | 1.0     | Platform | Initial HIPAA mapping |
