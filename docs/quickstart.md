# Quick Start

From zero to a working Genus OS instance in 10 minutes.

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 16+** with the pgvector extension
- **Redis 7+**
- **Ollama** (for embeddings, reranking, and generation)

## Option A: Docker (recommended)

```bash
pip install genusos
robothor init --docker   # Generates docker-compose, starts containers, runs migrations, pulls models
robothor status
robothor serve
```

The wizard creates a `~/robothor` workspace with a `docker-compose.yml` (PostgreSQL+pgvector, Redis, Ollama), starts the containers, runs the database migration, and pulls the required embedding/reranker models.

## Option B: Local Infrastructure

Install PostgreSQL with pgvector, Redis, and Ollama on your system, then:

```bash
pip install genusos
robothor init            # Interactive: prompts for DB config, runs migrations, pulls models
robothor status
robothor serve
```

The wizard checks prerequisites, prompts for database connection details, creates the workspace, runs migrations, and pulls Ollama models.

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

For CI or scripted installs, use `--yes` with environment variables:

```bash
export ROBOTHOR_DB_HOST=localhost
export ROBOTHOR_DB_PASSWORD=mypassword
robothor init --yes      # Uses env vars + defaults, zero prompts
```

### Flags

| Flag | Description |
|------|-------------|
| `--yes`, `-y` | Non-interactive mode (uses env vars + defaults) |
| `--docker` | Generate docker-compose.yml and start containers |
| `--skip-models` | Skip Ollama model pulling |
| `--skip-db` | Skip database migration |
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
