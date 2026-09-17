# Platform vs Instance — Architectural Boundary

Genus OS separates **platform code** (what ships to everyone) from **instance configuration** (what's personal to each deployment). This document explains the model, how it's enforced, and how to make decisions about where new code belongs.

## Three Layers

| Layer | What | Tracked in git? | Examples |
|-------|------|-----------------|----------|
| **Platform** | Core engine, tools, migrations, dashboard, docs | Yes | `robothor/`, `crm/`, `infra/`, `app/`, `docs/*.md` |
| **Instance** | Identity, agent configs, learned skills, memory, secrets | No | `brain/`, `brain/skills/`, `docs/agents/*.yaml`, `local/`, `.env` |
| **Runtime** | Session state, assembled prompts, tenant context | No (in-memory) | System prompts, warmup blocks, scratchpad |

A platform upgrade — a new wheel, or a new image tag — only touches Layer 1. Layers 2 and 3 are untouched.

## How Claude Code Sees Both Layers

Claude Code reads `CLAUDE.md` files from the directory tree:

- `CLAUDE.md` (root) — **Platform rules**. Tracked. Every clone gets this.
- `brain/CLAUDE.md` — **Instance rules**. Gitignored. Personal identity and operator preferences.
- `robothor/engine/CLAUDE.md` — **Subsystem rules**. Tracked. Engine-specific dev guidance.
- `crm/CLAUDE.md` — **Subsystem rules**. Tracked. CRM-specific dev guidance.

Claude sees all of them in a session, but only the tracked files enter git. Your personal instance file `brain/CLAUDE.md` stays local.

## How Engine Agents Access Rules

Engine agents don't read `CLAUDE.md` files — they use:

- **Instruction files** (`brain/agents/*.md`) — per-agent behavior rules
- **Warmup context** — memory blocks, status files, peer agent state
- **Tool calls** — `read_file` to access `docs/*.md` when they need reference material
- `brain/SOUL.md` — instance personality, injected via warmup

## Decision Tree: Where Does This Go?

```
Is it personal data (name, email, phone, address)?
  → brain/CLAUDE.md or brain/SOUL.md (instance)

Is it an API key, token, or password?
  → .env or vault (instance, never git)
    vault keys are named by robothor/vault/naming.py, never by hand:
      providers/<provider-id>/api_key[_<N>]   LLM credentials, N >= 2 for spares
      channels/<channel>/<field>              channel tokens and secrets

Is it an agent configuration (schedule, model, tools)?
  → docs/agents/<name>.yaml (instance)

Is it an agent instruction (behavior, procedures)?
  → brain/agents/<name>.md (instance)

Is it a skill an agent wrote for itself?
  → brain/skills/<name>/ (instance) — written there by the engine,
    never by hand. agents/skills/ is the platform's own library.

Is it a platform tool, migration, or engine feature?
  → robothor/ or crm/ or infra/ (platform)

Is it a rule that ALL Claude Code sessions should follow?
  → CLAUDE.md root (platform)

Is it a rule specific to YOUR Claude Code sessions?
  → brain/CLAUDE.md (instance)

Is it a test fixture?
  → Use generic names: Alice, Bob, agent@example.com, test-tenant
```

## Enforcement

Three mechanisms prevent instance data from leaking into platform code:

1. **`.gitignore`** — `brain/*.md`, `brain/skills/`, `docs/agents/*.yaml`, `local/`, `.robothor/`, `.env*` are all excluded from tracking — every instance keeps them local.

2. **Pre-commit hook** (`check-instance-leak`) — Scans staged files for hardcoded user home directory paths, personal email addresses, phone numbers, and street addresses. Blocks the commit with clear messages.

3. **`DEFAULT_TENANT`** (`robothor/constants.py`) — All code uses `DEFAULT_TENANT` (from `ROBOTHOR_DEFAULT_TENANT` env var, default `"default"`) instead of hardcoding a tenant name.

## Operator Identity

The operator (the human who owns this Genus OS instance) is a first-class concept, but their personal data never enters the platform repo.

- **Path is platform** (hardcoded in `robothor.constants.owner_config_path()`): `~/.robothor/owner.yaml` — a user-level dotfile, intentionally independent of `ROBOTHOR_WORKSPACE` (which holds project data, not identity). Every instance looks in the same place.
- **Content is instance** (gitignored via `.robothor/`): the operator's name, emails, nicknames. Template at `templates/owner.yaml.example`.
- **Loaded by** `robothor.owner_config.load_owner_config()` on daemon startup and by resolvers that need to disambiguate the operator from other CRM contacts sharing a first name.
- **Linked to CRM** via `tenant_users.person_id` (migration `039_operator_identity.sql`), populated idempotently by `bootstrap_owner_person_links()` on daemon startup. One owner per tenant, DB-enforced by a partial unique index.

**Rules of thumb:**

| I want to... | Do this | Not this |
|--------------|---------|----------|
| Know who the operator is | `get_owner_person(tenant_id)` | `search_people("<first-name>")` |
| Send to the operator's email | `OwnerConfig.email` / `all_emails()` | Hardcode an email |
| Check "is the speaker the operator?" | `OwnerConfig.matches_name(name)` | First-name string equality |
| Refuse a write unless approved by the operator | `resolve_task()`'s `requires_human` path | Custom string checks |

Legacy env vars `ROBOTHOR_OWNER_EMAIL` / `ROBOTHOR_OWNER_NAME` remain as a fallback for one release cycle; the loader emits a `DeprecationWarning` when it falls back.

## The Agent Builder Flow

Agents are built using a CLI + Claude Code workflow:

1. **Scaffold**: `genus agent scaffold <name>` creates a manifest template and instruction file
2. **Refine**: Open Claude Code — the `AGENT_BUILDER.md` guide teaches it how to customize agents for your business
3. **Deploy**: `genus agent install <name>` activates the agent in the fleet
4. **Iterate**: Engine agents (Agent Architect, Nightwatch) can propose improvements via PRs

The Helm's agent builder (`/api/agent-manifests`) is the same flow without the
ssh session: it scaffolds from the same templates, validates with the same
checks, writes the same files, and then calls the engine reconcile so the agent
fires without a restart. See `docs/AGENT_BUILDER.md`.

Agent manifests and instructions are instance data — they stay in `docs/agents/` and `brain/agents/` (gitignored). Platform code provides the engine, tools, and templates.

Two sub-directories of `docs/agents/` are instance-owned and gitignored too:

| Directory | Written by | What it holds |
|-----------|-----------|---------------|
| `docs/agents/retired/` (instance) | `DELETE /api/agent-manifests/{id}` | Manifests taken off the fleet. Moved, never unlinked — re-instating an agent is a `mv` back. |
| `docs/agents/.history/<id>/` | every manifest write | The five previous versions of that agent's manifest, newest last. A convenience over `git`, not the backup story. |

Neither is visible to the engine: `load_manifest_dir` globs `*.yaml` one level
deep and non-recursively, so a retired or historical manifest can never be
loaded as a live agent.

## Skills

Skills split the same way, and for the same reason: an agent writes them while
it works, so they are instance data even though the platform ships a library of
its own.

| Directory | Layer | Tracked | Written by |
|-----------|-------|---------|------------|
| `agents/skills/` | Platform | Yes | Humans, in a pull request. The engine only ever reads it. |
| `brain/skills/` (`ROBOTHOR_INSTANCE_SKILLS_DIR`) | Instance | No | `create_skill`, `update_skill`, `skill_archive`, `genus import` |

The engine reads the bundled directory first and the instance directory second,
so a skill in the instance **shadows** a bundled one of the same name. That makes
`update_skill` on a bundled skill copy-on-write: the tracked file is never
rewritten, and the revision lands in the instance tree. Retirement
(`skill_archive`) moves a skill into the instance's `brain/skills/.archive/`,
so it can never delete a tracked file.

Which layer a skill belongs to is recorded in its `meta.json` as
`"origin": "instance" | "platform"`. Two things read it:

- `tests/test_no_tracked_instance_files.py` fails if a tracked skill under
  `agents/skills/` carries an instance origin — the leak this boundary exists to
  stop.
- `genus skills migrate-instance` moves exactly those out of the platform tree
  and into the instance one. It is idempotent, takes `--dry-run`, reports a
  name that already exists in the instance as a conflict rather than
  overwriting it, and leaves every platform-origin skill alone.

## Upgrade Path

How the platform code arrives depends on the substrate — a compose instance
moves a tag, a host instance moves a wheel — but one migrator moves the schema
either way, and the doctor is what says whether it landed.

```bash
pip install -U genusos     # Host install: the new platform code
genus migrate              # Apply whatever the new release added
genus doctor               # Exit 0, or it names what is wrong
```

`genus migrate --status` shows the ledger with the provenance of every row, and
the migrator holds an advisory lock and verifies each file's checksum. Compose
upgrades are a tag edit and one `up -d`, with the one-shot `migrate` service
holding the platform services until it exits 0 — see
[`docs/deployment.md`](deployment.md).

Upgrades touch platform code only. Your instance's `brain/`, agent configs, and `.env` are untouched. If a template has been updated (e.g., a new field in `templates/SOUL.md`), the upgrade shows a diff and lets you decide whether to adopt it.
