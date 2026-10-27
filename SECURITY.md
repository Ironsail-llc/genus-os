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
- **Credential files are never read by agents**: `read_file` refuses `.env`
  and `*.env` files, SOPS `*.enc.*`, key material (`*.pem`, `*.key`, SSH keys),
  `.netrc`/`.pgpass`, cloud credential files and anything under `.ssh`, `.aws`,
  `.kube`, `/run/robothor` or `/etc/robothor` before opening them
  (`robothor/engine/secret_paths.py`), and `exec` refuses a command that would
  print one (`cat`, `grep`, `head`, `sed` on the file, or a bare `printenv`)
  while still allowing the file to be sourced for an authenticated command.
  Tool output is still scanned and redacted, and every credential warning is
  recorded in `agent_guardrail_events`.
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
documented here and approved through security review. The exceptions in force
are listed below; an entry past its expiry date must be removed or renewed
through the same review, never left in place.

### Current exceptions

| Advisory | Package | Why it does not apply | Expires | Tracking | Approved |
|---|---|---|---|---|---|
| GHSA-7hp6-4w63-5g45 (CVE-2026-114612) | litellm 1.92.2 (locked) | Privilege escalation in the litellm proxy server through reuse of its salt key. This project imports litellm as a completion library only: no `litellm[proxy]` extra, no `litellm.proxy` import, no proxy process in any image, chart or workflow. | 2026-11-10 | [#723](https://github.com/Ironsail-llc/genus-os/issues/723): bump litellm to a patched release (1.100.4, 1.101.3, 1.102.2 or 1.103.1 and later) and remove the ignore from both `pip-audit` gates | Repository owner, 2026-10-27 |

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
