# Productize Genus OS: gap analysis vs OpenClaw v2 + Grok Bot, and the build plan

## Context

Philip's ask (2026-09-10): look deeply at OpenClaw v2 and Grok Bot, inventory their
features and how they work, produce a gap analysis against Genus OS, and plan to build
the gaps. The framing that matters: **he cannot install Genus OS for his own enterprise
today** because too many pieces are unclear or underdeveloped, while Grok Bot is trivially
easy and OpenClaw is getting feature-dense. The deliverable is productization: UI,
configurability, install-and-run-reliably-in-a-company.

What is already known (memory, 2026-08-24 sweep):
- Genus leads on memory, reliability, code quality, multi-agent, security design,
  agent-quality observability. It trails on plugins (marketplace), distribution, and
  the runner architecture.
- WildClawBench: parity-to-lead on Safety, near-parity on Social. Performance is not the
  blocker; **installability is**. The bench attempt found two fresh-install defects the
  box hid (container could not make an LLM call; fresh DB denied every agent every tool).
- Standing goal: Genus OS a front-runner vs OpenClaw / Hermes / DeepSeek harness; plugin
  architecture is the next cycle; Apollo/Impetus/PF must leave core.
- Philip decides from clickable mockups, not abstract questions.

## Summary (read this first)

**Finding.** Genus OS already has more enterprise substance than OpenClaw v2 or Grok
Bot: tenancy, RLS, fail-closed RBAC, audit + SIEM, rootless sandbox, durable memory,
agent-quality measurement. None of it is reachable without reading source. The
published quickstart is dead on line 1 (`pip install robothor`; the package is
`genusos`), six install paths overlap and none completes, `init` never creates the
owner file, Telegram is hard-required, SOPS is hard-required on systemd, 91 of 137
`ROBOTHOR_*` settings are documented nowhere, and the dashboard has two pages with no
editor for agents, channels, providers, users, or settings. OpenClaw's 2.0 was literally
"fix install and it became 2.0". Grok Bot's whole advantage is twelve onboarding
decisions (one credential, agent = three fields, chat is the UI, automation derived
from a task). Copy their onboarding shape, not their security posture.

**Plan.** Three workstreams, ~50 PRs, three falsifiable phase exits:
- **A — Zero to running:** `genus init` two-phase wizard with a real completion probe,
  `genus doctor --fix`, one typed settings model with a test that makes undocumented
  env vars impossible, one secrets accessor, one migrator, one manifest validator
  (fail-closed), a nightly fresh-install CI gate driven by the quickstart's own commands.
- **B — The Helm becomes the product:** browser setup wizard, local login + MFA,
  Settings (providers with Test connection, channels with verify, users/roles, secrets
  write-only, schema-driven config, all flags, plugins), agent create/edit as three
  fields + advanced drawer writing manifests, chat-first home with inline approvals,
  Inbox, Automations with ran/delivered/completed, Memory/Audit/Logs views.
- **C — Reach:** Channel protocol (Slack delivery, Teams plugin, web chat per user,
  email, `ask_user`), plugin install/lockfile/signed registry, agent bundle share,
  docs as product with CI-checked commands, PyPI + install.sh + Helm OCI.

**Phase exits:** (1) a clean container follows the quickstart to chat with a real
completion, nightly in CI; (2) an admin creates an agent, schedules it, connects Slack,
invites a user, and reads run truth without a terminal; (3) one-liner install on a
company box or cluster, PyPI + OCI published, second channel is a plugin.

**Twelve defaults chosen** (table at the end) — Compose pilot, Slack first, local login
with owner MFA, `genus` verb, pydantic-settings, env-file secrets, GitHub Pages registry,
close RFC #267, observe-mode on the prod box, retire-not-delete, no UI restarts, no
hosted offering this cycle.

---

## Research findings

### OpenClaw v2 (v2026.8.1, released 2026-08-30; latest v2026.9.3 on 2026-09-08)
Sources: docs.openclaw.ai/releases/2026.8.1 (15 sections), openclaw.ai/blog/openclaw-2-accidentally,
docs.openclaw.ai/{start/wizard,gateway/configuration,web/control-ui,start/teams,gateway/security}.
389k stars, 81.8k forks, ~1,000 contributors on v2, 16,977 PRs in one release.

**Why it's "2.0":** the team set out ONLY to simplify installation and the browser UI;
that cleanup cascaded into the whole product. Installability was the release.

**Install path:** `curl -fsSL https://openclaw.ai/install.sh | bash` (or npx / npm /
brew-ish / Docker / k8s / Nix / a dozen cloud one-clicks). Then the Quick Start wizard:
(1) one-line security ack, (2) read-only scan for existing API-key env vars, local AI
CLIs (Claude Code / Codex logins), tool-capable Ollama / LM Studio models, (3) pick a
provider and **the route is tested with a real completion** before proceeding,
(4) optional extras, (5) save, (6) start gateway and open the dashboard **straight into
a chat**. Headless/SSH installs get an authenticated link + port-forward instructions.
`openclaw onboard --install-daemon` does wizard + systemd/LaunchAgent in one go.
Non-interactive setup validates every choice before writing anything and returns
`--json`. Stated target: five minutes.

**Configurability:** one JSON5 file `~/.openclaw/openclaw.json`, optional (safe defaults
if absent). Two-bucket schema: root = infra + cross-agent defaults; `agents.defaults` =
loop behaviour; `agents.entries.<name>` = per-agent overrides. Edited four ways: wizard
(`openclaw configure`), CLI (`config get/set/patch/unset/validate/schema`), Control UI
Config tab, or direct file edit with **auto hot-reload** (invalid reload skipped, last
good kept; restart-only for gateway mode/plugin load). **Strict schema: unknown key =
gateway refuses to start** with file/line/path/allowed-values in the error. Destructive
change detection. `.bak` ring. Secrets via `${VAR}` + four ref schemes (env/file/exec/
store), Vault/1Password refs, team-scoped Secret Store (NOT encrypted at rest).
`openclaw doctor --fix` migrates and repairs. `openclaw security audit` = drift from
secure defaults with auto-fix.

**Channels:** 31+. Telegram/WebChat bundled; Discord/Slack/WhatsApp/Signal/Teams/
iMessage/Matrix/Google Chat/... are official plugins installed with one command
(`openclaw channels add --channel slack --token ...`). Pairing/allowlist/open DM modes.
Slack Enterprise Grid. `ask_user` renders as native buttons on Telegram/Discord/Slack.

**Control UI (rebuilt, 575 ms cold start):** Chat, Sessions, New Session (workspace +
placement picker), Settings (identity, Plugins, Skills, MCP, Memory w/ live status,
Sessions, Connected accounts), Inbox (alerts+approvals), Automations dashboard (full
CRUD, run, pause, failure routing), file editor, Changes panel (git/PR/CI), Browser
panel, Desktop panel, web terminal, Skill Workshop, Goals, Cmd-K. Plugin
browse/install/enable/disable from UI with Safe/Review/Blocked scan verdict. Advanced
plugin config stays CLI-only ("what the web form cannot safely represent").

**Agents:** workspace (SOUL.md/AGENTS.md/USER.md) + state dir + SQLite sessions.
Bindings route (channel, account, peer) → agent, most-specific-wins. Sub-agents, ACP
external agents, swarm progress in chat. Agent Plugins = bundles of config + workspace
files + skills + MCP servers, previewed as a plan before apply.

**Skills:** AgentSkills-spec SKILL.md dirs (same format we use), ClawHub registry with
scan verdicts, `$skill` references, and the new **Skill Workshop**: agent proposes skill
edits from lessons, operator reviews diff + scanner + benchmark output, applies /
rejects / quarantines. New installs default to `auto` learning mode.

**Automations:** cron → "Automations". Triggers: schedule, conditions, monitored
event streams, `/loop`. History separates ran / delivered / completed. Auto-disable
after 10 consecutive failures. Restart recovery replays missed slots. Gmail + IMAP
watchers (IMAP read-only, allowlisted).

**Memory:** built-in LanceDB + SQLite-vec + MEMORY.md/USER.md + Memory Wiki. Per-agent
scoped; network-derived memory flagged untrusted; `memory forget` with preview.
Retired: reranking, query expansion, cross-agent search (we have all three).

