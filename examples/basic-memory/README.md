# Basic Memory Example

A minimal example showing how to use Genus OS's memory system: store facts, search semantically, and explore the entity knowledge graph.

## Prerequisites

1. **PostgreSQL 16** with [pgvector](https://github.com/pgvector/pgvector) extension installed.
2. **Ollama** running locally with an embedding model pulled:
   ```bash
   ollama pull qwen3-embedding:0.6b
   ```

## Install

```bash
pip install robothor
```

## Configure

Set environment variables (or rely on defaults):

```bash
export ROBOTHOR_DB_HOST=127.0.0.1
export ROBOTHOR_DB_PORT=5432
export ROBOTHOR_DB_NAME=robothor_memory
export ROBOTHOR_DB_USER=your_user
export ROBOTHOR_DB_PASSWORD=your_password
```

## Set Up Database

```bash
robothor migrate
```

This creates all required tables (memory, entities, CRM, audit, etc.). Safe to re-run — uses `IF NOT EXISTS` everywhere.

## Run

```bash
python main.py
```

## What It Does

1. **Stores facts** -- extracts structured facts from plain text using a local LLM, generates vector embeddings, and saves them to PostgreSQL.
2. **Searches semantically** -- given a natural language query, finds the most relevant stored facts using pgvector cosine similarity.
3. **Builds a knowledge graph** -- extracts named entities (people, technologies, organizations) and their relationships from stored facts.
4. **Queries the graph** -- looks up an entity and shows all its connections.

## Architecture

```
Plain Text
    |
    v
LLM Extraction (Ollama)
    |
    v
Structured Facts + Vector Embeddings
    |
    v
PostgreSQL + pgvector
    |
    v
Semantic Search / Entity Graph Queries
```
