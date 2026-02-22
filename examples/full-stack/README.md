# Full Stack Example

Run the complete Robothor stack with Docker Compose: PostgreSQL with pgvector, Redis, Ollama, and the Robothor API server. One command to get a fully functional AI memory and RAG system.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) v2+
- At least 8 GB of RAM (16 GB recommended for larger LLM models)
- At least 10 GB of disk space for models

## Quick Start

```bash
# 1. Copy the example environment file
cp .env.example .env

# 2. Edit .env with your preferred settings (defaults work for local dev)
#    At minimum, set a database password.

# 3. Start the stack
docker compose up -d

# 4. Wait for Ollama to pull models (first run only, may take a few minutes)
docker compose logs -f ollama

# 5. Pull the required models into Ollama
docker compose exec ollama ollama pull qwen3-embedding:0.6b
docker compose exec ollama ollama pull qwen3-next:latest

# 6. Verify everything is running
docker compose ps
curl http://localhost:9099/health
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| `postgres` | 5432 | PostgreSQL 16 + pgvector 0.8 |
| `redis` | 6379 | Redis 7 (caching, session store) |
| `ollama` | 11434 | Ollama LLM server (embeddings, generation) |
| `robothor` | 9099 | Robothor API (memory, RAG, ingestion) |

## Usage

Once the stack is running, you can interact with the Robothor API:

### Ingest content
```bash
curl -X POST http://localhost:9099/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "content": "The quarterly review showed a 15% increase in user engagement.",
    "source_channel": "api",
    "content_type": "note"
  }'
```

### Search memory
```bash
curl "http://localhost:9099/search?q=user+engagement&limit=5"
```

### RAG query
```bash
curl -X POST http://localhost:9099/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What happened with user engagement?"}'
```

### Health check
```bash
curl http://localhost:9099/health
```

## Data Persistence

All data is persisted in Docker volumes:

| Volume | Contents |
|--------|----------|
| `pgdata` | PostgreSQL database files |
| `redis-data` | Redis persistence (AOF) |
| `ollama-models` | Downloaded LLM models |

To reset everything:
```bash
docker compose down -v
```

## Scaling

For production use, consider:

- **GPU acceleration**: Mount your NVIDIA GPU into the Ollama container by uncommenting the `deploy` section in `docker-compose.yml`.
- **Larger models**: Pull bigger generation models for better quality (e.g., `llama3.1:70b` if you have the VRAM).
- **External PostgreSQL**: Point `ROBOTHOR_DB_HOST` to a managed PostgreSQL instance with pgvector support.
- **Multiple workers**: Scale the `robothor` service with `docker compose up -d --scale robothor=3` behind a load balancer.
