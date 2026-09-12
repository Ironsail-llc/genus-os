# Quick Start

From zero to a working Genus OS instance in 10 minutes.

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 16+** with the pgvector extension
- **Redis 7+**
- **Ollama** (for embeddings, reranking, and generation)

## Option A: Docker for the infrastructure

`genus init --docker` writes a `docker-compose.yml` into the workspace with
PostgreSQL+pgvector, Redis and Ollama, starts them, and then runs the same
wizard as below against them.

```bash
pip install genusos
genus init --docker
```

## Option B: Local infrastructure

Install PostgreSQL with pgvector, Redis and (optionally) Ollama, export a
provider key, then run the wizard.

These are the exact commands CI replays on a fresh machine. The block assumes
PostgreSQL and Redis are already installed and running — `genus init` REQUIRES
both for the `local` substrate and blocks in phase 1 without them, so the
acceptance gate runs it on an image that provides them (or use Option A above,
which starts them in containers first):

<!-- install-gate: local -->
```bash
pip install genusos
export OPENROUTER_API_KEY=sk-your-key
genus init --yes --owner-name "Ada Lovelace" --owner-email ada@example.com
genus doctor --json
```
<!-- /install-gate -->

`genus init` runs in two phases. The first prints a plan — every step, and
whether it will create something, find it already there, or skip it — and
checks all of it before writing anything:

```
  Plan (local):
    + ack         genus init writes an operator identity to ~/.robothor/owner.yaml ...
    + prereqs     5 present
    + provider    will test openrouter/openai/gpt-5.4 with a 1-token completion
    + identity    will write /home/you/.robothor/owner.yaml
    - models      skipped (the chosen provider is not Ollama)
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

Open it. That page is the only way into a fresh instance — there is no account
yet, so the sign-in page has nothing to offer — and it walks you through the
operator account, one provider API key (tested for real before it lets you
past), an optional Telegram channel, and your first agents. It ends in the
chat, signed in.

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
| `--substrate NAME` | Where the instance runs (`local`; compose/systemd/helm are not yet selectable) |
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
