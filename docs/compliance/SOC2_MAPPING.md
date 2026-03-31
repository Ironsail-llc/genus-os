# Genus OS -- SOC 2 Trust Service Criteria Mapping

Version: 1.0
Last updated: 2026-03-30
Scope: Platform-level controls mapped to AICPA SOC 2 Trust Service Criteria (2017 revision)

---

## Overview

This document maps the Genus OS security controls (defined in `SECURITY_CONTROLS.md`)
to the SOC 2 Trust Service Criteria. The mapping covers all five Trust Service
Categories: Security, Availability, Confidentiality, Processing Integrity, and Privacy.

Each criterion includes the platform controls that address it, an assessment of
coverage, and notes on any gaps requiring deployer action.

---

## Legend

| Symbol | Meaning |
|--------|---------|
| COVERED | Platform control fully addresses the criterion |
| PARTIAL | Platform control partially addresses; deployer action needed |
| GAP | No platform control; deployer must implement |

---

## CC1 -- Control Environment

*The entity demonstrates a commitment to integrity and ethical values, exercises oversight responsibility, establishes structure and authority, demonstrates commitment to competence, and enforces accountability.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| CC1.1 -- Integrity and ethical values | CM-03 | PARTIAL | Nightwatch code review enforces review discipline. Deployer must establish organizational code of conduct. |
| CC1.2 -- Board oversight | -- | GAP | Platform does not implement governance structures. Deployer responsibility. |
| CC1.3 -- Organizational structure | CM-02 | PARTIAL | Manifest validation enforces agent configuration standards. Deployer must define organizational roles and responsibilities. |
| CC1.4 -- Commitment to competence | CM-01, CM-02 | PARTIAL | Pre-commit hooks and validation scripts enforce technical standards. Deployer must establish hiring and training practices. |
| CC1.5 -- Accountability | MA-01, MA-02 | COVERED | Audit logging and guardrail event logging provide accountability trails for all system actions. |

---

## CC2 -- Communication and Information

*The entity internally communicates information necessary to support the functioning of internal control, and externally communicates information related to matters affecting the functioning of internal control.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| CC2.1 -- Internal communication | MA-01, MA-02, MA-03 | COVERED | Audit logs, guardrail events, and agent telemetry provide comprehensive internal reporting on system operations. |
| CC2.2 -- Internal communication of control deficiencies | IR-03 | COVERED | Failure Analyzer identifies and communicates control deficiencies through task creation. |
| CC2.3 -- External communication | -- | GAP | Platform does not implement external stakeholder communication. Deployer must establish external reporting processes. |

---

## CC3 -- Risk Assessment

*The entity specifies objectives with sufficient clarity to enable the identification and assessment of risks relating to objectives.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| CC3.1 -- Objectives specification | ES-01, ES-04 | PARTIAL | Guardrail policies and budget enforcement define operational boundaries. |
| CC3.2 -- Risk identification and analysis | -- | **GAP** | No formal risk assessment process is documented in the platform. Deployer must conduct and document a formal risk assessment. |
| CC3.3 -- Fraud risk assessment | DP-03, AC-05 | PARTIAL | Sensitive data detection and branch protection mitigate specific fraud vectors. Deployer must conduct a comprehensive fraud risk assessment. |
| CC3.4 -- Change impact assessment | CM-02 | PARTIAL | Manifest validation catches configuration errors. Deployer must implement formal change impact assessment procedures. |

---

## CC4 -- Monitoring Activities

*The entity selects, develops, and performs ongoing and/or separate evaluations to ascertain whether the components of internal control are present and functioning.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| CC4.1 -- Ongoing monitoring | MA-01, MA-02, MA-03, MA-04 | COVERED | Comprehensive monitoring via audit logs, guardrail events, agent telemetry, and fleet health metrics. |
| CC4.2 -- Deficiency evaluation | IR-03, MA-04 | COVERED | Failure Analyzer evaluates deficiencies every 2 hours. Fleet health monitoring detects anomalies using 2-sigma baseline comparison. |

---

## CC5 -- Control Activities

*The entity selects and develops control activities that contribute to the mitigation of risks to the achievement of objectives to acceptable levels.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| CC5.1 -- Risk mitigation activities | AC-01 through AC-05, ES-01 through ES-05 | COVERED | Full suite of access controls and execution safety controls. |
| CC5.2 -- Technology controls | AC-01, AC-02, AC-03, ES-01, ES-02 | COVERED | Zero Trust access, API authentication, RBAC, guardrails, and sandboxing. |
| CC5.3 -- Policy and procedure deployment | CM-02, ES-01 | COVERED | Manifest validation and guardrail engine enforce policies automatically. |

