# Robothor Memory System
**Version:** 4.2 (Intra-Day Consolidation + Cross-Domain Insights)
**Last Updated:** 2026-03-10
**Hardware:** NVIDIA DGX Spark GB10 — 128GB unified memory, 20-core ARM (Cortex-X925/A725), CUDA 13.0

---

## Overview

Fact-first memory architecture mapping to cognitive functions. All LLM calls go through `llm_client.py` (local Ollama only). Zero API costs.

**v4.0 changes:** Dropped legacy short_term/long_term tables (dead since Feb). Fixed nightly maintenance (was crashing on non-existent model). Added quality gates on fact extraction. Implemented hybrid search (vector + BM25 keyword). Switched IVFFlat → HNSW index. Wired consolidation and forgetting. Added interactive session warmup with entity-aware context.

**v4.2 changes:** Added intra-day consolidation — after each continuous_ingest run, if >= 5 unconsolidated facts exist, a lightweight consolidation pass merges similar facts (min_group_size=2 vs nightly's 3). New `consolidated_at` column tracks which facts have been processed. Added cross-domain insight discovery — after consolidation, an LLM pass finds non-obvious connections between facts from different categories. Insights stored in `memory_insights` table with vector embeddings. New `search_insights` MCP tool. Nightly maintenance now includes a 72h insight discovery window and sweeps unconsolidated facts as safety net.

```
┌─────────────────────────────────────────────────────────────┐
│  WORKING MEMORY (agent_memory_blocks)                       │
│  • Named blocks: persona, user_profile, working_context    │
│  • Refreshed 4x daily by periodic_analysis.py              │
│  • Injected into agent warmup (cron + interactive)         │
│  • Staleness flagged if >24h old                           │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  FACT STORE (memory_facts) — PRIMARY MEMORY                 │
│  • Structured facts with quality gates (>15 chars, entities)│
│  • Hybrid search: vector (HNSW) + BM25 + RRF fusion       │
│  • Optional reranker (Qwen3-Reranker cross-encoder)        │
│  • Categories: personal, project, decision, preference,    │
│    event, contact, technical                               │
│  • Conflict resolution: dedup, update, contradiction       │
│  • Lifecycle: importance scoring, decay, consolidation,    │
│    forgetting (garbage collection)                         │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  ENTITY GRAPH (memory_entities + memory_relations)          │
│  • Named entities: people, projects, tech, orgs            │
│  • Typed relationships between entities                    │
│  • Used in search expansion (1-hop related entity facts)   │
│  • Mention counting and temporal tracking                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

| Component | Model | Size | Purpose |
|-----------|-------|------|---------|
| Generation | qwen3-next (Q4_K_M) | ~50 GB | Primary LLM (Qwen3-Next-80B-A3B) |
| Embeddings | qwen3-embedding:0.6b | ~0.6 GB | Convert text → 1024-dim vectors |
| Reranker | dengcao/Qwen3-Reranker-0.6B:F16 | ~1.2 GB | Cross-encoder relevance (yes/no) |
| Storage | PostgreSQL 16 | - | Database |
| Vector Search | pgvector 0.6.0 (HNSW) | - | Cosine similarity + BM25 hybrid |
| Web Search | SearXNG (Docker) | - | Metasearch engine |
| Orchestrator | FastAPI (uvicorn) | - | RAG pipeline on port 9099 |
| MCP Server | mcp library | - | Model Context Protocol interface |
| Object Detection | YOLOv8-nano | ~6 MB | Person/object detection (vision service) |
| Face Recognition | InsightFace buffalo_l | ~300 MB | Face detection + ArcFace embedding |
| Vision LLM | llama3.2-vision:11b | 7.8 GB | Scene analysis (on-demand) |

---

## Data Flow

### 1. Ingestion (Cross-Channel, Real-time)
```
New content (any channel: discord, email, cli, api, telegram, camera)
    │
    ▼
POST /ingest or ingest_content()
    │
    ▼
Extract facts via LLM (fact_extraction.py)
    │
    ▼
For each fact:
    ├→ Find similar existing facts (conflict_resolution.py)
    ├→ Classify: new / duplicate / update / contradiction
    ├→ Act: store / skip / supersede old fact
    └→ Extract and store entities + relations (entity_graph.py)
```

### 2. Retrieval (On-demand — Full RAG Pipeline)
```
Query → Classify → Select RAG Profile
    │
    ├→ Memory Search (pgvector cosine: short_term + long_term)
    ├→ Fact Search (pgvector cosine: memory_facts)
    └→ Web Search (SearXNG)
    │
    ▼
Merge results → Rerank with Qwen3-Reranker → Generate via qwen3-next
    │
    ▼
Response with citations
```

### 3. Conflict Resolution Flow
```
New fact extracted
    │
    ▼
find_similar_facts() — cosine search over memory_facts
    │
    ├─ No similar facts → store directly
    │
    └─ Similar fact found → classify_relationship() via LLM
        │
        ├─ "new"           → store, no conflict
        ├─ "duplicate"     → skip, don't store
        ├─ "update"        → store new, supersede old (is_active=False)
        └─ "contradiction" → store new, supersede old (is_active=False)
```

### 4. Lifecycle Maintenance
```
run_lifecycle_maintenance() — nightly (3 AM)
    │
    ├→ Step 1: Score importance for unscored facts (LLM-judged, 0.0-1.0)
    ├→ Step 2: Compute decay scores (recency × access × reinforcement × importance)
    ├→ Step 3: Prune low-quality facts (garbage collection)
    ├→ Step 4: Find and consolidate similar fact groups (LLM-merged, min_group=3)
    ├→ Step 5: Sweep remaining unconsolidated facts (safety net)
    └→ Step 6: Cross-domain insight discovery (72h window)

run_intraday_consolidation() — after each continuous_ingest (threshold >= 5)
    │
    ├→ Check unconsolidated count (consolidated_at IS NULL)
    ├→ Find similar facts (unconsolidated only, min_group=2, LIMIT 100)
    ├→ Consolidate matches (LLM-merged)
    ├→ Mark all unconsolidated facts as consolidated
    └→ Run cross-domain insight discovery (12h window, if merges occurred)
```

**Decay formula:**
- `recency = exp(-hours / 72h_half_life)`
- `access_boost = min(log(1 + access_count) / 5, 0.3)`
- `reinforcement_boost = min(log(1 + reinf_count) / 5, 0.2)`
- `importance_floor = importance * 0.4`
- `score = max(importance_floor, recency) + access_boost + reinforcement_boost`

**Cross-domain insights:**
- Selects recent facts from >= 2 categories (>= 3 facts total)
- LLM finds non-obvious connections between different domains
- Validated: >= 20 chars, >= 2 valid fact IDs, cross-category
- Deduped by cosine similarity (threshold 0.85)
- Stored in `memory_insights` table with vector embeddings
- Searchable via `search_insights` MCP tool

### 5. MCP Interface (for future model connections)
```
MCP Client (Claude Code, Cursor, etc.)
    │ stdio
    ▼
mcp_server.py
    │
    ├→ search_memory — semantic fact search
    ├→ store_memory — ingest + extract facts
    ├→ get_stats — memory statistics
    ├→ get_entity — entity graph lookup
    ├→ search_insights — cross-domain insight search
    ├→ look — capture snapshot + VLM scene description
    ├→ who_is_here — check who's detected by vision
    └→ enroll_face — enroll a person's face for recognition
```

---

## Database Schema

```sql
-- Tier 2: Short-term (48h TTL)
CREATE TABLE short_term_memory (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    content_type VARCHAR(50),
    embedding vector(1024),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '48 hours'),
    accessed_at TIMESTAMPTZ DEFAULT NOW(),
    access_count INTEGER DEFAULT 0
);

