# Quick Start

From zero to a working Robothor brain in 10 minutes.

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 16+** with the pgvector extension
- **Redis 7+**
- **Ollama** (for embeddings, reranking, and generation)

## Option A: Docker (recommended)

```bash
git clone https://github.com/robothor-ai/robothor.git
cd robothor

# Configure
cp infra/robothor.env.example .env
# Edit .env -- at minimum, set ROBOTHOR_DB_PASSWORD

# Start infrastructure
docker compose -f infra/docker-compose.yml up -d

# Install the package
pip install -e ".[all]"

# Run migrations
psql -U robothor -d robothor_memory -f infra/migrations/001_init.sql

# Pull required Ollama models
docker exec robothor-ollama ollama pull qwen3-embedding:0.6b
docker exec robothor-ollama ollama pull Qwen3-Reranker-0.6B:F16

# Check status
robothor status
```

## Option B: Manual Setup

Install PostgreSQL with pgvector, Redis, and Ollama on your system, then:

```bash
pip install robothor

# Configure
export ROBOTHOR_DB_HOST=localhost
export ROBOTHOR_DB_NAME=robothor_memory
export ROBOTHOR_DB_USER=your-user
export ROBOTHOR_DB_PASSWORD=your-password

# Create database and run migrations
createdb robothor_memory
psql -d robothor_memory -c "CREATE EXTENSION IF NOT EXISTS vector"
psql -d robothor_memory -f infra/migrations/001_init.sql

# Pull Ollama models
ollama pull qwen3-embedding:0.6b
ollama pull Qwen3-Reranker-0.6B:F16

robothor status
```

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
    # Create entities
    pg_id = await upsert_entity("PostgreSQL", "technology")
    proj_id = await upsert_entity("Robothor", "project")

    # Add a relationship
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
pip install robothor[api]
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
