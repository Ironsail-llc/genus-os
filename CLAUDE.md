# Genus OS — Project Root

Genus OS is a deterministic AI agent platform. Users deploy instances with their own identity, agents, and workflows.

`CLAUDE.md` is the canonical instruction file for every coding agent here; `AGENTS.md` points at it.

## Quick Start

```bash
pip install -e ".[dev]" && pre-commit install                  # once
pytest tests/ robothor/ -m "not slow and not llm and not e2e and not integration"   # fast unit baseline
ruff check robothor/ bench/ scripts/ && mypy robothor/ --ignore-missing-imports
```

Every command, by phase: `.claude/rules/commands.md`.

## Platform vs Instance

This is the most important concept in the codebase. Getting it wrong leaks personal data into git.

| Layer        | Location                                               | Tracked | Purpose                                   |
| ------------ | ------------------------------------------------------ | ------- | ----------------------------------------- |
| **Platform** | `robothor/`, `crm/`, `infra/`, `app/`, `docs/*.md`     | Yes     | Core engine, tools, migrations, dashboard |
| **Instance** | `brain/`, `docs/agents/*.yaml`, `local/`, `.robothor/` | No      | Identity, agent configs, memory, secrets  |

**The rule**: Platform code must never contain instance-specific data — no personal names, email addresses, domains, phone numbers, home addresses, hardcoded tenant IDs, or absolute paths. Use environment variables and constants instead.

