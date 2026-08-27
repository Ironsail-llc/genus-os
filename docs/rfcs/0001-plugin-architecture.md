# RFC 0001 — Plugin Architecture v1: Governed Plugins on Open Standards

- **Status**: Draft — for operator review
- **Author**: platform
- **Created**: 2026-08-19
- **Scope**: design only; no implementation ships with this document

## Summary

Genus OS's README promises "everything is a plugin." Today that is true internally
(tool registry, adapters, skills, guardrail policies) but false externally: a third
party cannot package, distribute, or install an extension without forking the repo.
This RFC defines the v1 third-party plugin surface. The strategy is deliberate:

1. **Ride existing standards, not a bespoke SDK.** MCP servers are the tool-plugin
   mechanism. agentskills.io-compatible `SKILL.md` directories are the skill-plugin
   mechanism. Python entry points (group `genusos.plugins`) are the deep-extension
   mechanism. We inherit three existing ecosystems on day one instead of growing a
   fourth from zero.
2. **Governance is the differentiator.** Every plugin — regardless of mechanism —
   executes inside the tenant-RLS + RBAC + guardrail + injection-scan +
   exec-allowlist + audit envelope that already exists. No competitor has this.
   A plugin declares a permission manifest; the platform enforces it with
   subsystems that already ship (mapped file-by-file below).
3. **Nothing breaks.** All three existing extension surfaces (engine tools,
   `agents/skills/`, MCP adapters/bridge) are grandfathered as degenerate plugins.

## Motivation

The 2026-08 competitive analysis is blunt: DeepSeek Harness (dsh) shipped "thin core
+ npm plugins" and got 2,600+ community plugin repos in six days; Hermes rides the
agentskills.io standard with self-improving skills; OpenClaw's ClawHub has the
largest catalog and a public supply-chain scandal (a data-exfiltrating third-party
skill found by Cisco; on-host execution by default). All three prove ecosystems come
from riding a package registry, and all three run third-party code with **no tenancy,
no RBAC, no audit trail, and weak-to-absent sandboxing**. That is the quadrant we
take: the plugin platform you can run for more than one user without an incident.

## Design principles

- **P1 — Standards over SDK.** If an extension can be an MCP server or a SKILL.md
  directory, it must be. The Python API surface is reserved for things that cannot
  cross a process boundary (memory providers, LLM providers, schedulers, channels).
- **P2 — Fail-closed by default.** An undeclared permission is a denied permission.
  This mirrors `robothor/engine/permissions.py` ("no match → denied") and the
  guardrails' fail-loud unknown-policy check (`_KNOWN_POLICIES` in
  `robothor/engine/guardrails.py`).
