# Quick Start

From zero to a signed-in Genus OS instance. There is **one** install path —
`genus init` — with two substrates to choose between, and a nightly CI job that
replays the commands on this page on a fresh machine.

## What changed in 1.69

Six overlapping install recipes were replaced by the wizard, the doctor and the
gate described below. Each capability, with the command that exercises it:

| Capability | What it means | Exercise it with |
|------------|---------------|------------------|
| **One wizard** | `genus init` checks everything before it writes anything, records each finished step, and resumes rather than restarting | `genus init --dry-run` |
| **Two substrates** | `local` runs the platform on this machine; `compose` runs the whole stack from released GHCR images | `genus init --substrate compose` |
| **A doctor** | One command answers "is this instance actually working?", with severities, an exit code and two repairs | `genus doctor`, `genus doctor --fix` |
| **Secrets backends** | The unit environment, a 0600 file, or SOPS + age — chosen at install time. SOPS is opt-in, no longer a prerequisite for starting | `genus init --secrets-backend file` |
| **A browser setup wizard** | A single-use `/setup` link creates the operator account, takes one provider key and installs the first agents, with no terminal | `genus auth setup-link` |
| **An install gate** | CI extracts the three marked blocks below and runs them on a clean runner every night, so a doc that stopped being true fails a build | `python scripts/extract_doc_commands.py --file docs/quickstart.md --block local` |

## Prerequisites

Pick a substrate first; each needs a different machine.

**`--substrate compose` — everything in containers**

| Requirement | Why |
|-------------|-----|
| Docker Engine 24+ with the Compose v2 plugin | The stack is compose services; `genus init` refuses to plan without both |
| Python 3.11+ and `pip` | For the CLI itself. On current Debian and Ubuntu a system-wide `pip install` is refused (PEP 668) — use `pipx install genusos`, or a virtualenv |
| A non-root account | The containers run as that account's uid so they can read the workspace; `genus init` refuses a root install rather than leaving files nothing else can open |
| A provider API key | The wizard makes a real one-token completion before it records anything |
| ~20 GB of disk | Images, the database volume and the local RAG models |
| An NVIDIA container runtime | *Optional.* `nvidia-smi` answering is what decides whether the GPU overlay joins |

**`--substrate local` — the platform on this machine**

| Requirement | Why |
|-------------|-----|
| Python 3.11+ | The platform itself |
| PostgreSQL 16+ with the pgvector extension | Memory, CRM and the engine's own tables. **Required** — `genus init` blocks in phase 1 without it |
| Redis 7+ | Sessions and the event bus. Also required |
| Ollama | *Optional.* Embeddings, reranking and local RAG generation. Without it, memory search has no embeddings and the wizard says so |
| A provider API key | As above |

## Option 0: one line

`install.sh` is a thin wrapper around everything below: it fetches the two
compose files **pinned to a release tag**, puts the CLI in a private
virtualenv, and hands over to `genus init`. It decides nothing the wizard
decides.

```bash
curl -fsSL https://ironsail-llc.github.io/genus-os/install.sh | bash -s -- --substrate compose
```

That line deliberately **installs nothing**. A pipe has no terminal on the
other end, so there is nowhere to confirm a plan — the script prints what it
would do and stops. Read it, then run it for real:

```bash
curl -fsSL https://ironsail-llc.github.io/genus-os/install.sh | bash -s -- \
  --substrate compose --yes \
  --owner-name "Ada Lovelace" --owner-email ada@example.com
```

| Flag | What it does |
|------|--------------|
| `--substrate compose\|pipx` | `compose` runs the whole stack in containers; `pipx` installs the CLI and runs the `local` substrate. Default: `compose` when Docker and the Compose v2 plugin are both present |
| `--version vX.Y.Z` | The release to install. Default: `$GENUS_VERSION`, else the latest GitHub release, else the version the script shipped with |
| `--dir DIR` | Where a compose install lives (default `~/genus`) |
| `--owner-name`, `--owner-email` | The operator identity `owner.yaml` records. Required with `--yes`; `$GENUS_OWNER_NAME` and `$GENUS_OWNER_EMAIL` work too |
| `--yes` | Run the plan instead of previewing it |
| `--dry-run` | Print the plan and every command, execute nothing |

A provider key is read from the environment and **never** prompted for —
`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`
or `GROQ_API_KEY`. Export one before the line above, or give the wizard one in
the browser at the end.

