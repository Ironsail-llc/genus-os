# Security Policy

## Reporting a Vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report vulnerabilities privately via [GitHub Security Advisories](https://github.com/Ironsail-llc/genus-os/security/advisories/new). You will receive a response within 48 hours acknowledging your report, and we will work with you on a fix before any public disclosure.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest `main` | Yes |
| Older releases | Best effort |

## Security Measures

Genus OS employs defense-in-depth security across secrets management, agent execution, and access control. For detailed inventories and compliance mappings, see:

- **[Security Controls Inventory](https://github.com/Ironsail-llc/genus-os/blob/main/docs/compliance/SECURITY_CONTROLS.md)** — 20+ controls across 6 categories
- **[SOC 2 Mapping](https://github.com/Ironsail-llc/genus-os/blob/main/docs/compliance/SOC2_MAPPING.md)** — controls mapped to Trust Service Criteria
- **[HIPAA Mapping](https://github.com/Ironsail-llc/genus-os/blob/main/docs/compliance/HIPAA_MAPPING.md)** — generic platform safeguards for healthcare deployments

### Audit API

Programmatic audit access is available via the Bridge API:
- `GET /api/audit/events` — query audit log with time/type/actor filters
- `GET /api/audit/guardrails` — query guardrail events (blocked/warned/allowed)
- `GET /api/audit/stats` — aggregated statistics for rolling time windows

### Summary of Repository Controls

- **Secrets boundaries**: Helm supports per-workload Vault projections, while
  systemd deployments can use SOPS, age, and tmpfs. Provisioning, rotation,
  revocation, and evidence remain deployment responsibilities.
- **Security gates**: CI scans repository history with Gitleaks, audits Python
  dependencies without standing exceptions, and blocks critical image findings
  in the release workflow.
- **Access control**: Dashboard OIDC, signed service tokens, route-specific
  scopes, tenant binding, and Engine tool-dispatch authorization are enforced
  by repository code. IdP configuration and repository rulesets are external.
- **Agent containment**: Manifest allowlists, execution guardrails, lifecycle
  hooks, budget limits, and optional per-run container isolation reduce the
  agent execution boundary; operators must configure least privilege.
- **Recovery and payment minimization**: Encrypted, checksummed snapshots and
  token-only client/operational payment contracts are implemented mechanisms,
  not evidence of a deployed recovery objective or PCI compliance.

See the controls inventory for exact implementation evidence, limitations, and
required operator actions. Repository mechanisms alone do not prove that a
control is operating in a deployed environment.

## Dependency Advisory Policy

The Python dependency gate runs `pip-audit` without standing advisory
exceptions. Dependency floors are raised when a fix is available, and any new
advisory fails CI and release builds until it is remediated. If an advisory
cannot be fixed immediately, its temporary, time-bounded disposition must be
documented here and approved through security review; there are currently no
such exceptions.

## Scope

The following are in scope for security reports:

- The `robothor` Python package and its dependencies
- The Agent Engine and its tool registry
- The CRM Bridge API
- The Helm dashboard
- Authentication and authorization mechanisms
- Secret handling and storage

Out of scope:

- Third-party services (Twilio, Google APIs, etc.) — report to those providers directly
- Social engineering attacks
- Denial of service attacks