-- Tier 3: Long-term (permanent)
CREATE TABLE long_term_memory (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    summary TEXT,
    content_type VARCHAR(50),
    embedding vector(1024),
    metadata JSONB DEFAULT '{}',
    original_date TIMESTAMPTZ,
    archived_at TIMESTAMPTZ DEFAULT NOW(),
    source_tier2_ids INTEGER[]
);

-- Fact Store (structured facts with lifecycle)
CREATE TABLE memory_facts (
    id SERIAL PRIMARY KEY,
    fact_text TEXT NOT NULL,
    category VARCHAR(50) NOT NULL,    -- personal, project, decision, preference, event, contact, technical
    entities TEXT[] DEFAULT '{}',
    confidence FLOAT DEFAULT 1.0,
    source_content TEXT,
    source_type VARCHAR(50),
    source_channel VARCHAR(50),       -- discord, email, cli, api, telegram, etc.
    embedding vector(1024),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    superseded_by INTEGER REFERENCES memory_facts(id),
    is_active BOOLEAN DEFAULT TRUE,
    -- Lifecycle columns
    access_count INTEGER DEFAULT 0,
    last_accessed TIMESTAMPTZ DEFAULT NOW(),
    importance_score FLOAT DEFAULT 0.5,
    decay_score FLOAT DEFAULT 1.0,
    reinforcement_count INTEGER DEFAULT 0
);