What the script will not do: run as root, run under `sh`, `sudo` anything,
download from `main` rather than a release tag, or pipe anything else into a
shell. Rather not pipe a URL into a shell at all? Download it, read it, run it:

```bash
curl -fsSLO https://ironsail-llc.github.io/genus-os/install.sh
less install.sh
bash install.sh --substrate compose --yes \
  --owner-name "Ada Lovelace" --owner-email ada@example.com
```

From a checkout the same script is `scripts/install.sh`, which is what CI
replays on a fresh machine every night — the site serves a copy of this file,
not a second implementation:

<!-- install-gate: install-sh -->
```bash
export ROBOTHOR_DB_PASSWORD=choose-a-password
export OPENROUTER_API_KEY=sk-your-key
bash scripts/install.sh --substrate compose --yes \
  --dir ~/genus \
  --owner-name "Ada Lovelace" --owner-email ada@example.com
```
<!-- /install-gate -->

The rest of this page is what that script does, step by step, and the two
substrates it chooses between.

## Option A: the whole stack in containers (`--substrate compose`)

The enterprise pilot path, and the shortest one: released images from GHCR, a
one-shot migration the services wait on, and a dashboard on
`http://127.0.0.1:3004`.

Run it as the account that will own the instance, **not** as root.

The wheel does not carry the compose files, so fetch the two the stack is made
of first. `genus.env` — the 0600 file holding every credential — is written
inside the workspace, which the block below makes the current directory by
passing `--workspace .`:

<!-- install-gate: compose -->
```bash
pip install genusos
mkdir -p ~/genus && cd ~/genus
curl -fsSLO https://raw.githubusercontent.com/Ironsail-llc/genus-os/main/infra/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/Ironsail-llc/genus-os/main/infra/docker-compose.apps.yml
export ROBOTHOR_DB_PASSWORD=choose-a-password
export OPENROUTER_API_KEY=sk-your-key
genus init --substrate compose --yes --workspace . --owner-name "Ada Lovelace" --owner-email ada@example.com
export ROBOTHOR_WORKSPACE="$PWD"
genus doctor --json
```
<!-- /install-gate -->

CI replays this block on a fresh machine every night.

`genus doctor` runs on the HOST, where nothing has handed it the database
password — so it reads `genus.env` itself, from the workspace, the same file
the containers get through `env_file`. It refuses to read that file unless it
is mode 0600 and says so, because it holds the database password and the
provider key.

What the substrate adds to the wizard:

| Step | What it does |
|------|--------------|
| `prereqs` | Docker 24+ and Compose v2 are **required**; a root install is refused; `nvidia-smi` is optional and decides whether the GPU overlay is used |
| `render` | Writes `genus.env` (0600) — the database password, the provider key, the dashboard's `AUTH_SECRET` and `GENUS_BRIDGE_SSO_SECRET`, and the uid and gid the containers run as — copies `owner.yaml` into the workspace the containers mount, and picks the image tag |
| `up-infra` | Starts PostgreSQL, Redis and Ollama on their own, then waits for Ollama to accept connections |
| `models` | Pulls the RAG models into the stack's Ollama, over its published port |
| `up` | One `docker compose --env-file genus.env -f docker-compose.yml -f docker-compose.apps.yml up -d` for the platform services |
| `wait` | Polls `/ready` on the engine, bridge, orchestrator and dashboard (`--wait-timeout`, default 180s) and names whichever did not answer |

The `up` is split in two on purpose. The dashboard and the bridge only start
once the orchestrator is healthy, and the orchestrator's readiness depends on
what is in Ollama — which starts empty. Bringing everything up at once meant
`docker compose up` exited 1 on a machine that had never run the stack, and
the bridge and dashboard were never created at all.

There is no migration step to run: the compose file carries a one-shot
`migrate` service that the engine, bridge and orchestrator wait on with
`service_completed_successfully`, and `verify` confirms the result through the
doctor's `db.migrations` check.

**Image tags.** The release build publishes `vX.Y.Z`, `vX.Y`, `vX` and
`sha-<short>` and deliberately **no** `latest`, so `GENUS_IMAGE_TAG` has no
default in the compose file — `docker compose` refuses to run without it rather
than pulling a tag that does not exist. `genus init` writes the version of the
CLI you installed into `genus.env`; `--image-tag vX.Y.Z` overrides it.

