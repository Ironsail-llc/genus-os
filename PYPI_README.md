# Genus OS

**A self-hosted AI agent operating system.** Deterministic engine, persistent
memory, multi-tenant CRM, and a plugin surface for skills and connectors.
Everything is a plugin.

Genus OS runs a fleet of agents you define as YAML manifests. Each agent gets
durable memory, scheduled and event-driven execution, tool sandboxing, RBAC,
and an audit trail. You bring your own models — cloud APIs or a local runtime.

## Install

```bash
pip install "genusos[api]"
genus init
```

`genus init` is an interactive wizard: it creates the workspace, writes the
configuration, runs the database migrations, and scaffolds a starter agent.
Then `genus status` shows what is running and `genus --help` lists every verb.

The `genusos` and `robothor` console scripts are aliases of `genus` and behave
identically.

## Extras

| Extra | Adds |
|-------|------|
| `api` | REST API and web control plane |
| `channels` | Slack channel support |
| `vision` | Image understanding and face recognition |
| `federation` | Peer-to-peer instance networking |
| `mcp` | Model Context Protocol server |
| `tui` | Terminal chat interface |
| `all` | Everything above, plus the dev toolchain |

## Requirements

Python 3.11+, PostgreSQL 16+ with pgvector, and Redis 7+. The repository
ships Compose files for those services under `infra/` and `examples/`.

## Links

- Documentation: <https://ironsail-llc.github.io/genus-os/>
- Source and issues: <https://github.com/Ironsail-llc/genus-os>

## License

MIT
