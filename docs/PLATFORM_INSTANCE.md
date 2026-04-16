# Platform vs Instance — Architectural Boundary

Genus OS separates **platform code** (what ships to everyone) from **instance configuration** (what's personal to each deployment). This document explains the model, how it's enforced, and how to make decisions about where new code belongs.

## Three Layers

| Layer | What | Tracked in git? | Examples |
|-------|------|-----------------|----------|
| **Platform** | Core engine, tools, migrations, dashboard, docs | Yes | `robothor/`, `crm/`, `infra/`, `app/`, `docs/*.md` |
| **Instance** | Identity, agent configs, memory, secrets | No | `brain/`, `docs/agents/*.yaml`, `local/`, `.env` |
| **Runtime** | Session state, assembled prompts, tenant context | No (in-memory) | System prompts, warmup blocks, scratchpad |

Platform upgrades (`git pull` + `robothor upgrade`) only touch Layer 1. Layers 2 and 3 are untouched.

## How Claude Code Sees Both Layers

Claude Code reads `CLAUDE.md` files from the directory tree:

- `CLAUDE.md` (root) — **Platform rules**. Tracked. Every clone gets this.
- `brain/CLAUDE.md` — **Instance rules**. Gitignored. Personal identity and operator preferences.
- `robothor/engine/CLAUDE.md` — **Subsystem rules**. Tracked. Engine-specific dev guidance.
- `crm/CLAUDE.md` — **Subsystem rules**. Tracked. CRM-specific dev guidance.

Claude sees all of them in a session, but only the tracked files enter git. Your personal `brain/CLAUDE.md` stays local.

## How Engine Agents Access Rules

Engine agents don't read `CLAUDE.md` files — they use:

- **Instruction files** (`brain/agents/*.md`) — per-agent behavior rules
- **Warmup context** — memory blocks, status files, peer agent state
- **Tool calls** — `read_file` to access `docs/*.md` when they need reference material
- `brain/SOUL.md` — personality injected via warmup

## Decision Tree: Where Does This Go?

```
Is it personal data (name, email, phone, address)?
  → brain/CLAUDE.md or brain/SOUL.md (instance)

Is it an API key, token, or password?
  → .env or vault (instance, never git)

Is it an agent configuration (schedule, model, tools)?
  → docs/agents/<name>.yaml (instance)

Is it an agent instruction (behavior, procedures)?
  → brain/agents/<name>.md (instance)

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

1. **`.gitignore`** — `brain/*.md`, `docs/agents/*.yaml`, `local/`, `.robothor/`, `.env*` are all excluded from tracking.

2. **Pre-commit hook** (`check-instance-leak`) — Scans staged files for hardcoded user home directory paths, personal email addresses, phone numbers, and street addresses. Blocks the commit with clear messages.

3. **`DEFAULT_TENANT`** (`robothor/constants.py`) — All code uses `DEFAULT_TENANT` (from `ROBOTHOR_DEFAULT_TENANT` env var, default `"default"`) instead of hardcoding a tenant name.

## Operator Identity

The operator (the human who owns this Genus OS instance) is a first-class concept, but their personal data never enters the platform repo.

- **Path is platform** (hardcoded in `robothor.constants.owner_config_path()`): `<ROBOTHOR_WORKSPACE>/.robothor/owner.yaml`. Every instance looks in the same place.
- **Content is instance** (gitignored via `.robothor/`): the operator's name, emails, nicknames. Template at `templates/owner.yaml.example`.
- **Loaded by** `robothor.owner_config.load_owner_config()` on daemon startup and by resolvers that need to disambiguate the operator from other CRM contacts sharing a first name.
- **Linked to CRM** via `tenant_users.person_id` (migration `039_operator_identity.sql`), populated idempotently by `bootstrap_owner_person_links()` on daemon startup. One owner per tenant, DB-enforced by a partial unique index.

**Rules of thumb:**

| I want to... | Do this | Not this |
|--------------|---------|----------|
| Know who the operator is | `get_owner_person(tenant_id)` | `search_people("Philip")` |
| Send to the operator's email | `OwnerConfig.email` / `all_emails()` | Hardcode an email |
| Check "is the speaker the operator?" | `OwnerConfig.matches_name(name)` | First-name string equality |
| Refuse a write unless approved by the operator | `resolve_task()`'s `requires_human` path | Custom string checks |

Legacy env vars `ROBOTHOR_OWNER_EMAIL` / `ROBOTHOR_OWNER_NAME` remain as a fallback for one release cycle; the loader emits a `DeprecationWarning` when it falls back.

## The Agent Builder Flow

Agents are built using a CLI + Claude Code workflow:

1. **Scaffold**: `robothor agent scaffold <name>` creates a manifest template and instruction file
2. **Refine**: Open Claude Code — the `AGENT_BUILDER.md` guide teaches it how to customize agents for your business
3. **Deploy**: `robothor agent install <name>` activates the agent in the fleet
4. **Iterate**: Engine agents (Agent Architect, Nightwatch) can propose improvements via PRs

Agent manifests and instructions are instance data — they stay in `docs/agents/` and `brain/agents/` (gitignored). Platform code provides the engine, tools, and templates.

## Upgrade Path

```bash
robothor upgrade          # Pull latest platform + run new migrations
robothor upgrade --dry-run  # Preview what would change
```

Upgrades touch platform code only. Your `brain/`, agent configs, and `.env` are untouched. If a template has been updated (e.g., a new field in `templates/SOUL.md`), the upgrade shows a diff and lets you decide whether to adopt it.