`genus init --substrate compose --yes --json` emits the usual document with
three extra keys: `compose.files`, `compose.images` and `compose.ready`.

**Sign-in.** The wizard turns on local email+password for the instance and mints
the two shared secrets the dashboard and the bridge authenticate each other
with. Nothing else is needed on day one; OIDC or Cloudflare Access can be added
later, and `genus doctor` will tell you when one is configured.

Upgrades, the dev and GPU overlays, and what each variable in `genus.env` does
are in [Deployment](deployment.md#docker-compose).

## Option B: the platform on this machine (`--substrate local`)

The default. PostgreSQL and Redis must already be installed and running —
`genus init` REQUIRES both for the `local` substrate and blocks in phase 1
without them, so the acceptance gate runs this block on an image that provides
them:

<!-- install-gate: local -->
```bash
pip install genusos
export OPENROUTER_API_KEY=sk-your-key
genus init --yes --owner-name "Ada Lovelace" --owner-email ada@example.com
genus doctor --json
```
<!-- /install-gate -->

CI replays this block on a fresh machine every night.

`genus init` runs in two phases. The first prints a plan — every step, and
whether it will create something, find it already there, or skip it — and
checks all of it before writing anything:

```
  Plan (local):
    + ack         genus init writes an operator identity to ~/.robothor/owner.yaml ...
    + prereqs     5 present
    + provider    will test openrouter/openai/gpt-5.4 with a 1-token completion
    + identity    will write ~/.robothor/owner.yaml
    - models      skipped (Ollama is not reachable, so memory search has no embeddings)
    ...
```

If a required check fails, **nothing is written** and the command exits 1
naming the check. That is the whole contract of `--yes`: it either produces a
running instance or changes nothing. Fix what it named and run it again — each
finished step is recorded in `<workspace>/.robothor/init_state.yaml`, so a
re-run resumes rather than restarts.

The provider step makes a real one-token completion before it records
anything. A key that does not work fails here, in ten seconds, rather than on
your first message. `--offline` records the choice unprobed and says so — and
deliberately does not count as done, so the next run probes it for real.

Re-running is routine. An existing `owner.yaml` is never overwritten, the two
sign-in secrets are never rotated, and completed steps are skipped — except
`ack`, `signin`, `services`, `verify` and `link`, each of which mints or checks
something that has to be current. (On the compose substrate `render`,
`up-infra`, `up` and `wait` re-run too.)

Where a `local` install puts things:

| What | Where |
|------|-------|
| Operator identity | `~/.robothor/owner.yaml` |
| Settings (database, provider model, secrets backend) | `<workspace>/.robothor/config.yaml` |
| Sign-in secrets (`AUTH_SECRET`, `GENUS_BRIDGE_SSO_SECRET`) | `<workspace>/genus.env`, mode 0600 |
| Provider key | The instance vault, slot 1 |
| Telegram bot token | **Nowhere, on this substrate.** `--telegram-token` verifies it with `getMe` and records only the bot's name; see [Next steps](#next-steps) for where to put the token itself |
| Resume state | `<workspace>/.robothor/init_state.yaml` |
| Workspace pointer (`ROBOTHOR_WORKSPACE`) | `<workspace>/.env` |
| Agent manifests and fleet defaults | `<workspace>/docs/agents/` (instance data) |

### Start the services

`genus init` starts nothing by default: launching two daemons as a side effect
of a configuration command is a surprise in a provisioning script and at a
terminal alike. It prints the two commands that start **this** install, worded
for it —

- a wheel install: `genus engine start`, then `genus serve` (which needs the
  API extra: `pip install "genusos[api]"`);
- a checkout with the units installed: `sudo systemctl start robothor-engine`,
  then `sudo systemctl start robothor-bridge robothor-app`.

`genus init --start` runs them for you instead of printing them.

Started by hand, they do **not** read the `genus.env` the wizard just minted —
that is what the systemd units' `EnvironmentFile=` is for — so source it first,
or the bridge comes up and refuses every SSO exchange:

```bash
set -a; . "$ROBOTHOR_WORKSPACE/genus.env"; set +a
genus engine start
genus serve --host 127.0.0.1 --port 9099
```

What ends up listening where, and what ships it:

| Port | Process | Comes from |
|------|---------|------------|
| 18800 | The agent engine | the wheel (`genus engine start`) |
| 9099 | The orchestrator / RAG API | the wheel plus the `[api]` extra (`genus serve`) |
| 9100 | The bridge — CRM, auth, and the first-run `/api/setup/*` routes | the checkout (`crm/bridge/bridge_service.py`), the `robothor-bridge` unit, or the compose `bridge` service |
| 3004 | The Helm dashboard, which serves the `/setup` **page** | the `robothor-app` unit, or the compose `dashboard` service |

The bridge's own `/ready` includes a check against the orchestrator, so
starting the bridge alone answers 503 for ever. Start the orchestrator first.

**The link `genus init` prints is on `:3004`, so it needs the dashboard.** That
is the Next.js app in `app/` — the compose substrate publishes it, and
`robothor-app.service` runs it from a checkout.

The wheel carries the `robothor` package and nothing else: not `app/`, not
`crm/bridge/`. So a box set up with `pip install genusos` alone has the engine
and the orchestrator, and neither the dashboard that serves the `/setup` page
nor the bridge that serves the routes behind it — nothing answers that link.
Two ways to have a first-run wizard: `--substrate compose`, which brings all
four services, or a checkout, where the units run the bridge and the dashboard
from the source tree. (CI proves the wheel path by running the bridge from the
checkout and walking `/api/setup/*` on `:9100` directly, which is the API the
page drives — not a route to recommend to a human.)

`systemd` and `helm` are not selectable from `--substrate`; `genus init` says so
rather than pretending they do not exist. Installing the units on a box the
wizard has already set up is
[one script](deployment.md#systemd-services).

## Option C: containers for the dependencies only

`genus init --docker` writes a `docker-compose.yml` into the workspace with
PostgreSQL+pgvector, Redis and Ollama, starts them, and then runs the same
`local` wizard against them — the platform itself still runs on the host.

```bash
pip install genusos
genus init --docker
```

## The setup wizard

`genus init` ends with a line like this:

```
  Open the setup wizard (the link works once, for 30 minutes):
    http://127.0.0.1:3004/setup?token=...
```

Open it. That page is the only way into a fresh instance — `genus init`
deliberately creates no account, so the sign-in page has nothing to offer yet.
An install that claimed the instance on your behalf would close `/setup` before
you ever opened it, and leave an owner account with no way to sign in.

Six steps, in the order in which things become possible:

| Step | What it asks | Notes |
|------|--------------|-------|
| Welcome | Nothing | Shows what the instance already detected: providers with a key, reachable models, whether Ollama answers |
| Operator | Full name, email, password (twice) | This is the account you sign in with from then on. Minimum 12 characters, hashed with argon2id |
| Provider | Provider, API key, default model | The key is tested with a real completion before the step will let you past. A failed test is shown, not swallowed — "configured but not working" is the state this wizard exists to prevent |
| Channel | A Telegram bot token | **Optional.** Verified with `getMe` if you give one. Skip it; nothing later needs it |
| Agents | A preset — minimal, standard or full | Minimal is three agents and nothing scheduled; standard adds email triage, calendar watch and briefings; full is the whole catalogue |
| Two-factor | Enrol an authenticator | Required for the owner while local email+password is on: a password is otherwise the whole of what protects the instance and every credential in it |

From the Provider step onwards the page carries a strip of the doctor's
required checks, so you can see the instance become healthy as you fill it in.

**The security properties, because they are the reason the wizard exists:**

- **The link is single-use and expires in 30 minutes.** Only its SHA-256 digest
  is stored, in `<workspace>/.robothor/setup_token.yaml` (0600).
- **The token never persists in the browser.** It lives in React state only —
  not `localStorage`, not a cookie — and is stripped from the address bar as
  soon as it has been spent, so a screenshot, a shared link or a referrer
  header cannot carry it.
- **Credentials are write-only.** What you type goes into one POST and is
  cleared; no field is ever read back.
- **It stops working for good.** The moment an operator account exists, `/setup`
  and every route behind it answer 404, and `genus auth setup-link` refuses to
  mint another. The completion signal is the owner row in the database, not a
  file, so deleting something on disk does not reopen it.
- **Lost the link, or set the box up headless?** `genus auth setup-link` on the
  server mints a fresh one. It is deliberately local-only: shell access to the
  box is the one credential the wizard's gate can rely on before an account
  exists.
- **Not sitting at that machine?** `127.0.0.1` is loopback on the *server*.
  `genus init` prints an `ssh -L` line for that case; run it on your own
  machine, then open the link there.

You do not need to set `GENUS_LOCAL_LOGIN` yourself — the wizard turns email and
password sign-in on for the instance when it creates your account, and the
dashboard asks the bridge which methods are live rather than reading its own
environment.

Prefer a different lifetime for the link? `GENUS_SETUP_TOKEN_TTL_SECONDS`
(default 1800) sets it platform-wide, and `genus auth setup-link --ttl 2h`
widens one link. See the
[configuration reference](reference/configuration.md).

## When the doctor is red

`genus doctor` is the single answer to "is this instance actually working?". It
runs every check, groups them by category, and prints `✓` for a pass, `✗` for a
failure and `·` for a check that could not run. **A skip is never a pass.**

```bash
genus doctor                        # everything, as a table
genus doctor --json                 # the same report, machine-readable
genus doctor --only db.migrations   # one check
genus doctor --category database    # one category
genus doctor --offline              # nothing that costs money, leaves the box, or forks
genus doctor --fix                  # repair what can be repaired
genus doctor --fix --dry-run        # say what --fix would repair
```

Severity decides whether a finding is fatal: `required` means the instance does
not work, `recommended` means it works but something is drifting, `info` is for
the record.

| Exit code | Meaning |
|-----------|---------|
| 0 | no `required` check failed |
| 1 | a `required` check failed |
| 2 | the doctor itself could not run — an unknown `--only` id, a registry that would not import |

2 is deliberately separate from 1: "nothing is wrong" and "nothing was checked"
must never share an exit code, or a typo in a CI gate becomes a permanently
green build.

**What `--fix` covers.** Exactly two repairs, both idempotent, and both re-run
their own check afterwards so the report says what the re-run found rather than
what the repair claimed:

- `db.migrations` — applies pending migrations, the same work as `genus migrate`;
- `db.rbac_service_role` — seeds the `service` role;
- `db.rls_coverage` — applies the `tenant_isolation` policy to any tenant table that lacks it.

Everything else prints the command to run. `identity.owner_account` is
deliberately never auto-fixed: minting a privileged account from a diagnostic
that can run on a timer would be a privilege-escalation path, not a repair.

The three findings a fresh install actually hits:

| Finding | What it means | What to do |
|---------|---------------|------------|
| `db.migrations` fails | The schema is behind the manifest this build ships | `genus doctor --fix`, or `genus migrate`. If it reports **drift** or a ledger row this checkout does not ship, `--fix` refuses on purpose — read `genus migrate --status` and see [the schema section](deployment.md#the-schema) before touching anything |
| `provider.keys` or `provider.completion` fails | No credential resolved, or the fleet's default model would not answer one token | Store the key where the platform reads it: `genus vault set providers/openrouter/api_key <key>`, then re-run. A 402 means the key is capped, a 403 means the key may not use *that model* — a model problem, not a credential one |
| `secrets.bridge_sso` fails | `GENUS_BRIDGE_SSO_SECRET` is not where the process that needs it reads it — on systemd the units read `/etc/robothor/robothor.env`, not the workspace's `genus.env` | Point the units at it, per [the sign-in secrets](deployment.md#the-sign-in-secrets-the-units-need). Until you do, the bridge refuses every SSO exchange and nobody can sign in. This used to be invisible — the bridge reported ready for eight days while nobody could get in; the bridge's `/ready` now carries an `sso_secret` check of its own, and this doctor check is the other half |

`identity.owner_account` failing on a box where you have not opened `/setup`
yet is not a fault — the doctor reports it as a skip while first-run setup is
legitimately pending.

The dashboard's Health view serves the same report from `GET /api/doctor`,
offline and memoised for 30 seconds. Run the CLI when you need an answer taken
just now. Full check inventory: [Diagnostics](deployment.md#diagnostics-genus-doctor).

## Non-interactive mode

For CI or scripted installs, `--yes` takes defaults and the environment and
asks nothing:

```bash
export ROBOTHOR_DB_HOST=localhost
export ROBOTHOR_DB_PASSWORD=mypassword
export OPENROUTER_API_KEY=sk-your-key
genus init --yes --owner-name "Ada Lovelace" --owner-email ada@example.com
```

Want to see what it would do first? `genus init --dry-run` prints the plan and
writes nothing. `genus init --yes --json` emits one document on stdout — the
plan, every step's result, the first-run URL and the exit code — with the human
narration on stderr, so you can pipe it into `jq` and still watch it work. No
credential appears in either stream; the setup token appears exactly once,
inside `first_run_url`.

Every flag, for `genus init` and for every other verb, is in the
[CLI reference](reference/cli.md).

## Next steps

**Connect Telegram.** Nothing asks for a bot token during install, and a
Telegram-free instance is a supported deployment. To add one, the token goes
**in the environment the engine reads** — `ROBOTHOR_TELEGRAM_BOT_TOKEN` — which
is the one place the channel and `genus doctor` both look:

| Substrate | Put the token in |
|-----------|------------------|
| compose | `genus.env`, then `docker compose … up -d` to re-create the engine |
| systemd | `/etc/robothor/robothor.env` (or the secrets file your backend uses), then `sudo systemctl restart robothor-engine` |
| a wheel, by hand | the shell that runs `genus engine start` |

`genus config set ROBOTHOR_TELEGRAM_BOT_TOKEN …` is **refused**: the setting is
declared secret, and the command names `genus vault set` instead. That is the
one case where the vault is the wrong answer — nothing in the settings layer
reads a vault row for this credential, so a token stored there alone leaves the
channel dark. `genus init --telegram-token` is the supported route at install
time (it verifies the token with `getMe`, and on the compose substrate writes it
into `genus.env`). The chat id is not a secret and does go through `genus config`:

```bash
genus config set ROBOTHOR_TELEGRAM_CHAT_ID <chat-id>
genus doctor --only telegram.token
```

The `telegram.token` check calls `getMe` and reports a token that is set but not
shaped like one, or a chat id with no token — the two half-configured states
that otherwise look fine until an alert goes nowhere.

**Invite a user.** Onboarding is a closed allowlist: an account exists because
an operator added it, never because somebody reached the sign-in page.

```bash
genus user add --name "Bob" --email bob@example.com --role member
genus user set-password bob@example.com
genus user list
```

**Install more agents.** The preset the wizard installed is a starting fleet,
not a ceiling.

```bash
genus agent catalog
genus agent install <agent-id>
genus agent list
```

Manifests land in `<workspace>/docs/agents/` (instance data — yours, and they
survive platform upgrades). See `docs/AGENT_BUILDER.md` in the repository for writing
one from scratch.

**Read on:**

- [Configuration](configuration.md) — precedence, `genus config`, secrets backends
- [Deployment](deployment.md) — compose upgrades, systemd, Helm, diagnostics
- [CLI reference](reference/cli.md) — every verb and flag
- [Configuration reference](reference/configuration.md) — every setting
- [Memory System](memory-system.md) — facts, lifecycle, conflict resolution
- [Event Bus](event-bus.md) — Redis Streams pub/sub with RBAC
- `examples/` — runnable demos: `basic-memory`, `rag-chatbot`, `vision-sentry`, `full-stack`

## Using the memory API directly

The platform is a library as well as a service. Storing a fact and searching
semantically, with the embeddings the `models` step pulled:

```python
import asyncio
from robothor.memory.facts import store_fact, search_facts

async def main():
    fact = {
        "fact_text": "The project uses PostgreSQL with pgvector for semantic search",
        "category": "technical",
        "entities": ["PostgreSQL", "pgvector"],
        "confidence": 0.95,
    }
    fact_id = await store_fact(fact, "quickstart tutorial", "conversation")
    print(f"Stored fact #{fact_id}")

    results = await search_facts("what database do we use?", limit=3)
    for r in results:
        print(f"  [{r['similarity']:.3f}] {r['fact_text']}")

asyncio.run(main())
```

Ingestion extracts facts, resolves conflicts and builds the entity graph in one
call:

```python
import asyncio
from robothor.memory.ingestion import ingest_content

async def main():
    result = await ingest_content(
        content="Alice from Acme Corp decided to migrate their API to FastAPI. "
                "The deadline is March 15th.",
        source_channel="email",
        content_type="conversation",
    )
    print(f"Stored {result['facts_processed']} facts, "
          f"skipped {result['facts_skipped']} duplicates, "
          f"found {result['entities_stored']} entities")

asyncio.run(main())
```

The entity graph, the RAG pipeline and the HTTP API each have their own page —
[Memory System](memory-system.md) for the first two, and `genus serve` (or
`pip install "genusos[api]"` on a machine that only wants the API) for the
third.
