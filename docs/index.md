# Genus OS

<p align="center">
  <img src="images/genus-os-logo.png" width="360" alt="Genus OS logo">
</p>

**The enterprise AI operating system you deploy on your own infrastructure.**

Define agents in YAML. Wire them into governed pipelines with audit trails and
guardrails. Federate across sites, teams, and subsidiaries with cryptographic
trust and scoped permissions. Everything is a plugin — skills, tools, channels,
and connectors snap into a deterministic core.

Your infrastructure. Your data. Your rules.

## Install it

```bash
curl -fsSL https://ironsail-llc.github.io/genus-os/install.sh | bash -s -- --substrate compose
```

That line previews and installs nothing — a pipe has no terminal on the other
end, so there is nowhere to confirm a plan. Read what it prints, then run it
again with `--yes`. [Quick Start](quickstart.md) has the prerequisites, both
substrates by hand, and what to do when the doctor is red.

Deploying this for a company? **Start with the
[Enterprise Guide](enterprise/00-overview.md)** — eight short pages from a
compose pilot to operating a fleet: install, identity, channels, agents,
secrets, backup and upgrade, and what to open when something is wrong.

## Why Genus OS

Genus OS is a self-hosted agent operating platform for organizations that need
to own their runtime, data flows, policies, and deployment lifecycle. It can run
on-premises, in a private cloud, or in a deliberately configured air gap.

- **Data sovereignty** — core services run on your infrastructure; local models
  can keep model traffic inside the deployment.
- **Security** — Vault/SOPS deployment options, signed sessions and scoped
  tokens, per-agent tool allow/deny lists, runtime guardrails, and
  cryptographic federation identity.
- **Governance** — structured agent-run, tool, policy, and task evidence;
  review workflows; approval boundaries; OTel-compatible trace context.
- **Scalability** — federate autonomous instances across sites, teams, and
  subsidiaries with scoped exports/imports and no transitive trust.

## Highlights

- **Governed agent platform** — declarative YAML agent manifests, conditional
  workflows, and a large registered tool catalog gated by per-agent
  allow/deny lists and guardrail policies.
- **Everything is a plugin** — skills, tools, messaging channels, external SaaS
  APIs (via the generic REST-to-MCP connector bridge), and MCP servers attach
  declaratively to a small deterministic engine.
- **Intelligence layer** — two-tier memory with hybrid search (HNSW vectors +
  BM25, fused by Reciprocal Rank Fusion); embedding, reranking, and generation
  can be fully local.
- **Enterprise federation** — Ed25519 signed invites, Hybrid Logical Clocks,
  and NATS JetStream transport connect instances into a peer-to-peer mesh.
- **The Helm** — a Next.js control plane for chat, tasks, event streams, and
  fleet health.

## Get Started

- [Enterprise Guide](enterprise/00-overview.md) — the whole path, in eight
  task-shaped pages, for a company running a pilot and growing it.
- [Quick Start](quickstart.md) — prerequisites, both substrates, the setup
  wizard, and what to do when the doctor is red.
- [Configuration](configuration.md) — how settings resolve, and the ones worth
  a narrative.
- [Deployment](deployment.md) — Docker Compose, Helm, systemd, and the
  diagnostics.
- [CLI reference](reference/cli.md) and
  [settings reference](reference/configuration.md) — generated from the parser
  and the settings model, so they cannot drift.
- [Release notes](release-notes.md) — what changed, for operators, admins and
  agent authors.
- [Architecture](architecture.md) — how the pieces fit together.
- [Platform vs Instance](PLATFORM_INSTANCE.md) — the boundary that keeps your
  data out of the platform.

## Source

Genus OS is developed in the open at
[Ironsail-llc/genus-os](https://github.com/Ironsail-llc/genus-os) under the MIT
license.