**Enterprise:** "one trust boundary per gateway" — mutually-trusting team only,
separate gateways for distrust. Team ingress: Tailscale identity (recommended),
trusted proxy (Cloudflare Access), or shared secret. Roles (`gateway.roles`: scopes
operator.read/write/approvals, session others: view/read/write, sandbox: required).
Session permission modes read-only/guarded/workspace/full. Approvals durable across
surfaces with standing grants. **No native SSO/SAML/OIDC documented. No
Prometheus/OTel documented. Sandbox OFF by default. Multi-tenant = not supported.**
Backup: `openclaw backup create|verify|restore`. Updates: `openclaw update --dry-run`,
rehearsed in isolated candidate state (9.3).

**Criticisms:** The Register "pours glitter on a slow-burning security dumpster fire";
secret store plaintext; sandbox off; ClawHavoc supply-chain (341 malicious skills);
repeated prompt-injection exfil papers; HN: "primary usecase is fixing whatever broke
this week"; docs "intimidating"; upgrade breakage reports on 2.0 migration.

**Unverified:** ClawHub true package count (site shows 30 skills/12 plugins; blogs
claim 13k), npm downloads, SSO, audit export, metrics.

### Grok Bot (xAI / SpaceXAI, beta 2026-08-11, in all paid Cursor + SuperGrok plans since 2026-08-26)
Sources: docs.x.ai/grok-bot/*, x.ai/news/introducing-grok-bot, VentureBeat, Vellum breakdown.

**What it is:** a desktop/mobile chat app where you create named "Bots" (durable AI
teammates). Every Bot runs on ONE persistent, account-scoped cloud computer (Firecracker
microVM) with a browser, filesystem, terminal, and your real logged-in sessions. Closed
source, cloud-only, xAI models only, no self-host, no API, no model picker.

**Install experience (the thing Philip is reacting to):**
1. Download app (dmg / msi / deb / AppImage / iOS / Android). No CLI, no package manager.
2. Sign in with Cursor (OAuth). Org SSO is inherited from the org's existing Cursor SAML
   app. **That is the only credential asked for at install.** No model keys, no OAuth
   apps, no webhooks, no config file.
3. Cloud computer provisions silently while you answer a short "which tools do you use"
   questionnaire; answers seed suggested Bots.
4. Create a Bot = **three text fields**: Name, Job, Description (rules/boundaries).
5. Type a task in plain English. Target: first result in five minutes.
6. Credentials are acquired **lazily**: when the Bot hits a login/2FA/CAPTCHA it stops,
   you "take over" the screen for ten seconds, type the password, hand control back.
7. A one-off task can be promoted to a Skill (or taught by recording 10 min of browsing),
   then bound to a Routine (schedule + timezone + approval boundaries + Active toggle).

**Feature inventory:** account-wide Plugins marketplace (Gmail, GCal, Drive, Outlook,
Teams, Slack, Salesforce, Notion, HubSpot; BYO MCP but must be publicly reachable);
browser-first for everything else; group chats of 2-6 Bots; per-Bot memory (no
inspection/export UI); routines 50/Bot, 20 run records kept; per-action approvals +
saved "Auto Review" rules; mandatory human takeover for secrets/payments; Team Rules;
Enterprise: SAML+SCIM, network destination allowlists, 90-day action recording, audit
logs; ISO 27001/42001; US-only residency; template marketplace (69 templates) with
"Add to Grok Bot" one-click clone minus credentials.

**Weaknesses (documented):** the shared computer is not a security boundary (xAI says so);
deleting a Bot leaves its sessions/files/creds behind; approval fatigue; injection track
record at xAI; no model choice; no memory UI; no benchmarks published; no self-host.

**Why it wins on ease (the 12 decisions we can copy the *shape* of):** hosted, one
credential, browser-first coverage, human takeover as the auth primitive, agent = 3
fields, chat app is the whole UI, automation is derived from a task not authored in a
builder, shared state as UX, no model picker, bundled distribution, SSO by inheritance,
share-link template cloning.

### Genus OS today: install, config, UI (repo audit @ 543d709f29, v1.65.0; key claims spot-verified)

**Install paths: six, overlapping, none complete as documented.**
- `docs/quickstart.md:15,28,131` and `docs/deployment.md:73` say `pip install robothor`;
  `pyproject.toml:6` is `name = "genusos"`. The published Getting Started is dead on line 1.
- `robothor init` (`robothor/setup.py:116`, 13 resumable steps) treats Postgres/Redis/
  Ollama as optional prereqs, writes a 16-line `.env` with no provider key, **never
  creates `~/.robothor/owner.yaml`** (and `templates/owner.yaml.example:4` documents the
  wrong path vs `robothor/constants.py:52`), never creates `.robothor/config.yaml`.
- systemd: `docs/deployment.md:57` says hand-copy units; `scripts/install-units.sh:5-9`
  says that is the bug (units have unexpanded `${ROBOTHOR_WORKSPACE}`); the real path
  (`install-units.sh`, `render-unit.sh`, `infra/setup.sh`) is in no top-level doc. Engine
  unit hard-requires SOPS (`ExecStartPre=decrypt-secrets.sh` → `/etc/robothor/secrets.enc.json`),
  and `.sops.yaml` pins the maintainer's own age key. No doc explains SOPS setup.
- App Docker Compose (`infra/docker-compose.apps.yml`) exists and is referenced only by CI.
- Helm (`helm/genus-os/`) is best documented but `/ready` requires `docs/agents/main.yaml`,
  which is not in the repo and only `robothor agent install` renders it.
- `docs/DEPLOYMENT.md` vs `docs/deployment.md` case-collide (repo can't check out on
  macOS/Windows defaults); the uppercase one is a stale README copy.
- Migrations: THREE mechanisms — `robothor migrate` (canonical, manifest of 113, checksummed),
  `robothor upgrade` (globs `*.sql`, no checksum/lock, own state file), docs say
  `psql -f 001_init.sql`; plus `examples/full-stack/init-db.sql` frozen snapshot.
- No `robothor doctor`. `config validate` (`robothor/config.py:405`) hard-REQUIRES
  `ROBOTHOR_TELEGRAM_BOT_TOKEN` + `_CHAT_ID`; there is no documented Telegram-free deploy.
- README step 2-3: copy ONBOARDING.md to CLAUDE.md and "open Claude Code" — onboarding
  requires Claude Code. And setup.py scaffolds a *different, divergent* copy.

**Configuration: 228 env vars, four secret stores, two validators, 68 flags.**
- 137 `ROBOTHOR_*` vars read; **91 documented nowhere**; 7 documented ones are dead.
- 39 `*_ENABLED` + 29 `*_MODE` flags; only 21 governed in the DB store / UI
  (`robothor/flags/store.py:18`); 47 are env-only + restart.
- Manifests: `docs/agents/schema.yaml` used only by a repo script; the engine's
  `config_schema.py:264` validator is hand-written, never reads schema.yaml, fail-open
  (never raises). README says schema is enforced at startup. It is not.
- Secrets: AES vault (`robothor/vault/`) is read at runtime ONLY by `auth/tokens.py`;
  provider/channel creds come from env. `docs/ONBOARDING.md` gates readiness on the vault.
  Also SOPS, K8s VSO, plain `.env`. Key pool knows one provider prefix (`key_pool.py:108`).
- Same concept twice: Telegram token (2 env names), Ollama URL (3 names), owner identity
  (owner.yaml vs deprecated env, init writes the deprecated one), flags (4 places).
- Precedence chain `_defaults.yaml → manifest → .robothor/config.yaml → env → runtime`
  documented only in `templates/AGENT_BUILDER.md:430`.

**The Helm (app/): 2 pages, 10 client-side views, mostly read-only.**
Exists: Dashboard (LLM canvas), Tasks (approve/reject), Agents (RO list), Marketplace
(agent templates from programmaticresources.com — hardcoded SaaS URL), Controls (21
flags), Fleet (RO), Runs (trace viewer, good), Workflows (RO), Health (RO), Canvas, chat
panel (SSE), memory-search component (no sidebar entry), routines-manager component.
**Missing: agent create/edit, channel setup, provider/key management ("The Next.js
dashboard has no model-provider credentials" — app/README.md:41), users/roles, general
settings, onboarding wizard, plugin management, logs, audit view.** Auth = OIDC or
Cloudflare Access only; with neither configured the sign-in page has no buttons; escape
hatch is `GENUS_INSECURE_DEV_MODE`. No local login.

**Channels:** Telegram (deep, 1,800+ lines), Slack inbound-command only, email via
external `gws` CLI (OAuth undocumented), web chat. `deliver()` routes only to telegram
and event_bus. Voice/SMS units documented in SERVICES.md do not ship. No Discord/
WhatsApp/Signal/Teams/Matrix.

**Plugins:** host is good (10 entry-point groups, contract version, governance manifest
`genus-plugin.yaml`, SIGHUP reload, PR #462) but: 1 plugin ships (`genus-hostinfo`),
CLI is list-only, no registry, `docs/PLUGINS.md` unpublished. RFC #267 still open.

**Enterprise substance is real but 100% CLI/SQL and undocumented:** tenants, 6 roles
(duplicated in 3 files), RBAC fail-closed, RLS, OIDC+CF Access, audit + SIEM
(webhook/syslog), snapshot create/verify/restore, federation, export/import. The only
doc with `robothor user add` is an unpublished runbook.

**Docs:** 11 of 70 published; READING_GUIDE points into untracked `brain/` for 8 of 25
rows; SERVICES.md lists 6 units that don't exist; runbooks written for one machine
("GB10 ThinkStation"); README says 83 migrations (actual 113).

## Gap analysis

Legend: **LEAD** = Genus ahead; **PAR** = parity; **GAP** = behind; **CRIT** = blocks
enterprise install today.

| Axis | OpenClaw v2 | Grok Bot | Genus OS | Verdict |
|---|---|---|---|---|
| Zero→running | `curl \| bash`, wizard tests a real completion, opens into chat, ~5 min, headless gets an auth link | download app, sign in with Cursor, done | wrong pip name; six paths; SOPS required; owner never created; Telegram required; needs Claude Code | **CRIT** |
| Doctor / self-repair | `doctor --fix`, `security audit`, `triage`, `update --dry-run` | n/a (hosted) | none; `config validate` checks 3 vars + 4 ports | **CRIT** |
| Config model | one JSON5 file, strict schema, hot reload, 4 edit surfaces, schema lookup | natural language + toggles | 228 env vars (91 undocumented), 4 secret stores, 2 validators, 68 flags in 4 places | **CRIT** |
| Settings UI | Config tab, Plugins, Skills, MCP, Memory, accounts, per-user providers | Plugins marketplace, Auto Review rules, admin console | 21 flags | **CRIT** |
| Agent creation | CLI + config file + Agent Plugin bundles | 3 text fields | YAML by hand; UI read-only | **GAP** |
| Provider / key setup | wizard detects existing keys/CLIs, tests route, `/model`, multi-account rotation | none (no picker) | env var; pool for one provider; no UI | **GAP** |
| Channel setup | 31+, one command each, native buttons for ask_user, Slack Enterprise Grid | none inbound (own app) | Telegram deep; Slack inbound only; email via undocumented OAuth | **GAP** (breadth) / PAR (Telegram depth) |
| Chat-first surface | Control UI opens into chat | the app IS chat | chat panel exists but home is an LLM-generated canvas | **GAP** |
| Automations UI | full CRUD, history = ran/delivered/completed, auto-disable, restart replay | routine toggle on profile | manifests + read-only views; run truth exists (#468) | GAP (UI only) |
| Identity / SSO | none native (Tailscale or proxy) | SAML + SCIM inherited from Cursor | OIDC + CF Access, 6 roles, tenants, RLS | **LEAD** on substance, GAP on operability (CLI only, no local login, no UI) |
| Audit / compliance | `openclaw audit` (unverified), no SIEM | 90-day action recording, audit logs (Enterprise) | audit log + SIEM webhook/syslog, no UI | LEAD substance / GAP surface |
| Secrets | `${VAR}`, env/file/exec/store refs, Vault/1Password refs; store plaintext at rest | human takeover; Bot never holds a secret | AES vault exists but inert for creds; SOPS; env | GAP (one store, documented) |
| Sandbox | off by default, Docker/Podman/SSH | Firecracker per account, shared across Bots | rootless podman, per-agent, governance manifest | **LEAD** |
| Multi-tenant | explicitly unsupported | per account; Bots not a boundary | tenants + RLS + RBAC fail-closed | **LEAD** |
| Memory | LanceDB + MEMORY.md; retired rerank/cross-agent; memory UI + forget | per-Bot, no UI | supersession+decay+RLS, hybrid+rerank; browser component unlinked | **LEAD** substance / GAP surface |
| Skills | AgentSkills spec + ClawHub + Skill Workshop (agent proposes, human reviews) | write or teach-by-demo, promote task → skill → routine | same SKILL.md format, provenance, one CLI verb | GAP (workshop, registry) |
| Plugins | ClawHub, UI install w/ scan verdict, Agent Plugin bundles | account-wide plugin marketplace | governed host, 1 plugin, list-only CLI, no registry | **GAP** (inventory + surface; host is LEAD) |
| Quality measurement | none | none published | benchmarks, judge, WildClaw harness | **LEAD** |
| Reliability controls | auto-disable, restart replay | n/a | watchdog lattice, reaper, key pool, resume | **LEAD** |
| Upgrade | `update --dry-run`, rehearsed candidate state, backup/verify/restore | n/a | semantic-release + Helm; `robothor upgrade` diverges from `migrate` | GAP |
| Docs | comprehensive (users say intimidating) | short, task-shaped | 11/70 published, stale, one-machine runbooks | **CRIT** |
| Distribution | install.sh, npm, Docker, k8s, Nix, 12 cloud one-clicks | inside every Cursor/SuperGrok plan | GHCR images + Helm; PyPI blocked on token; 8 stars | GAP |

**The shape of the gap.** Genus OS has more enterprise substance than either competitor
(tenancy, RBAC, RLS, audit, sandbox, memory, measurement). Every one of those strengths is
reachable only by reading source. OpenClaw's 2.0 was literally "fix install and it became
2.0". Grok Bot's entire advantage is twelve onboarding decisions. Neither competitor's
security posture should be copied; their onboarding shape should be.

**What NOT to build:** a hosted-only product (out of scope, strategic); one-trust-
boundary shared sessions; 31 channels; in-process unsandboxed plugin marketplace;
computer-use on paired Macs/phones; native mobile apps (this cycle).

## Build plan

Three workstreams, each a PR train of independently mergeable conventional-commit PRs.
A must land first (B and C consume its settings model, doctor, and secrets accessor).
Every PR: TDD, ratchet/guard tests where the defect class is "invisible on the box".

### Workstream A — Zero to running, one path (install, wizard, doctor, config)

**Decisions baked in (Philip can override):** canonical verb is `genus` (add
`genus = "robothor.cli:main"` to `pyproject.toml:120`; `robothor`/`genusos` stay as
aliases; module and `ROBOTHOR_*` prefix do NOT change this cycle). `pydantic-settings`
becomes a core dep (pydantic v2 already is, `pyproject.toml:31`). Secrets default backend
= env file; SOPS and AES vault opt-in. First-run auth = single-use bootstrap sign-in link
minted by the CLI. Compose pilot runs GHCR images, not bind-mounted source.

**A1. One install path per substrate, all driven by `genus init`.** New package
`robothor/init/` (`plan.py` InitPlan, `steps.py` Step protocol with `check()`/`apply()`,
`substrate.py` + `substrates/{compose,systemd,helm,local}.py`, `provider_probe.py`,
`identity.py`, `render.py`). `robothor/setup.py` keeps its public names (`run_init`,
`create_workspace`, `check_prerequisites`, resumable `.robothor/init_state.yaml`) and
becomes the orchestrator. Two-phase: `--yes` runs every `check()` first and writes
nothing if a required check fails; `--json` emits plan + step statuses + `first_run_url`.
- compose (enterprise pilot default): `infra/docker-compose.apps.yml` gains a one-shot
  `migrate` service (`python -m robothor.cli migrate`), engine/bridge/orchestrator
  `depends_on: migrate: service_completed_successfully` (mirrors the Helm
  `wait-for-migrations` init container, `helm/genus-os/templates/_python_workload.tpl:108`),
  `image: ghcr.io/ironsail-llc/genus-os/python:${GENUS_IMAGE_TAG}`, drop the
  `../brain`/`../docs`/`../robothor`/`../crm` bind mounts (dev overlay
  `docker-compose.dev.yml` restores them), GPU reservation moves to
  `docker-compose.gpu.yml` added only when `nvidia-smi` succeeds. Remove the
  `./migrations:/docker-entrypoint-initdb.d` mount at `infra/docker-compose.yml:29`
  (it runs `001_init.sql` outside the ledger). Delete the stale Telegram note at
  `docker-compose.apps.yml:36-39`.
- systemd (checkout-only this cycle): `scripts/install-units.sh --profile core|all`
  (today installs 60+ templates incl. backup/thermal timers, `install-units.sh:68-84`).
  SOPS decoupled: `robothor-engine.service:17` `ExecStartPre` → new
  `scripts/load-secrets.sh` dispatching on `ROBOTHOR_SECRETS_BACKEND` (sops → existing
  `decrypt-secrets.sh`; file → validate 0600 and copy to `/run/robothor/secrets.env`;
  env → empty file). `decrypt-secrets.sh:47-51` drops `ROBOTHOR_TELEGRAM_*` from
  `REQUIRED_KEYS`.
- helm: renders `values-instance.yaml`, a secrets script or Vault path list, and a
  workspace-seed ConfigMap with `docs/agents/main.yaml` (chart `/ready` requires it,
  `helm/genus-os/README.md:150-170`); prints `helm upgrade --install` + port-forward.
- Prereqs become REQUIRED per substrate (`setup.py:364-441` marks them optional today).

**A2. The wizard (17 resumable steps):** ack → substrate → prereqs → detect (env keys,
Codex login via `cli/codex.py:43`, Ollama `/api/show` capabilities contain `tools`) →
provider (**real 1-token completion via `llm_call`, `llm_client.py:528`; failure blocks**)
→ identity (`write_owner_config()` to `constants.owner_config_path()`; stop writing
`ROBOTHOR_OWNER_*` to `.env`, `setup.py:761-763`; fix `templates/owner.yaml.example:4`)
→ workspace → database → migrate (db.migrate only) → models → agents (`agent install
--preset standard`) → operator (`accounts.bootstrap_owner_account()`, `auth/accounts.py:406`)
→ channels (optional; Telegram `getMe` probe) → secrets backend → services → verify
(`/ready` set + `doctor --json` zero required failures) → link (bootstrap URL that lands
on chat). `config.validate()` (`config.py:394-503`) loses Telegram and becomes a doctor
alias. Note the daemon is already Telegram-optional (`daemon.py:1077-1082`).

**A3. `genus doctor [--fix] [--json] [--only ID] [--category C]`.** New
`robothor/doctor/` (model, context, registry, `checks/{config,database,redis,models,
channels,services,manifests,identity,secrets,host}.py`). Checks are time-boxed 5s,
severity required/recommended/info, fixers idempotent, exit 0/1/2, JSON mirrors
`readiness_response` (`health_contract.py:48`) so the Helm Health view renders it.
Includes `db.rbac_service_role` (the fresh-install defect from memory),
`db.migrations` drift, `provider.completion`, `manifests.schema` strict,
`auth.owner_account`, `host.systemd_drift` (wraps `scripts/instance_doctor.sh`, parses
`FINDING` lines). Plugin checks: `robothor/plugins/loader.py:29` gains
`"genus.doctor": "checks"`, ids must be distribution-prefixed, cannot shadow built-ins.

**A4. Config unification.** New `robothor/settings/` (`model.py` `GenusSettings(BaseSettings)`
with nested groups paths/database/redis/ollama/providers/engine/channels/auth/flags/
services/secrets/substrate; every field has a description and `json_schema_extra`
{restart_required, secret, since, aliases, governed}; `extra="forbid"`; `aliases.py`
DEPRECATED_ALIASES with one-time DeprecationWarning for two minor releases;
`sources.py` YAML `settings:` block of `.robothor/config.yaml` + secrets source;
precedence defaults < config.yaml < env < runtime). `robothor/config.py` and
`engine/config.py:101` keep their public API and become adapters.
- **Guard test** `tests/test_settings_registry.py::test_every_env_read_is_declared`: AST
  walk of `robothor/`, `crm/bridge/`, `scripts/*.py` + grep of unit files and env
  examples; every `ROBOTHOR_*`/`GENUS_*` read must be declared. Backlog (141 names)
  declared in the same PR so it starts green. **Ratchet**
  `test_env_read_sites_outside_settings_do_not_grow`.
- `docs/configuration.md`, `infra/robothor.env.example`, `infra/systemd/robothor.env.example`
  become GENERATED (`scripts/gen_configuration_doc.py`) with an equality test.
- `robothor/cli/config_cmd.py`: `genus config get|set|validate|schema|explain`. `set`
  routes: governed flag → `flags.store.set_flag` (hot); secret → secrets accessor;
  else → `settings:` in config.yaml with "restart required" unless hot.
- Strict unknown keys in `.robothor/config.yaml` under `ROBOTHOR_CONFIG_STRICT_MODE`
  (enforce for new installs, observe for existing).
- Hot set: governed flags, `_all`/agent overrides, fields flagged hot (log level, cost
  caps, max_concurrent_agents) via existing SIGHUP. Restart: ports, endpoints, auth,
  secrets backend, substrate.
- Collapse duplicates: Telegram → `ROBOTHOR_TELEGRAM_*` only (delete fallbacks at
  `engine/config.py:103-106,383-385,419-421,472-474`); Ollama → `ROBOTHOR_OLLAMA_URL`
  canonical (remove `OLLAMA_URL` at `services/registry.py:33`); owner → owner.yaml only;
  all 57 flags declared, 21 governed marked.

**A5. Secrets.** New `robothor/secrets/` one accessor, chain env-file → vault → sops;
`auth/tokens.py:60` and `key_pool.keys_from_env` read through it.

**A6. Migrations.** `cli/upgrade.py`: delete the glob path (lines 31-91, 132-151), call
`db.migrate.status()`/`apply()`; `git pull` behind `--pull`. Delete
`examples/full-stack/init-db.sql` and its mount; add a `migrate` service there. Delete
`admin.py:40-56 _find_migration_sql`. `infra/setup.sh:246-271` and
`docs/deployment.md:98-108` call the migrator. `tests/test_migration_count_claims.py`
fails any doc number ≠ `manifest_count()` (README:140 and helm README:174 say 83;
actual 113) — recommend dropping the number from prose. RISK: compose instances that
ran `001_init.sql` via initdb have no ledger row → `--baseline-adopt 001_init`.

**A7. Manifest validation.** Schema ships inside the package
(`robothor/engine/schema/agent_manifest.yaml`, byte-equal test vs `docs/agents/schema.yaml`).
New `engine/manifest_schema.py` `validate(data, strict)` → structural rules from schema
+ semantic rules moved verbatim from `config_schema.py`. `config_schema.validate_manifest`
becomes a shim. Load-time: under `ROBOTHOR_MANIFEST_SCHEMA_MODE=enforce`,
`load_agent_config` (`engine/config.py:913-931`) raises `ManifestSchemaError`, caught by
`_load_manifest_classified` (224-253) → `ManifestFailure("SchemaError")` so the
scheduler's "broken is not deleted" path and `/ready` treat it as broken, not absent.
README:241 becomes true.

**A8. First-run authenticated URL.** Migration 114 `auth_bootstrap_links`;
`accounts.create_bootstrap_link/consume_bootstrap_link` (single use, 15 min, owner only);
bridge `POST /api/auth/bootstrap` guarded by `X-Bridge-Auth` + loopback rule
(`auth/runtime.py:53-70`); dashboard `Credentials({id:"bootstrap-link"})` provider only
when `GENUS_BOOTSTRAP_LINKS_ENABLED=true` (doctor recommends off once SSO configured);
`genus auth bootstrap-link`. Needs the security-review skill before merge.

**A9. Fresh-install acceptance gate.** `docs/quickstart.md` carries one
`<!-- install-gate: compose -->` marked block per substrate; `scripts/extract_doc_commands.py`
feeds `.github/workflows/install-gate.yml` (nightly + on PRs touching init/settings/
secrets/doctor/infra/quickstart/Dockerfiles): build images from the PR, start
`tests/acceptance/mock_llm/` (OpenAI chat/models + Ollama tags/show/embed/chat, ~150
lines FastAPI), run the extracted commands in a fresh dir, `curl -f` all four `/ready`,
`genus doctor --json` zero required failures, `genus run --agent main "say pong"` prints
pong. Second job runs the `local` block on CI's postgres/redis (`ci.yml:148-175`).

**A PR train (14):**
| # | Title | Size |
|---|---|---|
| A1 | feat(cli): add genus console script and invoked-name help | S |
| A2 | feat(settings): typed settings model that declares every env var | L |
| A3 | feat(secrets): one accessor with env-file, vault and sops backends | M |
| A4 | feat(config): genus config get/set/schema/explain; strict config.yaml; telegram optional | M |
| A5 | feat(migrate): upgrade uses the canonical migrator; retire init-db.sql and initdb mounts | M |
| A6 | feat(manifests): single schema validator enforced at load behind a mode ladder | L |
| A7 | feat(doctor): genus doctor with a pluggable check registry | L |
| A8 | feat(auth): single-use bootstrap sign-in link for first run | M |
| A9 | feat(init): two-phase wizard with provider probe, owner.yaml and operator bootstrap | L |
| A10 | feat(init): compose substrate with migrate service, ready gate and first-run URL | M |
| A11 | feat(init): systemd substrate via install-units --profile core | M |
| A12 | feat(init): helm substrate renders instance values and workspace seed | M |
| A13 | ci: fresh-install acceptance gate driven by the quickstart's own commands | L |
| A14 | docs: install, doctor and configuration pages point at one path | S |
Deps: A2→A4,A7,A9; A3→A7,A9,A11; A6→A7; A8→A9,A10; A9→A10,A11,A12; A10→A13.
A1, A5, A6 can start in parallel with A2.

**A risks:** mock provider must satisfy hidden Ollama boot deps (orchestrator generation
check `orchestrator.py:210`, 1024-dim embeddings); pydantic-settings import cost on the
CLI hot path (must stay <50 ms, measure in A2); two config readers coexist until the
ratchet reaches zero; manifest `enforce` on the prod fleet may flag keys schema.yaml
lacks (default observe, doctor counts would-be errors first); bind-mount removal
changes Philip's dev loop (dev overlay).

### Workstream B — The Helm becomes the product (UI)

**Ground truth that shapes it:** the app is one page with a `ViewId` union and all 10
views mounted (`app-shell.tsx:157-180`); desktop default view is `dashboard`, chat is a
400px side panel pinned to `agent:main:primary` (`lib/config.ts:11`); argon2id hashing
(`robothor/auth/passwords.py`) and `user_accounts.password_hash` (migration 071) exist
and are unused; six roles duplicated in `tokens.py:34`, `deps.py:20`, `cli/user.py:32`;
scheduler `reconcile()` only PRUNES (`scheduler.py:270-283,1300-1370`) so cron changes
need a restart while instruction/model/tool edits hot-apply; a manifest-writing path with
atomic temp+rename + validation + path guard already exists (`templates/installer.py:
85-92,280-305`) and a 13-check validator (`templates/manifest_checks.py`);
`installed_agents` install/update/remove have NO `require_operator` and no audit
(`installed_agents.py:143-217`); **`/api/vault/get` returns plaintext secrets to
owner/admin sessions through the BFF proxy** (`integration.py:159-172`) — close it;
`runs.py:20-24` omits `delivered_at`, `delivery_channel`, `verified_status`; tool
escalations are in-RAM and Telegram-bound (`permission_escalation.py:126-132,265`);
no `journalctl` anywhere; the control-plane spec explicitly deferred manifest editing.

**Cross-cutting decisions:** D1 keep the single-shell app, add URL sync
(`hooks/use-view-route.ts`, `?v=&s=&id=`) — no App Router rewrite. D2 every new write
handler: `require_operator` first line + `audited()` helper (`routers/_audit.py`) +
a program-level test enumerating every non-GET `/api/` route asserting the gate.
D3 secrets write-only end to end: responses carry `{configured, fingerprint, updated_at,
source}`; `/api/vault/get` requires `typ=service`; BFF proxy denylists vault reads.
D4 real work stays in the engine (`/api/admin/*` with `engine:control` scope,
`engine/auth.py:93-98`); bridge proxies. D5 manifests remain source of truth: UI writes
validated YAML into `docs/agents/` then calls engine reconcile; simple-form saves
deep-merge only `FORM_OWNED_PATHS` so hand-written keys survive. D6 no instance data in
routers. D7 client role-gating is UX only. D8 tokens only, no colour literals.

**B1. First-run wizard (`/setup`).** `genus init` calls `robothor/setup_token.py::
create_setup_token(workspace)` → `.robothor/setup_token.yaml` (sha256, +30 min, single
use) and prints `http://<host>:3004/setup?token=…` plus ssh port-forward text for
headless. Bridge `routers/setup.py`: `GET /status`, `POST /claim` (constant-time,
rate-limited → 5-min claim token), `GET /detect` (env keys, Ollama models, Telegram),
`POST /operator` (creates owner with argon2 password, **writes `~/.robothor/owner.yaml`**,
returns tokens), `POST /complete`. `proxy.ts` redirects `/` to `/setup` while
incomplete; `/setup` 404s forever after. Steps: ack → operator → provider + key +
Test connection + default model → optional channel (Telegram verify + pairing code;
Skip) → first agent from three template cards → done → `/?v=chat`.
**Supersedes A8** (bootstrap-link migration): one mechanism, not two.

**B2. Local login.** Migration 114: `mfa_enabled`, `mfa_secret_enc`,
`failed_login_count`, `locked_until`, `password_updated_at`, `password_reset_tokens`.
Bridge `GET /api/auth/methods`, `POST /api/auth/login` (5/min per email+IP, lockout 15
min after 10, one generic 401, audit), `/login/mfa`, `/password`, `/mfa/enroll|confirm|
disable`. App: `Credentials({id:"local"})` in `auth.ts` whose `authorize()` posts to the
bridge; `signInAllowed`/`bridgeJwtCallback` gain a `local` branch; sign-in page renders
form + SSO button when both exist. `validate_auth_configuration` (`runtime.py:117-121`)
accepts `GENUS_LOCAL_LOGIN=true` as an alternative to OIDC issuers. Promotion to SSO =
Users view "Link SSO identity" wrapping `accounts.create_binding_grant`
(`accounts.py:197-244`). Owner MFA mandatory when local login is the only method.

**B3. Settings (view `settings`, sub-nav).**
- Providers & keys: `GET /api/providers` (per-provider configured/source/slots with
  fingerprints from `KeyStatus`), `PUT/DELETE /keys/{position}` (vault write, engine
  `POST /api/admin/secrets/reload`), `POST /{id}/test` → engine `llm_call` with the
  candidate injected for that call only (`{ok, model, latency_ms, error_class}`),
  `GET /api/models` from `_MODEL_REGISTRY` + `_plugin_model_limits()`, `PATCH /defaults`
  writes `_defaults.yaml` `model:` block. Engine: `key_pool._PROVIDER_KEY_VARS` gains
  anthropic/openai/gemini/deepseek; keys resolve vault-first then env with an env
  refresh from `vault.export_env()` on reload so litellm sees UI-written keys.
- Channels: `GET /api/channels` (configured/verified/running/restart_required/allowlist),
  `PUT` token writes, `POST /telegram/verify` (`getMe`), `POST /telegram/pair/start` →
  code the operator sends as `/start <code>` (matches the closed-onboarding path
  `telegram.py:852-908`), `POST /slack/verify` (`auth.test`). Email = honest read-only
  card this cycle.
- Users & roles: `GET /api/auth/roles` (kills the triplicate), `GET/POST /api/users`
  (invite with one-time link), `PATCH` (refuses demoting last owner, self-deactivate),
  `/deactivate` (+ `revoke_all_sessions`), `/binding-grant`, `/password-reset`; tenants
  reuse existing `/api/tenants`.
- Secrets: `GET /api/secrets` (key, category, fingerprint, `referenced_by` agents),
  `PUT/DELETE`, `POST /test` when a tester exists.
- Config: thin wrapper over A's settings model — `GET /api/settings/schema` (JSON Schema
  with `x-genus: {group, scope, reload: hot|restart:<svc>, secret, source, env_var}`),
  `GET /api/settings` (values + provenance, secrets as `{configured}`), `PATCH`
  (`{applied, pending_restart, errors}`, 422 applies nothing). `schema-form.tsx` renders
  by type; env-overridden fields read-only with a badge; pending-restart banner.
- Flags: all flags visible, grouped Guardrails (21, hot, verdicts) / Engine ladders /
  Tuning knobs (which move to the Config form). **Uses A's settings model as the single
  registry** (fields carry `governed`); `GOVERNED_FLAGS` derives from it. Non-writable
  rows render without a select — the UI must never imply a write that does nothing.
- Plugins: engine `routers/admin_plugins.py` (`PluginSet.loaded/failures` + manifest),
  enable/disable via C5's lockfile, `POST /reload` = the SIGHUP body; UI list + health;
  "Install from registry" wired when C6 lands.

**B4. Agents create/edit/delete.** Simple form = Name (id derived kebab-case, immutable),
Job/role (`description` + `department`), Instructions (rendered into `brain/<ID>.md` via
`templates/agent-instructions.md` with the INSTRUCTION_CONTRACT sections preserved);
hidden defaults (version, model from `_defaults.yaml`, schedule empty, delivery none,
Assistant tool preset). Advanced drawer: Model (+fallbacks from `/api/models`), Schedule
(cron builder + preview, timezone, max_iterations, session_target, catch_up), Delivery
(mode/channel/to from `/api/channels`), Tools (checkbox groups from engine `GET
/api/admin/tools`), Sandbox, Guardrails/exec_allowlist/write_path_allowlist, YAML tab
(mono textarea, jsdom-testable). Bridge `routers/agent_manifests.py` (all
`require_operator`): `GET ""` (incl. `broken:` from `load_manifest_dir`), `GET /{id}`
(manifest + yaml + instructions + validation), `POST /validate` (`config_schema` +
`manifest_checks.validate_agent` + `manifest_to_agent_config` + `CronTrigger.from_crontab`
+ tool names vs registry), `POST ""` (render → validate → atomic write → audit → engine
`POST /api/admin/scheduler/reconcile`; 409 on existing id), `PATCH /{id}` (bumps
version, appends changelog, `.history/` ring of 5), `DELETE` (`confirm == id`, moves to
`docs/agents/retired/`), `enable|disable`, `run`. Path safety reuses `contained_path`/
`validate_identifier` (`installed_agents.py:16-21`). **Engine: `reconcile()` grows to
add/replace/prune** (compare cron/timezone/heartbeat/worker with the registered
trigger, `add_job(replace_existing=True)`), keeping the dirty-scan interlock and
reporting `blocked.reason`.

**B5. Chat-first home + Inbox.** Default view `chat` on desktop and mobile
(`app-shell.tsx:75-77`); chat becomes a main view with the canvas as an optional rail;
LLM dashboard stays as a tab. Agent switcher from `/api/agents/manifests` (chattable =
`session_target: persistent` or core); `/api/chat/*` take `agent`, build
`agent:${agent}:primary`, engine rewrites per user under enforce. Inline approvals:
engine `on_status` gains `approval_required`; `POST /chat/approvals/{id}` resolves
`PermissionEscalationManager` (made channel-agnostic; C10 does the Channel side);
fallback = route tool escalations through durable `workflow_approvals`. Inbox view
(badge = count) composes `list_pending_approvals`, in-RAM escalations, and tasks in
REVIEW/`question_for_operator`; `POST /api/approvals/{kind}/{id}` fans out.

**B6. Automations.** `GET /api/automations` composes manifests + `agent_schedules` +
latest run + routines into one card model `{kind, cron, enabled, next_run_at,
breaker_tripped, last_run: {ran, delivered, completed}, last_failure_reason}`; `PATCH`
cron/timezone/enabled via the manifest pipeline; pause/resume; `GET /runs` with
`runs.py` extended by `delivered_at, delivery_channel, verified_status`; breaker reset.
Run history shows three columns ran / delivered / completed (OpenClaw's separation,
from our own run-truth columns).

**B7. Surface what exists.** Memory view (link `MemorySearch`; `POST
/api/memory/facts/{id}/forget/preview` read-only + `/forget {reason}` = `is_active=false,
valid_to=now()`, audited). Audit view over existing `/api/audit/*` + new `GET
/api/controls/audit` (`feature_flag_audit`), CSV export, auditor role. Logs view:
`routers/logs.py` → `journalctl -u <unit> -n N -o json` (list args, allowlisted units,
`sanitize_log` per line) returning `available:false` on container deploys.

**B8. Information architecture.** Sidebar: Chat | Workspace (Inbox, Agents, Automations,
Dashboard, Marketplace) | Observe (Runs, Fleet, Memory, Audit, Logs, Health) | Settings
(Providers, Channels, Users & roles, Secrets, Config, Flags, Plugins, Appearance).
Command palette derives from the same `navGroups` and gains verbs. Mobile: tab bar
Chat / Inbox / Agents / More; wizard and sign-in fully usable at 375px; YAML drawer =
full-screen sheet.

**B PR train (16 + mockups):**
| # | Title | Size |
|---|---|---|
| B0 | Mockup artifact (chat home, agent create + drawer, settings + providers, wizard, inbox, mobile tabs) — Philip decides from this | — |
| B1 | feat(helm): URL-synced views, sidebar groups, settings container, role-gated nav | M |
| B2 | fix(bridge): audited-mutation helper, route-enumeration gate test, close plaintext vault read, gate installed-agents | M |
| B3 | feat(auth): local email+password login (argon2, rate-limited, MFA) | L |
| B4 | feat(engine): providers, models, test-connection, vault-backed key pool | L |
| B5 | feat(helm): first-run wizard and setup token | L |
| B6 | feat(helm): settings providers and secrets UI | M |
| B7 | feat(engine): agent manifest API and scheduler add/replace reconcile | L |
| B8 | feat(helm): agents create, edit and delete UI | L |
| B9 | feat(helm): chat-first home, agent switcher, inline approvals | L |
| B10 | feat(helm): inbox of consolidated approvals | M |
| B11 | feat(helm): channels UI with telegram verify and pairing; users and roles UI | L |
| B12 | feat(helm): automations view with run truth columns | M |
| B13 | feat(flags): every flag visible, writable subset, grouped UI | M |
| B14 | feat(helm): memory browser with forget preview, audit view, logs view | M |
| B15 | feat(plugins): engine admin plugins, disable list, reload; plugins UI | M |
| B16 | feat(helm): schema-driven config form | M |
Deps: B3→B5; B4→B5,B6,B8; B7→B8,B12; A2→B13,B16; A3→B4 (vault key naming);
A7→B5 (doctor strip); C5→B15; C10→B9 (Channel.ask); B2 first (it closes a live leak).

**B risks:** R1 scheduler hot-add must keep the dirty-scan interlock; R2 litellm reads
`os.environ`, so a rotated key must be picked up without restart (test it); R3 does the
scheduler honour `agent_schedules.enabled`? (unverified; design writes
`schedule.enabled` into the manifest); R4 setup endpoints are public until claimed →
security-review skill before merge; R5 Logs assume `agent_id=` in log lines; R6
escalations touch the runner path (durable-approvals fallback); R9 relaxing the
production auth gate is a posture change → owner MFA mandatory; R10 the 10/min limiter
on `/api/actions/execute` must not sit in front of settings pages.

### Workstream C — Reach and ecosystem (channels, plugins, docs, distribution)

**Corrections found while designing:** Apollo and Princess Freya are ALREADY out of core
(only empty comment headers at `engine/tools/schemas.py:1266,2329`, a comment at
`detectors.py:620`, and hardcoded `impetus_*` names in `tools/handlers/benchmark.py:182-192`).
Impetus One is already an instance-land MCP adapter YAML (`~/.config/robothor/adapters/`)
pinned to `python -m robothor.connectors.rest_mcp_bridge` with `tools_allowed`; the only
live-workflow constraint is: never move that module path without a shim. Slack ALREADY
registers a sender (`engine/slack.py:110-124`) that `delivery.py:510-513` never calls
(hardcodes `_deliver_telegram`); `AgentConfig.delivery_channel` (`models.py:148`) is
never read. `config_schema.py:78` accepts delivery modes `{none,announce,summary,full}`
while `DeliveryMode` (`models.py:86`) is `ANNOUNCE/NONE/LOG` — reconcile. There is no
`ask_user` tool; the only human-in-loop primitive is `permission_escalation.py`
(Telegram inline keyboard). Distribution is further along than assumed: images ship
`v1.65.0`/`v1.65`/`v1`/`sha-*` with no `latest`; PyPI Trusted Publishing job exists
gated on `vars.PYPI_PUBLISH_ENABLED` (`release-and-build.yml:304-330`); Helm is
lint-only, never published.

**C1. Channel abstraction.** New `robothor/engine/channels/` (`base.py` `Channel`
protocol: `start/stop/send→SendReceipt(acknowledged, expected, platform_ids)/ask(
question, options)→str|None/resolve_identity/health/inbound_router`; `ingress.py`
shared inbound pipeline = the code duplicated between `slack.py:140-201` and
`telegram.py:387`; `access.py` pairing|allowlist|open policy; `telegram.py` WRAPS
`TelegramBot` (telegram*.py untouched; keeps plan mode); `slack.py`, `webchat.py`,
`email.py`). Delivery dispatch: `ANNOUNCE → get_channel(config.delivery_channel or
"telegram")`, `failed:no_channel` if absent; `register_platform_sender` stays as a shim.
Identity: new `user_channel_identities(tenant_id, user_id, channel, native_id, …)`
table + `_resolve_generic` in `identity/resolvers.py:44-60`; Telegram keeps its column.
Pairing: unknown sender gets a 6-char code; operator approves via `genus channel access
approve slack ABC123` or the Helm; **a channel message may never approve a pairing**.
`genus.channels` plugin group added to `loader.py:31-64`; channel plugins inert until
`genus channel add <name>` (same precedent as sandboxes).
- **Slack (core, first enterprise channel):** ship `docs/channels/slack-app-manifest.json`
  (Socket Mode → no public URL); wizard asks bot token, app token, default target,
  access mode; verify = `auth.test` + `conversations.list` + `chat_postMessage` asserting
  `ts` + `apps.connections.open`. Threads via `thread_ts`, Block Kit buttons for `ask`,
  DM targets. `slack-bolt` gets a `channels` extra in pyproject (declared nowhere today).
- **Microsoft Teams (plugin `genus-teams`):** Azure Bot resource + Teams app manifest;
  needs a public HTTPS messaging endpoint (`/api/channels/teams/messages`, JWT validated
  against Bot Framework OpenID); Adaptive Card `ask`; identity via `aadObjectId`,
  automatic when SSO is Entra.
- **Web chat as member channel:** app stops sending the literal `SESSION_KEY`
  (`app/src/app/api/chat/send/route.ts:20`); engine derives per-user keys under
  `ROBOTHOR_PER_USER_SESSIONS=enforce` (owner keeps shared key, `chat.py:140-142`);
  `WebchatChannel.send` writes chat_store + notifications so scheduled agents can
  deliver to a member's inbox; `ask` = SSE event rendered as buttons.
- **Email:** outbound via gws + SMTP fallback + DNC check; inbound IMAP watcher phase 2.
- **`ask_user` tool** (`tools/handlers/ask_user.py`) routes to the run's channel;
  `PermissionEscalationManager` takes a `Channel` instead of a bot.

**C2. Plugin surface.** Install = `pip install` into the engine's interpreter (pipx
inject under pipx); containers use `ROBOTHOR_PLUGIN_DIR=/workspace/.plugins` +
`pip install --target` + PYTHONPATH, Helm value `engine.plugins: [{name,version,sha256}]`
renders a `genus plugin sync` init container; image-bake documented for air-gap.
Lockfile `~/.config/robothor/plugins.lock` (name, spec, version, dist_sha256,
manifest_sha256, verdict, enabled, kinds); loader consults it BEFORE `ep.load()`
(disabled → refused; manifest hash drift → refused, mirrors `verify_adapter_integrity`
`adapters.py:143-172`); SIGHUP picks up changes. Verbs: `genus plugin install|remove|
enable|disable|info|doctor|sync`. Signed static registry index (`index.json` +
Ed25519 `index.json.sig`, reuse federation key code; publisher keys pinned in
`plugins/registry_keys.py`; `ROBOTHOR_PLUGIN_INDEXES` multi-index; offline install from
wheel + `--sha256`). Install pipeline: verify signature → sha256 → bounded extract
(reuse `hub_client._extract_archive`) → `genus-plugin.yaml` present → static checks
(undeclared entry-point groups, reserved names → blocked; hooks/guardrails/sandboxes/
channels → at least `review`; subprocess/eval/base64 heuristics) → injection screen
on bundled prompt text → verdict safe/review/blocked; `review` needs `--accept-review`.
`/api/plugins` engine route + doctor section; plugin failure never blocks `/ready`.
Hosting: GitHub Pages from a new `Ironsail-llc/genus-plugins` repo (canonical),
programmaticresources.com mirrors the same index (its `HubClient` already does
sha256-pinned downloads). **RFC #267: close and supersede** with
`docs/rfcs/0002-plugin-distribution-and-channels.md` documenting what shipped.
First five: (1) genus-hostinfo as first index entry, (2) `genus-rest-connector`
packaging `rest_mcp_bridge` with a re-export shim + `genus adapter migrate impetus-one`
that rewrites `command` and writes `command_sha256`, (3) Apollo/PF/Impetus vestige
cleanup + grep gate in `tests/test_core_instance_boundary.py`, (4) `genus-teams`,
(5) `genus-discord` as second `genus.channels` implementer (or an OpenClaw importer).

**C3. Agent bundles / share.** Reuse `robothor agent import` (`cli/agent.py:447`),
`installer.py:130-200`, hub sha256 download. Add `genus agent export <id>` (manifest +
instructions + referenced skills + adapter YAMLs with values collapsed to `${VAR}`,
`requires: {plugins, adapters, secrets}`; a literal token in an export is a test
failure), `genus agent install <path|url|slug>` with preview-as-plan then `--yes`,
bridge `POST /api/installed-agents/{id}/export` for a Helm Share button. Same signed
index envelope (`kind: agent-bundle`) so a company can host one private catalog.

**C4. Skill Workshop-lite (later-phase, engine side only).** `ROBOTHOR_SKILL_PROPOSALS=
off|observe|enforce`; in enforce, background-review/curator skill writes go to
`agents/skills/.proposals/<skill>/<id>/` with `proposal.json` (base_content_hash, diff,
scan_verdict); endpoints list/diff/apply (refuses on base-hash drift)/reject. Helm diff
view is B's.

**C5. Docs as product.** New `docs/enterprise/00-overview … 07-operate.md` (pilot
compose → SSO → channels → agents → backup/restore → upgrade → operate). Publish the
operator set by extending `mkdocs.yml:57-70,77-90` (PLUGINS, FEDERATION, CONNECTORS,
AGENT_BUILDER, OBSERVABILITY, TESTING, generic runbooks after scrubbing; helm README via
snippet include). Move one-machine runbooks (THERMAL, BACKUP_VOLUME_GUARD, box halves of
INSTANCE_DOCTOR/PAGING/SLOS, BENCHMARK_*) to `docs/instance/`. Generated
`docs/reference/cli.md` (walks `_build_parser()`) and `docs/reference/config.md` (from
A's model). **`scripts/check_doc_commands.py`**: every fenced `robothor|genus` command in
docs must parse via `_build_parser().parse_known_args` (fails on `pip install robothor`);
`check_doc_links.py` resolves relative links and backticked paths (`brain/` only when
labelled instance). Delete `docs/DEPLOYMENT.md`; fix READING_GUIDE (16 `brain/` refs),
SERVICES.md phantom units.

**C6. Distribution.** PyPI blockers: reserve `genusos`, create the `pypi` GH environment
+ trusted publisher, TestPyPI rehearsal input, `channels` extra + `genus` script, short
`PYPI_README.md` (README is instance-flavoured). `scripts/install.sh` published at the
docs site: docker path (fetch pinned compose, `.env`, up, `genus init --yes`) or pipx
path; version stamped by semantic-release prepareCmd; shellcheck + dry-run test.
`infra/docker-compose.release.yml` overlay pins GHCR images. `publish-helm` job pushes
the chart to `oci://ghcr.io/ironsail-llc/charts`. Release notes: `changelog.d/<pr>.<type>.md`
fragments assembled by audience (Operators / Admins / Agent authors) with a CI presence
check for feat/fix PRs.

**C PR train (20):**
| # | Title | Size |
|---|---|---|
| C1 | docs: make the published docs true (delete DEPLOYMENT.md, fix guide/services/quickstart, doc-command + link checkers in CI) | M |
| C2 | build(dist): PyPI readiness (genus script, channels extra, PYPI_README, TestPyPI input) | S |
| C3 | feat(channels): protocol, registry, Telegram wrapper, delivery by channel | M |
| C4 | feat(channels): Slack as delivery target with verify and `genus channel` CLI | M |
| C5 | feat(plugins): lockfile, enable/disable, info, doctor, remove, genus.channels group | M |
| C6 | feat(plugins): signed registry index, scan verdict, `genus plugin install`; RFC 0002 supersedes #267 | L |
| C7 | refactor(core): Apollo/PF/Impetus vestiges out; genus-rest-connector package; `genus adapter migrate` | M |
| C8 | feat(channels): access policy, pairing, generic identity table, bridge routes | M |
| C9 | feat(webchat): per-user sessions on, WebchatChannel, ask over SSE | M |
| C10 | feat(tools): ask_user tool and escalation over Channel.ask | M |
| C11 | feat(channels): email outbound with DNC | S |
| C12 | feat(plugins): genus-teams channel plugin | L |
| C13 | feat(agents): bundle export, URL install with preview-as-plan, Share route | M |
| C14 | build(dist): install.sh and compose release overlay | M |
| C15 | ci(helm): OCI chart publish and engine.plugins init container | M |
| C16 | ci(release): changelog fragments assembled by audience | S |
| C17 | docs: generated CLI and config reference | S |
| C18 | docs: enterprise guide set and published operator docs | L |
| C19 | feat(skills): proposal store and review endpoints (later-phase) | M |
| C20 | feat(plugins): genus-discord (optional) | M |
Deps: C4 verify writes to A's secret store (env until then); C8 needs A settings; C14
needs `genus init --yes --json` (A9); C17 config half needs A2; C12 needs C5+C8+C10.
B consumes `/api/plugins` (C5), `/api/channels/*` (C8), `ask` SSE (C9), export route (C13).

**C risks:** runtime pip on bare metal / `--target` on a PVC acceptable? (else image-bake
only); check the box's manifests for `delivery.mode: summary|full` before C3 tightens
validation; enterprises may refuse a public endpoint for Teams; injection screen is
LLM-backed so air-gapped verdicts are `static-only`; `genusos` availability on PyPI and
index-key custody; flipping per-user sessions must not change the owner's experience
(pin with a test).

## Reconciliation across workstreams (decided)

1. **First-run auth: B's setup token + local login wins; A8 is dropped.** `genus init`
   calls `create_setup_token()`; the wizard's operator step creates the owner with a
   password. No `auth_bootstrap_links` migration. Migration 114 is B2's local-auth
   columns.
2. **One flag registry.** A's settings model declares every flag with `governed` /
   `writable` / `restart_required` metadata; `GOVERNED_FLAGS` derives from it; B13's UI
   reads the schema. No separate `flags/registry.py`.
3. **One doctor.** A7's `robothor/doctor/` is the engine; the bridge exposes
   `GET /api/doctor` for the wizard strip, Health view, and config banner. C5's plugin
   doctor is a `genus.doctor` check set, not a second command.
4. **Vault key naming is A's** (`providers/<id>/api_key[_N]`, `channels/<name>/<field>`);
   B4 implements the engine refresh-from-vault; C4's Slack verify writes through it.
5. **Manifest validation is A6's module**; B7's `POST /validate` and C3's delivery
   channel check call it. C3 must first check the box's manifests for
   `delivery.mode: summary|full` before tightening.
6. **`genus` is the documented verb** (A1); C1's doc-command checker accepts all three
   names; C17 generates the CLI reference from the same parser.
7. **Impetus stays untouched** until `genus adapter migrate` (C7); A11's systemd substrate
   is checkout-only this cycle and never rewrites adapter YAML.

## Sequence (what ships first and why)

**Phase 0 — Truth (week 1, all S/M, parallel):** C1 docs made true + doc-command/link
checkers; A1 `genus` verb; A5 one migrator; B2 close the vault plaintext read + gate
installed-agents; C2 PyPI readiness; C7 core vestiges out. Closes the "published docs
lie" class and one live secret leak before any feature work.

**Phase 1 — Zero to running (weeks 2-4):** A2 settings model → A3 secrets → A4 config
CLI (Telegram optional) → A6 manifest validator → A7 doctor → B3 local login → B4
providers/models/test → B5 wizard + setup token → A9 init wizard → A10 compose
substrate → A13 fresh-install CI gate → A14 docs. **Exit criterion: a clean container
follows `docs/quickstart.md` and reaches chat in the Helm with a real completion, with
`genus doctor` green, in CI every night.**

**Phase 2 — Configure and operate from the UI (weeks 4-7):** B0 mockups (Philip gate) →
B1 shell → B6 providers/secrets UI → B7 manifest API + reconcile → B8 agents UI → B9
chat-first + B10 inbox → B11 channels/users → B12 automations → B16 config form → B13
flags → B14 memory/audit/logs. C3 channel protocol → C4 Slack delivery → C10 ask_user
run alongside. **Exit criterion: an admin creates an agent, sets its schedule, connects
Slack, invites a user, and reads the run truth without touching a terminal.**

**Phase 3 — Reach (weeks 7-10):** C5 plugin lockfile → C6 registry + install → B15
plugins UI → C8 pairing/identity → C9 webchat per-user → C11 email → C12 Teams plugin →
C13 bundles/share → A11 systemd + A12 helm substrates → C14 install.sh → C15 Helm OCI →
C16 changelog fragments → C17/C18 docs. **Exit criterion: Genus OS installs on a
company box or cluster from a documented one-liner, publishes to PyPI + OCI, and a
second channel is a plugin.**

Later-phase (explicitly not this cycle): C19 skill workshop, C20 Discord, native mobile
apps, computer-use, hosted offering.

Roughly 50 PRs. At the repo's demonstrated cadence (7-PR trains in a day, 26 overnight)
this is a multi-week program, not a quarter; the phase exits are the falsifiable
checkpoints.

## Verification

- **Every PR:** TDD (failing test first), `pytest -m "not slow and not llm and not e2e"`
  pre-commit, `cd app && pnpm test` for app changes, mypy strict + ruff clean, conventional
  commit title, security-review skill on B2/B3/B5/C6/C8.
- **Guard tests that make the defect class impossible** (each named in its PR):
  `test_every_env_read_is_declared` + ratchet (A2); `test_migration_count_claims` (A5);
  `test_manifest_schema_single_source` (A6); route-enumeration operator gate (B2);
  no-secret-in-response walker (B3-B6); export-contains-no-token (C13); doc-command
  parser + link checker in CI (C1); `test_core_instance_boundary` grep gate (C7);
  configuration doc equals generator output (A2).
- **Phase 1 end-to-end:** `install-gate.yml` (A13) on a clean container from the
  quickstart's own commands: four `/ready` green, `genus doctor --json` zero required
  failures, one real completion through the mock provider; nightly + on relevant PRs.
- **Phase 2 end-to-end:** a scripted browser run (Playwright, added in B8) from
  `/setup?token=` through agent create → schedule → Slack verify → invite user →
  automations shows ran/delivered/completed for the first run. Runs on staging via the
  `deploy-staging` label.
- **Live probes on the box (probe, don't trust silence):** rotate a provider key in the
  UI and confirm the next real run uses it (B4/R2); create an agent in the UI and see it
  fire on its cron without an engine restart (B7/R1); pair a Slack DM and receive a
  scheduled delivery in-thread (C4); `genus doctor --fix` on a DB with the `service`
  role deleted re-seeds it (A7).
- **Competitive re-measure:** re-run the cached competitive sweep after Phase 2 with
  installability and configurability as explicit axes with dated evidence.

## Decisions taken as defaults (override in review)

| # | Decision | Default | Why |
|---|---|---|---|
| 1 | Enterprise pilot substrate | Docker Compose single node (Helm for scale, systemd checkout for the box) | one `docker compose up`, GHCR images already exist, matches OpenClaw's Docker path |
| 2 | First enterprise channel | Slack (core); Teams as the first channel plugin | Socket Mode needs no public URL; sender already exists; Teams needs Azure + ingress |
| 3 | Day-one login | Local email+password with mandatory owner MFA, alongside OIDC/CF Access | five-minute install is impossible if an IdP is required first |
| 4 | Canonical CLI verb | `genus` (aliases kept) | package is `genusos`; docs must stop saying `pip install robothor` |
| 5 | Settings engine | pydantic-settings as core dep | pydantic v2 already core; schema export drives docs and the UI form |
| 6 | Secrets default | env file; vault + SOPS opt-in | SOPS hard-requirement is the #1 systemd blocker |
| 7 | Plugin registry | GitHub Pages canonical, programmaticresources.com mirror, signed index | offline mirroring, no commercial coupling in core |
| 8 | RFC #267 | close, supersede with RFC 0002 documenting what shipped | two designs in flight is worse than one honest one |
| 9 | Manifest/config strict modes on the prod box | observe; doctor counts would-be errors before flipping | the fleet may carry keys schema.yaml lacks |
| 10 | Agent delete | retire to `docs/agents/retired/` | reversible; scheduler prune semantics already exist |
| 11 | UI restarts services | not this cycle; banner names the unit | keep the write surface small until doctor is trusted |
| 12 | Hosted Genus OS | out of scope | strategic; Grok's hosted advantage is noted, not chased |

## First steps after approval

1. Commit this design as `docs/superpowers/specs/2026-09-10-productization-gap-analysis-design.md`
   (brainstorming skill requires the spec in-repo) and open a tracking issue with the
   three phase exit criteria.
2. Publish the gap analysis + plan as an artifact for Philip (his decision surface), and
   build the B0 mockup artifact before any B code.
3. Start Phase 0 as one PR train via subagent-driven development (writing-plans skill per
   PR, worktrees, two-stage review), with the memory-recorded merge-loop gotchas applied.