**This repository is public**, so the boundary covers deployment detail as well as personal data, and prose as well as files — commit messages, pull request bodies and release notes are all published, and the leak hooks scan only changed files. See [Platform vs Instance](docs/PLATFORM_INSTANCE.md#this-repository-is-public).

### What goes where

| I want to...                                                              | Put it in...                                           | Why                               |
| ------------------------------------------------------------------------- | ------------------------------------------------------ | --------------------------------- |
| Add a new engine tool                                                     | `robothor/engine/tools/`                               | Platform — all instances get it   |
| Configure my agent fleet                                                  | `docs/agents/*.yaml` + `brain/agents/*.md`             | Instance — survives upgrades      |
| Add a platform guardrail                                                  | Root `CLAUDE.md` (this file)                           | Platform — ships to everyone      |
| Add a personal rule for Claude                                            | `brain/CLAUDE.md`                                      | Instance — only your machine      |
| Write test fixtures                                                       | Use generic names (Alice, Bob, agent@example.com)      | Platform — no personal data       |
| Reference the operator                                                    | Say "the operator" not a personal name                 | Platform — works for any user     |
| Mention a deployment host, namespace, GitOps app, secret path or issue id | Describe it generically, or name the values key        | Platform — this repo is public    |
| Set a tenant ID default                                                   | `os.environ.get("ROBOTHOR_DEFAULT_TENANT", "default")` | Platform — env var, not hardcoded |
| Reference workspace path                                                  | `os.environ.get("ROBOTHOR_WORKSPACE", ...)`            | Platform — never `/home/username` |

The one exception for an issue id is the ticket key at the end of a PR title and commit subject (see Git Conventions).

## Rules

1. **Never embed instance data in platform code** — no names, emails, domains, addresses, phone numbers, or hardcoded tenant IDs. Use `DEFAULT_TENANT` from `robothor/constants.py`, `ROBOTHOR_WORKSPACE` env var, and generic test fixtures. This is the #1 cause of data leaks.
2. **Canonical paths** — use `os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor")` — never hardcode absolute paths like `/home/username/robothor`.
3. **Never commit secrets** — SOPS-encrypted, decrypted to tmpfs at runtime. Use `os.getenv()` in Python, `$VAR` in shell. Gitleaks pre-commit hook enforces this.
4. **Engine is the execution layer, manifests are source of truth** — all agents run via `robothor/engine/`. YAML manifests in `docs/agents/` are canonical config.
5. **System-level systemd services** — every long-running process in `/etc/systemd/system/`, enabled on boot. `Restart=always`, `RestartSec=5`.
6. **Manifests are source of truth for models** — check `docs/agents/*.yaml` `model:` blocks for current assignments.
7. **No localhost URLs in agent instructions** — engine's `web_fetch` blocks loopback. Localhost is fine in internal code and infra docs.
8. **TDD — write the failing test first.** RED (failing test, confirm it fails) → GREEN (minimum code to pass) → REFACTOR (keep tests green). Exempt: typos, docs, config-only edits. Test commands and conventions: `CONTRIBUTING.md`; phases and commits: `.claude/rules/tdd.md`.
9. **Update docs with the change** — see `docs/DOC_MAINTENANCE.md` for the checklist.
10. **Async boundaries** — engine internals (`robothor/engine/`) are fully async. `asyncio.run()` only in entry points (daemon.py, cli.py) and standalone scripts.
11. **Instance data is user-land** — `brain/`, `docs/agents/*.yaml`, and `docs/CRON_MAP.md` are .gitignored. They belong to the instance, not the platform. Agent configs survive platform upgrades.
12. **Operator identity comes from `~/.robothor/owner.yaml`** — load it via `robothor.owner_config.load_owner_config()`, resolve the operator's CRM row via `robothor.crm.dal.get_owner_person(tenant_id)`. **Never search for the operator by first name** (e.g. `search_people("<operator-first-name>")`) — use `prefer_owner=True` if you must, or call `get_owner_person()` directly. The `.robothor/` dir is gitignored; the path is platform-hardcoded. New instances copy `templates/owner.yaml.example`.

## Core Workflow: Ticket First, Test First

**Non-negotiable.** No work starts without a ticket in the project's Jira (see Documentation Requirement). No behaviour changes without a failing test first: RED, GREEN and REFACTOR are separate phases with separate commits (`.claude/rules/tdd.md`).

## Context Loading Strategy

Claude Code reads every `CLAUDE.md` in the directory tree, and loads `.claude/rules/` automatically. Other agents read the same files themselves (`AGENTS.md`).

| Tier        | Location                                                          | When loaded                                     |
| ----------- | ----------------------------------------------------------------- | ----------------------------------------------- |
| Always      | `CLAUDE.md` (this file), `.claude/rules/tdd.md`, `.claude/rules/commands.md` | Every session                                   |
| Path-scoped | `robothor/engine/CLAUDE.md`, `crm/CLAUDE.md` (subsystem rules, tracked)      | When working in that directory                  |
| Instance    | `brain/CLAUDE.md` (instance rules, gitignored)                    | Every session, if present                       |
| On-demand   | `docs/PLATFORM_INSTANCE.md`, `docs/DOC_MAINTENANCE.md`, `CONTRIBUTING.md`    | Before touching the boundary, docs, or tests    |

Your `brain/CLAUDE.md` is where personal identity, preferences, and instance-specific rules go. It never enters git. To create one for a new instance, run `robothor init` or copy from `templates/`.

## Per-Feature Design Checklist

Answer these BEFORE writing code or tests:

| Question                                                   | If yes                                                                                                     |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Writes anything tracked, or PR prose?                      | Rule 1 and the public-repo table in `docs/PLATFORM_INSTANCE.md`. Generic fixtures only.                     |
| New or re-scoped engine tool?                              | `docs/TOOLS.md`, with the rule for choosing it over any overlapping tool.                                  |
| Async code in `robothor/engine/`?                          | Rule 10. No blocking I/O on the loop.                                                                      |
| New table or column?                                       | A new migration + `robothor/migrations/manifest.txt`. Never edit an applied migration.                     |
| Tenant-scoped data?                                        | Take `tenant_id`; default from `DEFAULT_TENANT`. Never a literal.                                          |
| Needs the operator?                                        | Rule 12: `get_owner_person()`, never a name search.                                                        |
| Calls an LLM or external service?                          | Define the fallback; don't swallow errors. Mock LLMs in unit tests; test by properties, not exact output.  |
| Risky behaviour change?                                    | An off/observe/enforce flag in `infra/flags.yaml` + a runbook (`docs/runbooks/GUARDRAIL_FLIPS.md`).        |
| New setting or CLI verb/flag?                              | Regenerate `docs/reference/configuration.md` / `docs/reference/cli.md` (`.claude/rules/commands.md`).      |
| Dashboard (`app/`) change a user would see?                | Vitest for logic; Playwright for the user flow.                                                            |
| An operator, admin or agent author would notice it?        | A release-note fragment in `changelog.d/` (`changelog.d/README.md`).                                       |

## Quality Gates

| Phase           | What to run                                                                                                                 |
| --------------- | --------------------------------------------------------------------------------------------------------------------------- |
| **Development** | RED → GREEN → REFACTOR, one module at a time: `pytest robothor/<module>/tests/ -v`                                           |
| **Pre-commit**  | The hooks (gitleaks, ruff, instance-leak, CLAUDE.md size, deleted refs) and commitlint. Never `--no-verify`.                |
| **Pre-push**    | ruff check + format check, mypy, the unit suite, doc link and command checks, fragment lint; `app/` lint, tsc and vitest     |
| **PR**          | CI green, `PR Title` check, `Release Preview` comment read, both reviews requested, every thread answered                     |
| **Post-deploy** | Check the deployment's logs and traces for new errors (`docs/OBSERVABILITY.md`)                                             |

## Documentation Requirement

Every task is documented in the project's Jira, not in a file in this repo:

**BEFORE starting:** a ticket with the objective, approach, files to modify, and success criteria.
**AFTER completing:** a comment on the ticket with what changed, tests added, PR links, and status.

Docs in this repo still change with the code (rule 9). A PR without a ticket can be blocked when the review bot's ticket requirement is on.

## Reviews and Merging

- Every PR gets the review bot requested as reviewer, and the second reviewer triggered by its PR-comment trigger. The exact commands are deployment detail and live in the operator's private instructions.
- **Nothing merges without the operator's explicit approval.** Agents never merge and never push to `main`.
- Answer every review thread — fix it and name the commit, or reply with the reason you won't. Re-request review after each push.
- A push dismisses earlier approvals. That is expected: re-request, don't ask for the setting to change.

## Git Conventions

[Conventional Commits](https://www.conventionalcommits.org/), enforced by commitlint (commit-msg hook and CI) and the `PR Title` check.

- **PR title:** `type(scope): description (KEY-N)` — scope is a subsystem (`engine`, `crm`, `helm`, `app`, `ci`), never the ticket. Subject 1–72 characters, key included. It becomes the squash commit on `main`.
- **Commit subjects** on the branch end with the same `(KEY-N)`. The key appears nowhere else: not in code, docs, tests, fragments or the PR body.
- The subject starts lowercase: commitlint rejects sentence case (`Add …`, `CI's …`) in branch commits.
- Asked to commit without a type? Infer it: bug fix → `fix`, feature → `feat`, tests only → `test`.

## Operator Best Practices

These come from production experience running the first Genus OS instance:

- **Limit delivery agents** — only 2-3 agents should talk to the operator directly. Workers use `delivery: none` and communicate via CRM tasks and notifications.
- **Cloudflare tunnel for port-bearing services** — sensitive services use Cloudflare Access (email OTP). Public services have no auth.
- **Heartbeat, not polling** — use the main agent's heartbeat for decision-only updates, not per-agent delivery.

## Releases & Deployment

- **`main` is the only release branch.** Squash-merge only — the PR title becomes the commit on `main`, so it must parse as a Conventional Commit. The `PR Title` check enforces this; merge button stays disabled otherwise.
- **No release on merge?** The `Release Preview` sticky PR comment tells you in advance whether your PR will trigger a release. `chore:` / `docs:` (non-README) / `style:` / `test:` / `ci:` / `build:` do **not** bump versions. Change the type to `feat:` / `fix:` / `perf:` if a deploy is expected.
- **Production:** semantic-release on `main` produces a `vX.Y.Z` tag, GitHub Release, CHANGELOG bump, and updates `helm/genus-os/values-production.yaml`. ArgoCD syncs the production Application.
- **Staging is label-driven, single slot.** Add the `deploy-staging` label to a PR → builds image `pr-N-sha-<short>`, commits the bump to `helm/genus-os/values-staging.yaml` on `main` (with `[skip ci]`), syncs the staging Application. Removing the label or merging the PR resets staging to the latest `vX.Y.Z`. If another PR currently holds staging, the deploy is refused with a comment — unlabel the other PR first.
- **`git log helm/genus-os/values-staging.yaml`** is the staging deploy ledger. **`git tag --list 'v*'`** on `main` is the production release ledger.
- **Image tags:** GHCR, no `:latest`. Production pins exact `vX.Y.Z`; staging pins `pr-N-sha-<short>`.
- **Full conventions** live with the deployment, not here — see your infrastructure repo's release-and-deploy doc. This repo owns the chart and the pipeline; it does not describe any one deployment.

## AI Assistant Rules

### Do NOT

- Start work without a ticket in the project's Jira
- Write implementation before a failing test, or delete or weaken a test to get green
- Put instance data, hostnames, namespaces, company names or ticket ids (outside the PR title and commit subjects) in anything tracked or in PR prose
- Commit secrets or `.env` files, or bypass a hook with `--no-verify`
- Hand-edit generated files: `CHANGELOG.md`, `docs/reference/cli.md`, `docs/reference/configuration.md`
- Merge a PR, push to `main`, or treat a review bot's approval as the operator's

### Always

- Use `DEFAULT_TENANT`, `ROBOTHOR_WORKSPACE` and generic fixtures (Alice, Bob, `agent@example.com`)
- Update docs and write the release-note fragment in the same PR
- Request both reviews when the PR opens; answer every thread; re-request after each push
- Comment on the ticket when the work ends, or when you stop mid-way

### When Resuming Work

1. Read the ticket and its latest comment, then the open PR and its unresolved review threads (`gh pr view <n> --comments`)
2. Check `git status` and `git log origin/main..HEAD` for uncommitted or unpushed work
3. Run the fast unit suite to confirm the baseline before changing anything

## Quick Reference

- **Platform vs instance boundary**: `docs/PLATFORM_INSTANCE.md`
- **Contributing guidelines**: `CONTRIBUTING.md`
- **System map, reading guide**: `docs/READING_GUIDE.md`
- **Doc update checklists**: `docs/DOC_MAINTENANCE.md`
- **System architecture**: `docs/SYSTEM_ARCHITECTURE.md`
- **Onboarding new instances**: `templates/CLAUDE.md`

<!--
CLAUDE.MD MAINTENANCE
======================
These instructions apply ONLY when the user explicitly asks to update CLAUDE.md.

SIZE LIMIT: 210 lines, 18k characters. Check: wc -lc CLAUDE.md
(Subsystem CLAUDE.md files are capped at 80 lines by the claude-md-size hook.)

The rule numbers 1-12 are cited by code and tests ("CLAUDE.md rule 1"). Never renumber them; append.
The review bot reads only this file, from the base branch, so a rule reviewers must apply goes here.

WHERE TO PUT NEW CONTENT:
- TDD rule: .claude/rules/tdd.md
- CLI command: .claude/rules/commands.md
- Subsystem rule: robothor/engine/CLAUDE.md or crm/CLAUDE.md
- Personal or instance rule: brain/CLAUDE.md (gitignored)
- Doc-update rule: docs/DOC_MAINTENANCE.md
- Checklist item: Per-Feature Design Checklist above
- "Do not" rule: AI Assistant Rules > Do NOT above

NEVER ADD: status info, ticket ids, deployment detail, bot account names, verbose code examples, duplicate instructions
-->
