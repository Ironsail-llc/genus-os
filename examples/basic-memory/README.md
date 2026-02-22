# Basic Memory Example

A minimal example showing how to use Robothor's memory system: store facts, search semantically, and explore the entity knowledge graph.

## Prerequisites

1. **PostgreSQL 16** with [pgvector](https://github.com/pgvector/pgvector) extension installed.
2. **Ollama** running locally with an embedding model pulled:
   ```bash
   ollama pull qwen3-embedding:0.6b
   ```
3. **Database setup** -- create the database and tables:
   ```sql
   CREATE DATABASE robothor_memory;
   \c robothor_memory
   CREATE EXTENSION IF NOT EXISTS vector;

   CREATE TABLE memory_facts (
       id SERIAL PRIMARY KEY,
       fact_text TEXT NOT NULL,
       category TEXT NOT NULL DEFAULT 'personal',
       entities TEXT[] DEFAULT '{}',
       confidence FLOAT DEFAULT 1.0,
       source_content TEXT,
       source_type TEXT DEFAULT 'api',
       embedding vector(1024),
       metadata JSONB DEFAULT '{}',
       is_active BOOLEAN DEFAULT TRUE,
       created_at TIMESTAMPTZ DEFAULT NOW()
   );

   CREATE TABLE memory_entities (
       id SERIAL PRIMARY KEY,
       name TEXT NOT NULL,
       entity_type TEXT NOT NULL,
       aliases TEXT[] DEFAULT '{}',
       mention_count INT DEFAULT 1,
       last_seen TIMESTAMPTZ DEFAULT NOW(),
       UNIQUE(name, entity_type)
   );

   CREATE TABLE memory_relations (
       id SERIAL PRIMARY KEY,
       source_entity_id INT REFERENCES memory_entities(id),
       target_entity_id INT REFERENCES memory_entities(id),
       relation_type TEXT NOT NULL,
       fact_id INT REFERENCES memory_facts(id),
       confidence FLOAT DEFAULT 1.0,
       UNIQUE(source_entity_id, target_entity_id, relation_type)
   );
   ```

## Install

```bash
pip install robothor[llm]
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
