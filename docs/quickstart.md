# Quick Start

From zero to a working Genus OS instance in 10 minutes.

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 16+** with the pgvector extension
- **Redis 7+**
- **Ollama** (for embeddings, reranking, and generation)

Those are what the `local` substrate needs. `--substrate compose` needs none of
them except Python — PostgreSQL, Redis and Ollama all run in containers.

## Option A: The whole stack in Docker (`--substrate compose`)

The enterprise pilot path, and the shortest one: released images from GHCR, a
one-shot migration the services wait on, and a dashboard on
`http://127.0.0.1:3004`. The box needs Docker Engine 24+ with the Compose v2
plugin, plus Python 3.11+ and pip for the CLI itself. On current Debian and
Ubuntu a system-wide `pip install` is refused (PEP 668) — use `pipx install
genusos`, or a virtualenv.

Run it as the account that will own the instance, **not** as root: the
containers run as that account so they can read the workspace, and `genus init`
refuses a root install rather than leaving files nothing else can open.

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
| `prereqs` | Docker 24+ and Compose v2 are **required**; `nvidia-smi` is optional and decides whether the GPU overlay is used |
| `render` | Writes `genus.env` (0600) — the database password, the provider key, the dashboard's `AUTH_SECRET` and `GENUS_BRIDGE_SSO_SECRET`, and the uid the containers run as — copies `owner.yaml` into the workspace the containers mount, and picks the image tag |
| `up-infra` | Starts PostgreSQL, Redis and Ollama on their own, then waits for Ollama to accept connections |
| `models` | Pulls the RAG models into the stack's Ollama, over its published port |
| `up` | One `docker compose --env-file genus.env -f docker-compose.yml -f docker-compose.apps.yml up -d` for the platform services |
| `wait` | Polls `/ready` on the engine, bridge, orchestrator and dashboard (`--wait-timeout`, default 180s) and names whichever did not answer |

The `up` is split in two on purpose. The dashboard and the bridge only start
once the orchestrator is healthy, and the orchestrator's readiness depends on
what is in Ollama — which starts empty. Bringing everything up at once meant
`docker compose up` exited 1 on a machine that had never run the stack, and
the bridge and dashboard were never created at all.

There is no migration step: the compose file carries a one-shot `migrate`
service that the engine, bridge and orchestrator wait on with
`service_completed_successfully`, and `verify` confirms the result through the
doctor's `db.migrations` check.

**Image tags.** The release build publishes `vX.Y.Z`, `vX.Y`, `vX` and
`sha-<short>` and deliberately **no** `latest`, so `GENUS_IMAGE_TAG` has no
default in the compose file — `docker compose` refuses to run without it rather
than pulling a tag that does not exist. `genus init` writes the version of the
CLI you installed into `genus.env`; `--image-tag vX.Y.Z` overrides it.

`genus init --substrate compose --yes --json` emits the usual document with one
extra key: `compose.files`, `compose.images` and `compose.ready`.

**Sign-in.** The wizard turns on local email+password for the instance and mints
the two shared secrets the dashboard and the bridge authenticate each other
with. Nothing else is needed on day one; OIDC or Cloudflare Access can be added
later, and `genus doctor` will tell you when one is configured.

## Option B: Docker for the infrastructure only

`genus init --docker` writes a `docker-compose.yml` into the workspace with
PostgreSQL+pgvector, Redis and Ollama, starts them, and then runs the same
wizard as below against them — the platform itself still runs on the host.

```bash
pip install genusos
genus init --docker
```

## Option C: Everything local

Install PostgreSQL with pgvector, Redis and (optionally) Ollama, export a
provider key, then run the wizard.

These are the exact commands CI replays on a fresh machine. The block assumes
PostgreSQL and Redis are already installed and running — `genus init` REQUIRES
both for the `local` substrate and blocks in phase 1 without them, so the
acceptance gate runs it on an image that provides them (or use Option A or B
above, which start them in containers first):

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
finished step is recorded, so a re-run resumes rather than restarts.

The provider step makes a real one-token completion before it records
anything. A key that does not work fails here, in ten seconds, rather than on
your first message. `--offline` records the choice unprobed and says so.

## Open the link `genus init` printed

`genus init` ends with a line like this:

```
  Open the setup wizard (the link works once, for 30 minutes):
    http://127.0.0.1:3004/setup?token=...
```

Open it. That page is the only way into a fresh instance — `genus init`
deliberately creates no account, so the sign-in page has nothing to offer yet —
and it walks you through the operator account, one provider API key (tested for
real before it lets you past), an optional Telegram channel, and your first
agents. It ends in the chat, signed in.

The wizard is what creates your account, with your password. `genus init`
writes the identity (`~/.robothor/owner.yaml`) and mints the link, and stops
there: an install that claimed the instance on your behalf would close `/setup`
before you ever opened it, and leave an owner account with no way to sign in.

