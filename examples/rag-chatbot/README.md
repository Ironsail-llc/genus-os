# RAG Chatbot Example

Build a conversational chatbot powered by Retrieval-Augmented Generation (RAG). The chatbot searches a persistent memory store for relevant context and generates grounded answers with source citations.

## Prerequisites

1. **PostgreSQL 16** with pgvector extension and the memory tables created (see the basic-memory example README for schema).
2. **Ollama** running locally with these models pulled:
   - `qwen3-embedding:0.6b` (required, for semantic search)
   - `qwen3-next:latest` (required, for answer generation)
   - `dengcao/Qwen3-Reranker-0.6B:F16` (optional, for better result quality)
3. **Some data ingested** -- run the basic-memory example first, or ingest your own content.

## Install

```bash
pip install robothor[llm]
```

## Configure

```bash
export ROBOTHOR_DB_NAME=robothor_memory
export ROBOTHOR_DB_USER=your_user
export ROBOTHOR_DB_PASSWORD=your_password
```

## Run

```bash
python main.py
```

Type questions at the prompt. The chatbot will search your memory store, rerank results, and generate an answer with citations. Type `quit` or `exit` to stop.

## How It Works

1. **Query classification** -- the pipeline selects a retrieval profile (fast, general, research, code) based on the question type.
2. **Parallel retrieval** -- searches memory (pgvector cosine similarity) and optionally the web simultaneously.
3. **Cross-encoder reranking** -- Qwen3-Reranker scores each result as relevant/not-relevant to filter noise.
4. **Context injection** -- relevant results are formatted and injected into the LLM prompt.
5. **Generation** -- the LLM produces a grounded answer citing its sources.

## Architecture

```
User Question
    |
    v
Query Classification --> Profile Selection
    |
    v
Parallel Retrieval
  - Memory (pgvector)
  - Web (optional)
    |
    v
Cross-Encoder Reranking
    |
    v
Context Formatting
    |
    v
LLM Generation (Ollama)
    |
    v
Answer with Source Citations
```

## Modes

The script supports two modes:

- **Single-turn** (default) -- each question is independent.
- **Multi-turn** -- pass `--chat` to maintain conversation history across turns.