- **P3 — Manifest is the contract.** Consistent with platform rule 4 ("manifests are
  source of truth"), a plugin's `genus-plugin.yaml` is the single declaration of
  what it provides and what it may touch. Enforcement never trusts the plugin's code.
- **P4 — Reuse enforcement, don't rebuild it.** Every clause in the permission
  manifest maps to a subsystem that exists today (see enforcement table).
- **P5 — Platform/instance boundary holds.** Plugin *code* is platform-layer
  (installable by anyone); plugin *enablement and configuration* is instance-layer
  (`~/.config/robothor/`, gitignored), per `docs/PLATFORM_INSTANCE.md`.

## The three plugin mechanisms

### 1. Tool plugins = MCP servers

Any MCP server (stdio or HTTP) is a Genus OS tool plugin. The machinery exists:

- `robothor/engine/mcp_client.py` — stdio (Content-Length framing) and HTTP
  transports; supports both the legacy handshake and the stateless `2026-07-28`
  protocol per-server.
- `robothor/engine/adapters.py` — YAML files in `~/.config/robothor/adapters/`
  declare an MCP server; on startup the engine calls `tools/list` and registers the
  tools as first-class entries in the `ToolRegistry`
  (`robothor/engine/tools/registry.py`, `_adapter_routes`).
- `robothor/engine/extensions.py` — hot-reloads adapter changes without restart.
- `robothor/connectors/rest_mcp_bridge.py` — wraps any REST/OpenAPI/Hydra API as an
  MCP server; becomes the reference packaged plugin.

**v1 change**: the adapter YAML grows into (or is generated from) the permission
manifest below, and adapter-mounted tools stop being implicitly trusted — they pass
through the same guardrail pre/post pipeline as built-in tools, with the plugin's
declared scopes intersected in.

### 2. Skill plugins = agentskills.io SKILL.md directories

Already true. `agents/skills/AGENTSKILLS.md` documents the agentskills.io-compatible
frontmatter (name/description/tags/parameters/tools_required), the `meta.json`
runtime sidecar, and the Rip-3 lean catalog. **v1 change**: skills become
installable as packages (a plugin may ship a `skills/` directory that gets mounted
into the catalog, namespaced `<plugin>:<skill>`), and a skill's `tools_required`
list is validated against the *installing agent's* `tools_allowed` — a skill can
never smuggle in a tool grant. This is the direct answer to the ClawHub exfil
incident: a Genus skill is instructions only; capability comes from the manifest.

### 3. Engine plugins = Python entry points (`genusos.plugins`)

For extensions that must live in-process: memory providers, LLM providers,
schedulers, delivery channels, guardrail policies. Distribution is plain pip.

```toml
# a third-party plugin's pyproject.toml
[project]
name = "genusos-plugin-example-channel"
version = "0.1.0"
dependencies = ["genusos>=1.31,<2"]

[project.entry-points."genusos.plugins"]
example_channel = "genusos_plugin_example_channel:plugin"
```

```python
# genusos_plugin_example_channel/__init__.py  (shape, not implementation)
from robothor.plugins import GenusPlugin, hook

class ExampleChannelPlugin(GenusPlugin):
    api_version = "1.0"            # checked against the engine's PLUGIN_API_VERSION

    @hook("channel.register")      # extension points, v1 set below
    def register_channel(self, registry): ...

plugin = ExampleChannelPlugin()
```

v1 extension points (each maps to an existing seam):

| Extension point | Existing seam |
|---|---|
| `llm.provider` | `robothor/engine/llm_client.py` + `model_registry.py` (the codex provider, `codex_provider.py`, is the in-tree precedent) |
| `memory.provider` | `robothor/memory/` store/retrieval interfaces |
| `channel.register` | `robothor/engine/delivery.py` / `messaging.py` / `channel_bus.py` |
| `scheduler.trigger` | `robothor/engine/scheduler.py` hooks |
| `guardrail.policy` | `robothor/engine/guardrails.py` `_KNOWN_POLICIES` registration |
| `tools.mount` (escape hatch) | `ToolRegistry` — discouraged; MCP is the sanctioned path |

**Discovery ≠ activation.** `pip install` makes a plugin *visible* via
`importlib.metadata.entry_points(group="genusos.plugins")`; nothing loads until the
operator runs `genusos plugin enable <name>`, which records the enablement plus the
manifest hash in `~/.config/robothor/plugins.lock`. dsh auto-loads what you install;
we do not.

## The permission manifest

Every plugin ships `genus-plugin.yaml` at its package root. Undeclared = denied.

```yaml
# genus-plugin.yaml — permission manifest (v1)
name: example-crm-connector
version: 0.3.1
plugin_api: "1.0"                 # engine compat contract (see Versioning)
kind: mcp                         # mcp | skills | python  (one manifest may combine)
provides:
  tools: ["crm_lookup", "crm_note_add"]     # exact tool names it may register
  skills: []                                 # skill dirs it mounts, if any
  extensions: []                             # entry-point hooks, python kind only
permissions:
  scopes: ["crm:read", "crm:write"]          # RBAC scope strings, fail-closed
  tenants: own                               # own | children | list — never "*" in v1
  network:
    allow: ["api.example-vendor.com:443"]    # empty list = no egress
  exec:
    allow: []                                # regex prefixes; empty = no shell, ever
  filesystem:
    write: []                                # write-path allowlist; empty = read-only
  secrets: ["EXAMPLE_VENDOR_TOKEN"]          # env names injected; nothing else visible
sandbox: subprocess                          # trusted | subprocess | container
agents: ["main"]                             # which agents may see its tools ("*" ok)
```

### Enforcement mapping — every clause lands on an existing subsystem

| Manifest clause | Enforced by | Where (file) |
|---|---|---|
| `provides.tools` | Registry refuses to mount undeclared or name-colliding tools; adapter route table | `robothor/engine/tools/registry.py`, `dispatch.py` |
| `permissions.scopes` | Per-role/tool rules in `role_permissions` table, fail-closed evaluation; plugin scopes become a synthetic role intersected with the caller's role | `robothor/engine/permissions.py` |
| `permissions.tenants` | Postgres RLS — `set_config('app.tenant_id', …)` per connection; plugins never get a superuser connection | `robothor/db/connection.py` (`_apply_tenant_scope`), migrations 081/082, `docs/runbooks/TENANT_RLS.md` |
| `permissions.network` | `no_external_http` guardrail generalized to an allowlist argument; container tier adds netns-level deny | `robothor/engine/guardrails.py`; `robothor/engine/sandbox.py` |
| `permissions.exec` | Existing `exec_allowlist` policy (regex prefixes + metacharacter rejection, flag-gated modes) keyed by plugin id instead of agent id | `robothor/engine/guardrails.py` (`_check_exec_allowlist`) |
| `permissions.filesystem.write` | `write_path_restrict` guardrail; container tier mounts read-only outside declared paths | `robothor/engine/guardrails.py`; `sandbox.py` |
| `permissions.secrets` | Env allowlist at process spawn — subprocess/container plugins get *only* listed vars (adapters.py `${VAR}` interpolation already scopes this) | `robothor/engine/adapters.py`, `sandbox.py` |
| tool *output* | Injection scan on tool results before they re-enter the context | `robothor/engine/runner.py` (`guardrail_name="injection_scan"`) |
| everything | Guardrail events + run/step audit trail, SIEM export; per-plugin attribution added to event payloads | `robothor/audit/logger.py`, `siem.py`; `agent_runs`/`agent_run_steps` |
| observability | OTel spans annotated `plugin.name`/`plugin.version` | `robothor/engine/metrics.py` |

The honest caveat, stated here so the RFC doesn't overclaim: for **trusted-tier**
(in-proc) plugins these controls are policy, not physics — in-process Python can
bypass anything. That is why the trusted tier is operator-only (below) and why the
default tier for anything third-party is `subprocess` or `container`.

## Sandboxing tiers

| Tier | Runs as | For | Isolation source |
|---|---|---|---|
| `trusted` | in-proc, engine's asyncio loop | entry-point plugins the **operator** explicitly promotes (`genusos plugin enable --trusted`) | none beyond code review + lockfile hash; refuse to load if manifest hash drifts |
| `subprocess` (default) | child process, stdio MCP | most tool plugins | filtered env (secrets allowlist), guardrail pipeline on every call, engine-side timeouts (`McpServerConfig.timeout_seconds`) |
| `container` | rootless podman | anything with `exec` or `filesystem.write` grants, or unreviewed code | existing sandbox: no host $HOME, no sudo, pids-limit, per-run ephemeral — `robothor/engine/sandbox.py` (`sandbox_binary()` prefers rootless podman precisely because the docker socket is root-equivalent); manifest `sandbox:` key already validated in `robothor/engine/config_schema.py` |

Escalation rule: declaring `exec.allow` or `filesystem.write` forces
`sandbox: container` unless the operator overrides per-instance. Hermes offers seven
exec backends but no permission model deciding *which code deserves which backend*;
we offer three tiers chosen *by the manifest*.

## Discovery, install, registry

v1 is deliberately marketplace-free:

```
genusos plugin add genusos-plugin-example      # pip (PyPI or private index)
genusos plugin add git+https://…/repo.git      # git, pinned to a commit in the lock
genusos plugin add ./local-dir                 # development
genusos plugin list | enable | disable | remove
```

- `~/.config/robothor/plugins.lock` (instance-layer, gitignored) records source,
  version, manifest hash, tier, enablement. Load refuses on hash mismatch.
- Skills-only plugins are just git repos containing `genus-plugin.yaml` + `skills/`
  — zero Python required, matching how agentskills.io repos already circulate.
- **Deferred**: a hosted marketplace/index. We first publish a curated
  `awesome-genusos-plugins` list in-repo. A marketplace without review capacity is
  how OpenClaw got the Cisco headline; we won't ship one before we can gate it.

## Versioning & compatibility contract

- The engine exports `PLUGIN_API_VERSION` (semver, starts `1.0`). A plugin declares
  `plugin_api: "1.x"`; the engine loads it iff major matches and minor ≤ engine's.
  Loader failures are loud, per-plugin, and never take the daemon down.
- Python plugins pin `genusos>=1.31,<2` in their own metadata; pip resolves it.
- MCP plugins version at the protocol level, already handled per-server
  (`protocol: legacy | 2026-07-28` in `mcp_client.py` / adapter YAML).
- Skills carry no code, so compatibility is only frontmatter-schema versioning —
  additive changes only within a major, enforced by the existing manifest checks
  pattern (`robothor/templates/manifest_checks.py` precedent).
- Deprecations: one minor version with a logged warning before removal; the
  flag-gated off→observe→enforce discipline (`robothor/engine/feature_flags.py`)
  applies to any enforcement change that could break loaded plugins.

## Migration: the three existing surfaces (nothing breaks)

| Today | v1 status | Change required from users |
|---|---|---|
| Built-in engine tools (`robothor/engine/tools/handlers/`) | Remain in-tree, tier `trusted`, implicitly full-manifest. No code moves in v1. | none |
| `agents/skills/` directory | Becomes the "local skills plugin" — unnamespaced, highest precedence. Format already agentskills.io-compatible. | none |
| Adapter YAML (`~/.config/robothor/adapters/`) | Keeps loading verbatim; loader treats each adapter file as a degenerate manifest (`kind: mcp`, `sandbox: subprocess`, scopes = current implicit grants) and logs a one-line migration hint. Hot-reload (`extensions.py`) unchanged. | none now; `genusos plugin migrate-adapters` offered later |
| `rest_mcp_bridge.py` | Repackaged as the first official plugin (also stays importable in-tree). | none |

Agent manifests (`docs/agents/*.yaml`) keep `tools_allowed` exactly as-is; plugin
tools appear there by name like any other tool, matching the adapters.py contract.

## Honest read: what each competitor does better, and v1's answer

- **dsh does better**: the agent loop, model adapter, and UI are themselves
  swappable plugins; npm-native distribution produced 2,600 plugin repos in six
  days. **v1 answer**: we do not make the loop swappable (our loop carries the
  guardrail/checkpoint/audit spine — swapping it out would delete the product); we
  match the *distribution* economics with pip + entry points, and we ship the
  governance dsh has none of. If "replaceable loop" demand materializes, that is a
  v2 question, not a v1 blocker.
- **Hermes does better**: skills that write and improve themselves, and seven exec
  backends with near-zero-cost serverless deploy. **v1 answer**: we already share
  their skill *format* (agentskills.io), so their ecosystem's skills load here —
  inside an envelope where `tools_required` cannot exceed the agent's grants. The
  self-improvement loop is out of scope for this RFC (the background-review /
  curator machinery is the natural home) — named as an open question.
- **OpenClaw does better**: marketplace scale, brand, and channel breadth; skills
  install in one click. **v1 answer**: we don't out-catalog 386k stars. We make
  their catalog partially *ours* (SKILL.md compatibility means many ClawHub skills
  port trivially) and we sell the thing their Cisco incident proved they lack:
  a skill here cannot exfiltrate what the manifest never granted, and every call it
  induces is tenant-scoped, RBAC-checked, and audited.

## Delivery plan (PR-sized phases)

- **Phase 1 — loader + manifest (1 PR)**: `robothor/plugins/` package: manifest
  schema + parser (fail-closed defaults), entry-point discovery, `plugins.lock`,
  `genusos plugin add/list/enable/disable/remove` CLI, adapter-YAML grandfathering.
  No enforcement changes yet; plugins load in observe mode, audit-tagged.
- **Phase 2 — governance wiring (1 PR)**: scope intersection into
  `permissions.py`, per-plugin `exec_allowlist`/`network`/`write_path` guardrail
  keying, secrets env filtering at spawn, sandbox-tier escalation rule,
  plugin-attributed audit + OTel spans. Flag `ROBOTHOR_PLUGIN_ENFORCE`
  off→observe→enforce.
- **Phase 3 — ecosystem on-ramp (1 PR)**: packaged `rest-mcp-bridge` reference
  plugin, external `skills/` mounting with namespacing, `awesome-genusos-plugins`
  seed list, plugin-author guide in `docs/`, and conformance tests a plugin repo
  can run in its own CI (`genusos plugin verify`).

Prerequisite (separate track, already planned): `pip install genusos` actually
working from PyPI — entry-point plugins depend on the platform being installable.

## Open questions for the operator

1. **Tenancy of enablement**: v1 enables plugins per-instance. Do we need
   per-*tenant* plugin enablement before any multi-tenant hosting push, or is
   instance-level acceptable for v1?
2. **Trusted-tier bar**: is lockfile hash-pinning + explicit `--trusted` flag
   enough for in-proc plugins, or do we require signed manifests (Ed25519 — the
   federation keys in `robothor/federation/` are reusable) from day one?
3. **Skill self-improvement**: do we chase Hermes's self-writing-skills loop in the
   same cycle (curator/background_review already write skills) or keep v1 static?
4. **`guardrail.policy` extension point**: letting plugins *add* guardrails is
   pure upside; should they ever be able to *relax* one? (Proposed answer: never —
   manifests can only intersect, not union. Confirm.)
5. **OpenClaw importer** (`genusos import openclaw`, move #5 in the analysis):
   fold into Phase 3 or keep it a separate cycle?

## Non-goals for v1

Marketplace/registry hosting; swappable agent loop; paid plugins; remote plugin
execution (Modal/Daytona-style backends); Windows support beyond what the platform
already has; auto-updating plugins (the lockfile pins until the operator updates).
