# Genus OS for an enterprise

Eight pages, in the order a company actually meets them: a pilot on one
machine, then identity, then the channel your people already live in, then
agents, then the credentials those agents use, then backups and upgrades, then
operating it. Each page is a task, not a tour.

If you only read one line: install a pilot, run `genus doctor`, and do not
proceed past a red `required` check.

## What Genus OS is

A deterministic agent platform you deploy on your own infrastructure. Agents
are YAML manifests running on a small engine; memory, CRM, audit and run
evidence are tables in **your** PostgreSQL; skills, tools, channels and
connectors are plugins. There is no hosted tier to fall back to and no
telemetry leaving the deployment that you did not configure.

### Platform and instance

The one distinction that decides where everything lives.

| | Platform | Instance |
|---|---|---|
| What it is | The engine, the tools, the migrations, the Helm dashboard | Your identity, your agents, your memory, your credentials |
| Where | The `genusos` package and the container images | Your workspace directory and your database |
| Who owns it | Ironsail, under MIT | You |
| Survives an upgrade | Replaced by it | Untouched by it |

Agent manifests, instruction files and the operator's own record are instance
data. A platform upgrade never rewrites them. The full boundary, including
what a contributor may and may not put in tracked code, is
[Platform vs Instance](../PLATFORM_INSTANCE.md).

### One trust boundary per instance

An instance is a single administrative domain: one set of operator accounts,
one credential store, one audit log, one fleet. Multi-tenancy inside an
instance exists (`genus tenant`) and is enforced in the database with row-level
security, but it separates **data**, not administrators — an operator of the
instance can reach every tenant in it.

So the unit of separation between two organisations, or between a regulated
subsidiary and the rest of a group, is **a second instance**. Two instances can
then be federated: signed invites, scoped exports, no transitive trust.

### What runs where

| Component | What it does | Port (defaults) |
|---|---|---|
| Engine | Runs agents: the model loop, the tool registry, the scheduler, the channels | 18800 |
| Orchestrator | Retrieval and memory API | 9099 |
| Bridge | The API the dashboard talks to: users, agents, channels, audit, doctor | 9100 |
| The Helm | The Next.js control plane — chat, agents, Observe, Settings | 3004 |
| PostgreSQL | Everything durable: memory, CRM, runs, audit, flags | 5432 |
| Redis | Sessions and the event bus | 6379 |
| Ollama | *Optional.* Local embedding, reranking and generation | 11434 |

Nothing in that list is reachable from the internet by default. Publishing the
Helm is a decision you make on [Identity](02-identity.md).

## Three substrates

One install path — `genus init` — and three places to point it.

| Substrate | Use it for | What you operate |
|---|---|---|
| **Compose** | The pilot, and small production. Released images from GHCR, one `docker compose` stack | One machine, one `.env`, one `docker compose up -d` |
| **Helm** | Scale, HA, an existing cluster. The chart is published to an OCI registry at every release | A namespace, a values file, your own secret management |
| **Local (systemd)** | A checkout on a box you control, or development on the platform itself | systemd units, your own PostgreSQL and Redis |

Start on compose. A pilot that works is worth more than a cluster that is
nearly configured, and a compose instance's data moves to Helm with
`genus snapshot` — see [Backup and upgrade](06-backup-upgrade.md).

## Decide these before day one

Four decisions. None of them is irreversible, and three of them are much
cheaper made now.

1. **Identity provider.** Local email and password works on day one and owner
   MFA is mandatory while it is on. OIDC or Cloudflare Access can be added
   later and run alongside it. Decide who the *owner* account belongs to —
   it is the one role that cannot be demoted by anybody else.
2. **First channel.** Slack is built in; Microsoft Teams is a plugin; web chat
   in the Helm needs nothing. Whichever you pick, decide the access mode —
   `pairing`, `allowlist` or `open` — before anybody outside the pilot group
   can find the bot.
3. **Secrets backend.** Where the instance's credentials live, and who may
   hand a new one to an agent. See [Secrets](05-secrets.md).
4. **Which plugins.** Every plugin is code you are choosing to run. The
   registry is signed and the installer scans before it installs, but the
   decision is still yours, and the lockfile is where you record it.

Worth deciding in the first week, not the first day: your snapshot schedule and
where snapshots go, and whether you want the benchmark rotation as a regression
gate.

## The next seven pages

| Page | What you will have done |
|---|---|
| [1. Install](01-install.md) | A running instance, `genus doctor` green, an owner account |
| [2. Identity](02-identity.md) | Your people signing in, with the right roles |
| [3. Channels](03-channels.md) | Agents reachable where your people already are |
| [4. Agents](04-agents.md) | A fleet you configured, on schedules you chose |
| [5. Secrets](05-secrets.md) | Credentials in one managed store, not scattered in the environment |
| [6. Backup and upgrade](06-backup-upgrade.md) | A verified snapshot, and a rehearsed upgrade |
| [7. Operate](07-operate.md) | The pages you open when something is wrong, and the ladder for turning a control on |

Reference material the guide points at, rather than repeats:
[Quick Start](../quickstart.md), [Deployment](../deployment.md),
[Configuration](../configuration.md), the generated
[CLI reference](../reference/cli.md) and
[settings reference](../reference/configuration.md).