---

## CC6 -- Logical and Physical Access Controls

*The entity implements logical access security controls over information assets to protect them from security events.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| CC6.1 -- Logical access security | AC-01, AC-02, AC-03 | COVERED | Cloudflare Zero Trust, API key auth, and RBAC middleware. |
| CC6.2 -- Credential management | DP-01, DP-02 | COVERED | SOPS encryption and Vault credential storage. |
| CC6.3 -- Access removal | AC-01 | PARTIAL | Cloudflare Access allows immediate revocation. Deployer must implement provisioning/deprovisioning procedures. |
| CC6.4 -- Physical access | -- | GAP | Platform does not control physical access. Deployer responsibility. See Deployer Recommendations. |
| CC6.5 -- Logical access restrictions | AC-04, ES-01 | COVERED | Tool permission layering and guardrail policies restrict agent capabilities. |

---

## CC7 -- System Operations

*The entity manages system operations to detect and mitigate processing deviations.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| CC7.1 -- Infrastructure monitoring | MA-04 | COVERED | Fleet health monitoring with anomaly detection. |
| CC7.2 -- Incident detection | IR-01, IR-02, IR-03 | COVERED | Dead letter queue, graduated escalation, and failure analyzer. |
| CC7.3 -- Incident response | IR-02, IR-03 | PARTIAL | Automated escalation and analysis. Deployer must establish a formal incident response plan. |
| CC7.4 -- Recovery operations | IR-01 | PARTIAL | DLQ enables event replay. Deployer must implement formal recovery procedures and testing. |

---

## CC8 -- Change Management

*The entity authorizes, designs, develops, configures, documents, tests, approves, and implements changes to infrastructure and data to meet its objectives.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| CC8.1 -- Change authorization | CM-03, AC-05 | COVERED | Nightwatch draft PRs require human approval. Protected branch guardrails enforce this. |
| CC8.2 -- Change testing | CM-01, CM-02 | COVERED | Pre-commit hooks and manifest validation enforce testing standards. |
| CC8.3 -- Change documentation | CM-02, CM-03 | PARTIAL | Manifests and PRs provide change documentation. Deployer should maintain a change log or use a ticketing system. |

---

## CC9 -- Risk Mitigation

*The entity identifies, selects, and develops risk mitigation activities.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| CC9.1 -- Risk mitigation selection | ES-01, ES-02, ES-03, ES-04 | COVERED | Guardrails, sandboxing, lifecycle hooks, and budget enforcement. |
| CC9.2 -- Vendor risk management | -- | GAP | Platform does not implement vendor risk assessment. Deployer must assess LLM provider and cloud vendor risks. |

---

## A1 -- Availability

*The entity maintains availability of the system to meet its objectives.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| A1.1 -- Capacity management | MA-04, ES-04 | COVERED | Fleet health monitoring and budget enforcement prevent resource exhaustion. |
| A1.2 -- Recovery objectives | IR-01, IR-02 | PARTIAL | DLQ and escalation support recovery. **GAP: No formal disaster recovery plan or recovery time/point objectives (RTO/RPO) are defined at the platform level.** |
| A1.3 -- Recovery testing | -- | **GAP** | No formal disaster recovery testing is implemented. Deployer must establish and execute DR testing procedures. |

---

## C1 -- Confidentiality

*The entity protects confidential information to meet its objectives.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| C1.1 -- Confidential information identification | DP-03 | PARTIAL | Sensitive data guardrail detects common secret patterns. Deployer must classify information per organizational policy. |
| C1.2 -- Confidential information protection | DP-01, DP-02, DP-04 | COVERED | SOPS encryption, Vault storage, and tenant isolation. |
| C1.3 -- Confidential information disposal | -- | GAP | Platform does not implement data disposal procedures. Deployer must define retention and disposal policies. |

---

## PI1 -- Processing Integrity

*The entity achieves its objectives related to processing integrity.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| PI1.1 -- Processing accuracy | ES-01, ES-04 | COVERED | Guardrail policies and budget enforcement constrain processing within defined boundaries. |
| PI1.2 -- Processing completeness | ES-05 | COVERED | Stall watchdog detects and terminates incomplete runs, triggering re-evaluation. |
| PI1.3 -- Processing timeliness | MA-04, ES-05 | COVERED | Fleet monitoring and stall detection ensure timely processing. |