Three things worth knowing:

- **The link is single-use and expires in 30 minutes.** Lost it, or set the box
  up headless? Run `genus auth setup-link` on the server for a fresh one.
- **It stops working for good.** The moment an operator account exists, `/setup`
  and every route behind it answer 404. Sign in at the dashboard from then on.
- **Not sitting at that machine?** `127.0.0.1` is loopback on the *server*.
  `genus init` prints an `ssh -L` line for that case; run it on your own
  machine, then open the link there.

You do not need to set `GENUS_LOCAL_LOGIN` yourself — the wizard turns email and
password sign-in on for the instance when it creates your account, and the
dashboard asks the bridge which methods are live rather than reading its own
environment.

Prefer a different lifetime for the link? `GENUS_SETUP_TOKEN_TTL_SECONDS`
(default 1800) sets it — see the [configuration reference](reference/configuration.md).

### Non-interactive mode

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
narration on stderr, so you can pipe it into `jq` and still watch it work.

### Flags

| Flag | Description |
|------|-------------|
| `--yes`, `-y` | Non-interactive: take defaults and the environment, ask nothing |
| `--dry-run` | Print the plan and write nothing |
| `--json` | Plan, step results and first-run URL as JSON on stdout |
| `--substrate NAME` | Where the instance runs (`local` or `compose`; systemd/helm are not yet selectable) |
| `--offline` | Record the provider choice without testing it |
| `--provider ID` / `--model ID` | Choose the provider and model instead of being asked |
| `--preset NAME` | Agent catalogue preset to install (see `genus agent catalog`) |
| `--secrets-backend env\|file\|sops` | Where this instance's credentials come from |
| `--telegram-token TOKEN` | Verify and configure a Telegram bot (optional; nothing asks for one) |
| `--owner-name` / `--owner-email` | Operator identity for `owner.yaml` |
| `--start` | Start the services at the end instead of printing the commands |
| `--docker` | Generate docker-compose.yml and start the infrastructure containers |
| `--skip-models` | Skip Ollama model pulling |
| `--skip-db` | Skip the database steps |
| `--workspace PATH` | Workspace directory (default: `~/robothor`) |
| `--wait-timeout SECONDS` | How long `--substrate compose` waits for the stack to answer `/ready` (default: 180) |
| `--image-tag TAG` | Released image tag `--substrate compose` runs (default: `v<this CLI's version>`) |

## Store Your First Fact

```python
import asyncio
from robothor.memory.facts import store_fact, search_facts

async def main():
    # Store a fact with embedding
    fact = {
        "fact_text": "The project uses PostgreSQL with pgvector for semantic search",
        "category": "technical",
        "entities": ["PostgreSQL", "pgvector"],
        "confidence": 0.95,
    }
    fact_id = await store_fact(fact, "quickstart tutorial", "conversation")
    print(f"Stored fact #{fact_id}")

    # Search semantically
    results = await search_facts("what database do we use?", limit=3)
    for r in results:
        print(f"  [{r['similarity']:.3f}] {r['fact_text']}")

asyncio.run(main())
```

## Build the Knowledge Graph

```python
import asyncio
from robothor.memory.entities import upsert_entity, add_relation, get_entity

async def main():
    # Create entities. upsert_entity returns None for an unusable name
    # (a bare UUID, or under two characters) -- check `is not None`.
    pg_id = await upsert_entity("PostgreSQL", "technology")
    proj_id = await upsert_entity("Robothor", "project")

    # Add a relationship
    if pg_id is not None and proj_id is not None:
        await add_relation(proj_id, pg_id, "uses", confidence=0.95)

    # Query the graph
    info = await get_entity("Robothor")
    print(f"Entity: {info['name']} ({info['entity_type']})")
    for rel in info["relations"]:
        print(f"  -> {rel['relation_type']} -> {rel.get('target_name', rel.get('source_name'))}")

asyncio.run(main())
```

## Ingest Content

The ingestion pipeline extracts facts, resolves conflicts, and builds the entity graph automatically:

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

## Start the API Server

```bash
pip install "genusos[api]"
robothor serve --host 0.0.0.0 --port 9099
```

## Run RAG Queries

```python
import asyncio
from robothor.rag.pipeline import run_pipeline

async def main():
    result = await run_pipeline("What database does the project use?")
    print(result["answer"])
    print(f"Profile: {result['profile']}, Time: {result['timing']['total_ms']}ms")

asyncio.run(main())
```

## Next Steps

- [Memory System](memory-system.md) -- deep dive on facts, lifecycle, conflict resolution
- [Event Bus](event-bus.md) -- Redis Streams pub/sub with RBAC
- [Configuration](configuration.md) -- full env var reference
- [Deployment](deployment.md) -- Docker Compose, systemd, production checklist
- See `examples/` for complete runnable demos: `basic-memory`, `rag-chatbot`, `vision-sentry`, `full-stack`
