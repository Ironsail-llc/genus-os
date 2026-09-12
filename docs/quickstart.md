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
| **An install gate** | CI extracts the two marked blocks below and runs them on a clean runner every night, so a doc that stopped being true fails a build | `python scripts/extract_doc_commands.py --file docs/quickstart.md --block local` |

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

## Option A: the whole stack in containers (`--substrate compose`)

The enterprise pilot path, and the shortest one: released images from GHCR, a
one-shot migration the services wait on, and a dashboard on
`http://127.0.0.1:3004`.

Run it as the account that will own the instance, **not** as root.

The wheel does not carry the compose files, so fetch the two the stack is made
of first. They land in the directory you run `genus init` from, which is also
where it writes `genus.env` — the 0600 file holding every credential:

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
sign-in secrets are never rotated, and completed steps are skipped — except the
security note, the verification and the link, each of which mints or checks
something that has to be current.

Where a `local` install puts things:

| What | Where |
|------|-------|
| Operator identity | `~/.robothor/owner.yaml` |
| Settings (database, provider model, secrets backend) | `<workspace>/.robothor/config.yaml` |
| Sign-in secrets (`AUTH_SECRET`, `GENUS_BRIDGE_SSO_SECRET`) | `<workspace>/genus.env`, mode 0600 |
| Provider key, Telegram token | The instance vault |
| Resume state | `<workspace>/.robothor/init_state.yaml` |
| Agent manifests and fleet defaults | `<workspace>/docs/agents/` (instance data) |

`systemd` and `helm` are designed but not yet selectable from `--substrate`;
`genus init` says so rather than pretending they do not exist. Installing the
units on a box the wizard has already set up is
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
- `db.rbac_service_role` — seeds the `service` role.

Everything else prints the command to run. `identity.owner_account` is
deliberately never auto-fixed: minting a privileged account from a diagnostic
that can run on a timer would be a privilege-escalation path, not a repair.

The three findings a fresh install actually hits:

| Finding | What it means | What to do |
|---------|---------------|------------|
| `db.migrations` fails | The schema is behind the manifest this build ships | `genus doctor --fix`, or `genus migrate`. If it reports **drift** or a ledger row this checkout does not ship, `--fix` refuses on purpose — read `genus migrate --status` and see [the schema section](deployment.md#the-schema) before touching anything |
| `provider.keys` or `provider.completion` fails | No credential resolved, or the fleet's default model would not answer one token | Store the key where the platform reads it: `genus vault set providers/openrouter/api_key <key>`, then re-run. A 402 means the key is capped, a 403 means the key may not use *that model* — a model problem, not a credential one |
| `secrets.bridge_sso` fails | `GENUS_BRIDGE_SSO_SECRET` is not where the process that needs it reads it — on systemd the units read `/etc/robothor/robothor.env`, not the workspace's `genus.env` | Point the units at it, per [the sign-in secrets](deployment.md#the-sign-in-secrets-the-units-need). Until you do, the bridge refuses every SSO exchange and nobody can sign in **while `/ready` stays green** — the failure mode that locked an instance out for eight days |

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

**Connect Telegram.** Nothing asks for a bot token during install. Add one
afterwards from the dashboard's Settings page, or from a terminal:

```bash
genus vault set channels/telegram/bot_token <token>
genus config set ROBOTHOR_TELEGRAM_CHAT_ID <chat-id>
genus doctor --only telegram.token
```

The doctor's `telegram.token` check calls `getMe` and reports a token that is
set but not shaped like one, or a token with no chat to deliver to — the two
half-configured states that otherwise look fine until an alert goes nowhere.

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