---

## P1 -- Privacy

*The entity collects, uses, retains, discloses, and disposes of personal information to meet its objectives.*

| Criterion | Controls | Coverage | Notes |
|-----------|----------|----------|-------|
| P1.1 -- Privacy notice | -- | **GAP** | Platform does not generate privacy notices. Deployer must create and publish appropriate privacy notices. |
| P1.2 -- Choice and consent | -- | **GAP** | Platform does not implement consent management. Deployer must implement consent mechanisms appropriate to their jurisdiction. |
| P1.3 -- Collection limitation | DP-04 | PARTIAL | Tenant isolation limits data scope. Deployer must implement data minimization practices. |
| P1.4 -- Use and retention | -- | **GAP** | No formal privacy impact assessment at the platform level. Deployer must define data use and retention policies. |
| P1.5 -- Disclosure and notification | DP-03 | PARTIAL | Sensitive data guardrail prevents inadvertent disclosure. Deployer must implement breach notification procedures. |
| P1.6 -- Access | -- | GAP | Platform does not implement data subject access requests. Deployer must implement DSR handling. |
| P1.7 -- Quality | -- | GAP | Deployer responsibility. |
| P1.8 -- Monitoring and enforcement | MA-01, MA-02 | PARTIAL | Audit logging supports privacy monitoring. Deployer must implement privacy-specific monitoring rules. |

---

## Gap Summary

| Gap Area | Criteria Affected | Severity | Deployer Action Required |
|----------|-------------------|----------|--------------------------|
| No formal risk assessment process | CC3.2 | High | Conduct and document a formal risk assessment covering AI agent operations, data handling, and third-party dependencies. |
| No disaster recovery testing | A1.3 | High | Establish DR testing procedures including backup verification, failover testing, and documented RTO/RPO targets. |
| No formal privacy impact assessment | P1.4 | High | Conduct a PIA for each deployment covering data flows, retention, and jurisdictional requirements. |
| No vendor risk management | CC9.2 | Medium | Assess risks of LLM providers, cloud vendors, and other third-party services used in the deployment. |
| No data disposal procedures | C1.3 | Medium | Define data retention schedules and secure disposal procedures. |
| No external communication process | CC2.3 | Medium | Establish procedures for communicating control-related matters to external stakeholders. |
| No consent management | P1.2 | Medium | Implement consent mechanisms appropriate to the deployment's jurisdiction and use case. |
| No data subject access requests | P1.6 | Medium | Implement DSAR handling procedures and tooling. |
| Physical access controls | CC6.4 | Low | Implement physical security appropriate to the server hosting environment. |

---

## Deployer Recommendations

The following actions are recommended for deployers seeking SOC 2 Type II compliance:

1. **Conduct a formal risk assessment** (CC3.2) -- Document threat models specific to
   your deployment, including AI-specific risks such as prompt injection, model
   hallucination, and data leakage through LLM providers.

2. **Establish disaster recovery procedures** (A1.2, A1.3) -- Define RTO/RPO targets,
   implement backup verification, and schedule quarterly DR tests.

3. **Perform a privacy impact assessment** (P1.4) -- Map all personal data flows
   through the system, identify legal bases for processing, and document retention
   periods.

4. **Implement vendor risk management** (CC9.2) -- Assess LLM providers (data
   processing agreements, model training policies, data residency) and cloud
   infrastructure vendors.

5. **Define data lifecycle policies** (C1.3) -- Establish retention schedules for
   audit logs, agent run data, memory facts, and CRM records. Implement secure
   disposal procedures.

6. **Create an incident response plan** (CC7.3) -- Document escalation procedures,
   communication templates, and roles/responsibilities beyond the automated
   platform controls.

7. **Implement privacy controls** (P1) -- Deploy consent management, DSAR handling,
   breach notification procedures, and privacy-specific monitoring appropriate to
   your jurisdiction.

8. **Document organizational controls** (CC1) -- Establish governance structures,
   roles and responsibilities, and a code of conduct that covers AI agent operations.

---

## Revision History

| Date       | Version | Author   | Change |
|------------|---------|----------|--------|
| 2026-03-30 | 1.0     | Platform | Initial SOC 2 mapping |
