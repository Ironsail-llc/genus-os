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

- **[Security Controls Inventory](docs/compliance/SECURITY_CONTROLS.md)** — 20+ controls across 6 categories
- **[SOC 2 Mapping](docs/compliance/SOC2_MAPPING.md)** — controls mapped to Trust Service Criteria
- **[HIPAA Mapping](docs/compliance/HIPAA_MAPPING.md)** — generic platform safeguards for healthcare deployments

### Audit API

Programmatic audit access is available via the Bridge API:
- `GET /api/audit/events` — query audit log with time/type/actor filters
- `GET /api/audit/guardrails` — query guardrail events (blocked/warned/allowed)
- `GET /api/audit/stats` — aggregated statistics for rolling time windows

### Summary of Controls

- **Secrets management**: All secrets are SOPS-encrypted and decrypted to tmpfs at runtime. No secrets in environment files or code.
- **Pre-commit scanning**: Gitleaks runs on every commit to prevent secret leaks.
- **Dependency scanning**: Dependabot monitors for known vulnerabilities.
- **Secret scanning**: GitHub push protection blocks commits containing detected secrets.
- **Access control**: Branch protection requires reviewed PRs with passing CI for all changes to `main`.
- **Agent guardrails**: 8 execution safety policies (destructive writes, external HTTP, sensitive data, rate limiting, branch protection, exec allowlists, write path restrictions, desktop safety).
- **Docker sandbox**: Per-run ephemeral containers isolate computer-use agents.
- **Lifecycle hooks**: Blocking pre-tool-use hooks can prevent tool execution.
- **Budget enforcement**: Soft token caps with tool schema stripping after exhaustion.

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
