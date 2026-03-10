# Security Policy

## Reporting a Vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report vulnerabilities privately via [GitHub Security Advisories](https://github.com/Ironsail-llc/robothor/security/advisories/new). You will receive a response within 48 hours acknowledging your report, and we will work with you on a fix before any public disclosure.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest `main` | Yes |
| Older releases | Best effort |

## Security Measures

Genus OS employs the following security practices:

- **Secrets management**: All secrets are SOPS-encrypted and decrypted to tmpfs at runtime. No secrets in environment files or code.
- **Pre-commit scanning**: Gitleaks runs on every commit to prevent secret leaks.
- **Dependency scanning**: Dependabot monitors for known vulnerabilities.
- **Secret scanning**: GitHub push protection blocks commits containing detected secrets.
- **Access control**: Branch protection requires reviewed PRs with passing CI for all changes to `main`.

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