-- Entity Graph
CREATE TABLE memory_entities (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    entity_type VARCHAR(50) NOT NULL,  -- person, project, organization, technology, location, event
    aliases TEXT[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ DEFAULT NOW(),
    mention_count INTEGER DEFAULT 1,
    UNIQUE(name, entity_type)
);

CREATE TABLE memory_relations (
    id SERIAL PRIMARY KEY,
    source_entity_id INTEGER REFERENCES memory_entities(id) ON DELETE CASCADE,
    target_entity_id INTEGER REFERENCES memory_entities(id) ON DELETE CASCADE,
    relation_type VARCHAR(100) NOT NULL,
    metadata JSONB DEFAULT '{}',
    fact_id INTEGER REFERENCES memory_facts(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    confidence FLOAT DEFAULT 1.0,
    UNIQUE(source_entity_id, target_entity_id, relation_type)
);
```

---

## File Locations

```
/home/philip/robothor/brain/memory_system/
├── rag.py                 # Core RAG (3-tier memory, vector search)
├── llm_client.py          # Provider abstraction: Qwen3-Next via Ollama
├── reranker.py            # Qwen3-Reranker cross-encoder (yes/no)
├── orchestrator.py        # FastAPI pipeline (port 9099) + /ingest endpoint
├── rag_query.py           # RAG query engine
├── web_search.py          # SearXNG integration
├── fact_extraction.py     # LLM-based fact extraction + storage
├── conflict_resolution.py # Dedup, contradiction, update detection
├── entity_graph.py        # Entity + relationship graph
├── ingestion.py           # Cross-channel ingestion pipeline
├── lifecycle.py           # Decay, importance, consolidation, intra-day, insights
├── mcp_server.py          # MCP server (stdio transport)
├── vision_service.py      # Background vision detection loop (systemd)
├── memory_service.py      # Audit logging + vector memory CLI
├── continuous_ingest.py   # Tier 1: incremental deduped ingestion (*/10 min)
├── periodic_analysis.py   # Tier 2: meeting prep, blocks, entities, contact reconciliation (4x daily)
├── intelligence_pipeline.py # Tier 3: relationships, enrichment, engagement, patterns (daily)
├── ingest_state.py        # Shared dedup module (content_hash, watermarks)
├── crm_fetcher.py         # CRM data fetching via direct SQL (crm_dal)
├── contact_matching.py    # Pure-Python name matching (normalize, similarity, find_best_match)
├── weekly_review.py       # Sunday deep synthesis report
├── conftest.py            # Shared test fixtures (test_prefix, db_conn, cleanup)
├── pytest.ini             # Test configuration
├── test_continuous_ingest.py     # 18 tests
├── test_periodic_analysis.py     # 6 tests
├── test_intelligence_pipeline.py # 8 tests
├── test_contact_matching.py      # 28 unit tests (pure functions)
├── test_contact_reconciliation.py # 4 integration tests
├── test_phase1_fact_extraction.py
├── test_phase2_conflict_resolution.py
├── test_phase3_mcp_server.py
├── test_phase4_entity_graph.py
├── test_phase5_ingestion.py
├── test_phase6_lifecycle.py
├── test_stack.py          # Original integration test suite
├── start_rag.sh           # Stack start/stop/status
├── venv/                  # Python virtual environment
└── MEMORY_SYSTEM.md       # This documentation
```

---

## Intelligence Pipeline

Three-tier architecture for automated intelligence, all running via system crontab with local models:

| Tier | Script | Schedule | Purpose | Duration |
|------|--------|----------|---------|----------|
| 1 | `continuous_ingest.py` | */10 min | Incremental deduped ingestion from all sources | 0-3 min |
| 2 | `periodic_analysis.py` | 4x daily (7,11,15,19) | Meeting prep, memory blocks, entities, contact reconciliation | 3-8 min |
| 3 | `intelligence_pipeline.py` | Daily 3:30 AM | Relationships, enrichment, engagement, patterns, quality | ~23 min |

**periodic_analysis.py phases:** Meeting prep → memory blocks → entity enrichment → contact reconciliation (Phase 4: fuzzy-match memory_entities ↔ contact_identifiers, discover new CRM contacts)

**intelligence_pipeline.py phases:** Catch-up → relationships → contact enrichment (Phase 2.5: fill CRM fields from evidence) → engagement → patterns → quality → cleanup

**Supporting crons:**
| Job | Schedule | Purpose |
|-----|----------|---------|
| Memory Maintenance | 3:00 AM daily | TTL expiry, archival, lifecycle scoring |
| CRM Consistency | 3:15 AM daily | Cross-system contact/entity checks |
| Data Archival | Sunday 4:00 AM | Archive old data |
| Weekly Review | Sunday 5:00 AM | Deep synthesis report |

**Dedup:** `ingested_items` table tracks (source, item_id, content_hash). Same-hash items are skipped.
**Locking:** `fcntl.flock()` prevents concurrent Tier 1 runs. Tier 1 skips when Tier 3 is active.

---

## CLI Usage

```bash
cd /home/philip/robothor/brain/memory_system
source venv/bin/activate

# Store a memory (raw, short-term)
python3 rag.py store "content here" conversation

# Search memories
python3 rag.py search "query here"

# View stats
python3 rag.py stats

# Run maintenance manually (includes lifecycle)
python3 rag.py maintain

# Search facts
python3 -c "from fact_extraction import search_facts; import asyncio; print(asyncio.run(search_facts('query')))"

# Run tests
pytest -v -m "not slow"           # fast unit tests (mocked LLM)
pytest -v -m slow                 # integration tests (real LLM)
pytest -v                         # all tests
```

---

## API Endpoints (port 9099)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Component health check |
| POST | `/query` | Simple RAG query |
| POST | `/v1/chat/completions` | OpenAI-compatible chat |
| GET | `/v1/models` | List models |
| GET | `/profiles` | List RAG profiles |
| GET | `/stats` | Memory statistics |
| POST | `/ingest` | Cross-channel content ingestion |
| POST | `/vision/look` | Capture snapshot → VLM analysis |
| POST | `/vision/detect` | YOLO object detection |
| POST | `/vision/identify` | Face identification |
| GET | `/vision/status` | Vision service state |
| POST | `/vision/enroll` | Face enrollment |

### /ingest Request
```json
{
  "content": "Philip decided to use Qwen3 for the memory system",
  "source_channel": "api",
  "content_type": "conversation",
  "metadata": {"key": "value"}
}
```

### /ingest — Vision Event Example
```json
{
  "content": "Philip detected in living room",
  "source_channel": "camera",
  "content_type": "event",
  "metadata": {
    "detection_type": "person",
    "identity": "Philip",
    "known": true,
    "snapshot_path": "memory/snapshots/2026-02-07/143201.jpg",
    "camera_id": "living-room"
  }
}
```

---

## MCP Server

The MCP server provides a standard interface for external models to access the memory system. Currently built and tested; connect via `.claude.json` when ready.

**Tools:**
- `search_memory` — Semantic fact search (requires: `query`)
- `store_memory` — Ingest + extract facts (requires: `content`, `content_type`)
- `get_stats` — Memory system statistics
- `get_entity` — Entity graph lookup (requires: `name`)
- `memory_block_read`, `memory_block_write`, `memory_block_list` — Structured working memory blocks
- `log_interaction` — CRM interaction logging
- `look`, `who_is_here`, `enroll_face`, `set_vision_mode` — Vision system

**Config (in .claude.json):**
```json
"robothor-memory": {
  "type": "stdio",
  "command": "/home/philip/robothor/brain/memory_system/venv/bin/python",
  "args": ["/home/philip/robothor/brain/memory_system/mcp_server.py"]
}
```

---

## RAG Profiles

| Profile | Memory | Web | Rerank Top-K | Temp | Use Case |
|---------|--------|-----|-------------|------|----------|
| fast | 5 | 3 | 5 | 0.6 | Quick answers |
| general | 15 | 5 | 10 | 0.7 | Balanced |
| research | 30 | 15 | 15 | 0.5 | Deep retrieval |
| expert | 25 | 50 | 25 | 0.45 | Expert analysis |
| heavy | 30 | 100 | 30 | 0.5 | Maximum retrieval |
| code | 15 | 10 | 10 | 0.6 | Code-focused |

## Performance

| Operation | Time | Notes |
|-----------|------|-------|
| Embedding | <1s | qwen3-embedding:0.6b (1024-dim) |
| Vector Search | <1s | pgvector cosine similarity |
| Reranking | ~2-5s | Qwen3-Reranker batch (depends on result count) |
| Generation | 5-30s | qwen3-next (~45 tok/s) |
| Full RAG Pipeline | 10-40s | End-to-end with retrieval + generation |
| Fact Extraction | ~2 min | Thinking model overhead (~17K chars thinking → ~800 chars content) |
| Entity Extraction | ~3 min | Thinking model overhead (~24K chars thinking → ~1K chars content) |
| Full Ingestion | ~4-5 min | extract_facts + conflict_resolution + entity_extraction |

---

## Provider Abstraction

All LLM calls go through `llm_client.py`. To swap providers, change only that file. Key functions:

| Function | Purpose |
|----------|---------|
| `generate()` | Text generation (async) |
| `chat()` | Chat completion (async) |
| `get_embedding_async()` | 1024-dim embedding (async) |
| `detect_generation_model()` | Auto-detect available model |

### Structured Output via Format Schemas

For JSON extraction tasks, pass a JSON schema via the `format` parameter. Ollama constrains generation at the token level to match the schema, producing guaranteed-valid JSON. Combined with `think=True` (default), the model's reasoning goes in a separate `thinking` field and content is clean schema-conforming JSON.

```python
# Example: fact extraction with format schema
raw = await llm_client.generate(
    prompt="Extract facts from...",
    system="Extract facts as JSON array.",
    max_tokens=1024,
    format=FACT_EXTRACTION_SCHEMA,  # Ollama constrains output to match
)
parsed = json.loads(raw)  # Always valid JSON
```

**Thinking overhead:** Qwen3-Next is a thinking model. It generates reasoning tokens before content. `llm_client.py` adds 8192 tokens of overhead to `num_predict` so thinking doesn't eat the content budget. If the model returns empty content with `done_reason: length`, increase `thinking_overhead` in `chat()`.

**Retries:** `extract_facts()` retries up to 3 times on empty results, since thinking length varies between runs.

---

## Networking

| Endpoint | Address | Purpose |
|----------|---------|---------|
| Orchestrator | 0.0.0.0:9099 | RAG pipeline API |
| Tailscale | 100.91.221.100:9099 | Private access |
| Cloudflare | robothor.ai | Public tunnel |
| Ollama | 127.0.0.1:11434 | LLM inference (localhost only) |
| SearXNG | localhost:8888 | Web search |
| PostgreSQL | /var/run/postgresql:5432 | Database |

## Memory Budget (128GB unified)

| Component | Size | Notes |
|-----------|------|-------|
| qwen3-next (Q4_K_M) | ~50 GB | Primary generator |
| qwen3-embedding:0.6b | ~0.6 GB | Embeddings |
| Qwen3-Reranker-0.6B:F16 | ~1.2 GB | Cross-encoder |
| KV cache (qwen3-next) | ~8-16 GB | Context dependent |
| System + services | ~6 GB | OS, PostgreSQL, SearXNG |
| YOLOv8-nano | ~6 MB | Object detection (vision service) |
| InsightFace buffalo_l | ~300 MB | Face recognition (vision service) |
| **Total** | **~72-80 GB** | **48-56 GB headroom** |

## Status: OPERATIONAL

System v3.0 with fact extraction, entity graph, conflict resolution, cross-channel ingestion, lifecycle management, MCP interface, and three-tier intelligence pipeline. Contact enrichment pipeline links memory entities to CRM and fills CRM fields automatically. 2026-02-15.

### Known Issues / Future Work
- **Thinking time variance** — Qwen3-Next's thinking length varies widely (2K-25K chars). Retries mitigate but don't eliminate occasional empty results.
- **`think: false` broken on Qwen3** — Ollama bug. Do not use `think=False` with Qwen3 models; reasoning leaks into content field. Use `think=True` + `format` schema instead.
- **Local LLM enrichment quality** — Llama 3.2 11B extractions for contact enrichment are noisy (sometimes returns "null" as string). Confidence threshold helps but isn't perfect.
