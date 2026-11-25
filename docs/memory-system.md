# Memory System

The memory system is Genus OS's core — a multi-store architecture where facts are extracted, deduplicated, scored, clustered into episodes, abstracted into procedures, and organized into a knowledge graph. Memory decays, strengthens, gets superseded, consolidates, and — when agents act on it unsuccessfully — self-invalidates.

## Stores

| Store | Purpose |
|-------|---------|
| `memory_facts` | Atomic distilled facts with lifecycle scoring (importance, decay, outcome_failures) |
| `memory_entities` | Knowledge graph nodes (person, project, tech, …) |
| `memory_relations` | Knowledge graph edges (uses, works_at, manages, …) |
| `memory_mentions` | Which entity each fact mentions, by id (see [Graph schema](#graph-schema)) |
| `memory_relation_evidence` | Every fact that supports an edge |
| `memory_resolution_decisions` | Each decision that a name in a fact refers to an entity |
| `memory_insights` | LLM-discovered cross-domain connections |
| `memory_episodes` | Time-bucketed event clusters (added 2026-04) |
| `memory_procedures` | Skill library — named playbooks with success/failure tracking (added 2026-04) |
| `memory_vault` | Verbatim Knowledge Vault — exact reference data, never paraphrased (added 2026-05, RIP 12) |
| `vault_access_log` | Audit trail for every Knowledge Vault value read (added 2026-05) |
| `memory_intents` | Prospective/intent memory — standing objectives the operator works toward (added 2026-05, RIP 14) |
| `agent_memory_blocks` | Named text blocks (persona, user_profile, preferences, self_model, …) |
| `agent_breadcrumbs` | 7-day cross-run scratchpad per agent (added 2026-04) |
| `chat_messages` | Verbatim conversation turns with embeddings + 90-day TTL (added 2026-04) |
| `fact_access_log` | Per-run retrieval audit for outcome attribution (added 2026-04) |
| `ingested_items` | Dedup tracking (content hash per source+item+tenant) |
| `ingestion_watermarks` | Per-source progress and error tracking |

All embedding columns are `vector(1024)` with HNSW indexes (m=16, ef_construction=200).
Active-row **partial** HNSW indexes (`idx_*_active`, migrations 073/074) keep
superseded vectors out of the candidate budget. pgvector **0.8.2** is installed;
every vector search applies shared session tuning via
`robothor/memory/vector_tuning.py` `apply_hnsw_session(cur)` —
`hnsw.ef_search=100` plus, when `MEMORY_HNSW_ITERATIVE=1`,
`hnsw.iterative_scan=relaxed_order` (fetches past the `WHERE` filter until `LIMIT`
is met — the robust fix for filtered-vector recall).

## Retrieval

`search_facts(query, …)` implements hybrid retrieval:

1. Vector search (HNSW cosine, top 30)
2. BM25 keyword search (GIN tsvector, top 30)
3. **Reciprocal Rank Fusion** (k=60)
4. Optional entity-graph expansion (follow relations)
5. Optional **cross-encoder reranker** (Qwen3-Reranker-0.6B) — on by default; kill-switch via `MEMORY_RERANK_ENABLED=0`
6. Optional appended `memory_insights`, `memory_episodes`, and verbatim `chat_messages` (low-weight RRF merge)

### Memory router (RIP 15)

The `search_memory` tool previously hard-coded `expand_entities + insights +
episodes = True` on *every* call — a full fan-out for even a trivial lookup.
When `ROBOTHOR_RIP_15_ENABLED` is set, it routes through
`robothor/memory/router.py`, which classifies the query (heuristics) and queries
only the stores that fit:

| Class | Stores | Notes |
|-------|--------|-------|
| `exact_lookup` | Knowledge Vault captions + facts | numbers, ids, addresses |
| `temporal` | facts + episodes | results recency-reordered (latest first) |
| `how_to` | procedures + facts | |
| `who_is` | facts + entity-graph expansion | |
| `intent` | standing intents + facts | |
| `default` | facts + insights | no episode/entity fan-out |

Results from each store are fused with the shared `rrf_fuse` (k=60, in
`robothor/memory/fusion.py`) and budget-capped. `search_facts()` keeps its
signature for back-compat; only the tool path changes.

## Symbolic short-term memory (RIP 13)

Long tool-heavy runs blow the context budget. Beyond the existing offload
(`session._offload_tool_result` writes large tool outputs to a tempfile),
`robothor/engine/symbolic_memory.py` encodes the run's tool activity as a compact
**Mermaid task-state graph** keyed by `node_id`. The agent reasons over the graph
and calls `recall_node(node_id)` to read the byte-exact output of any step on
demand (the TencentDB-Agent-Memory technique).

Modes (`feature_flags.symbolic_memory_mode`, env `ROBOTHOR_RIP_13_MODE`):

- `observe` (default when `ROBOTHOR_RIP_13_ENABLED=1`) — build the per-run graph
  and log the would-be token savings; injected context is **unchanged** (safe).
- `enforce` — the runner injects `render_injection_block()` in place of raw tool
  tails. (Wired once the runner refactor lands; observe-mode is the current rollout.)

Ratio knobs (ported from the source project): `ROBOTHOR_MEMORY_OFFLOAD_MILD_RATIO`
(0.5), `ROBOTHOR_MEMORY_OFFLOAD_AGGRESSIVE_RATIO` (0.85),
`ROBOTHOR_MEMORY_MMD_MAX_TOKEN_RATIO` (0.2). Tool: `recall_node` (readonly).

## Lifecycle

Nightly in `robothor/memory/lifecycle.py::run_lifecycle_maintenance`:

1. Importance scoring (LLM-judged, 200/run, 600s budget) of facts not yet marked scored
2. Decay computation (recency of use × access × reinforcement × importance × **outcome penalty**)
3. Garbage pruning (low decay + low importance + never recalled)
4. Consolidation (similar facts merged, superseded via `is_active=FALSE`)
5. Unconsolidated sweep
6. Cross-domain insight discovery (72h window)
7. Cross-entity relationship inference
8. **Episode building** — cluster recent facts by time + entity overlap, LLM-title+summarize, embed
9. **Preference tracking** — extract from high-importance facts, detect drift (stale flag)
10. **Chat TTL** — delete 90d+ un-pinned, un-referenced verbatim turns
11. **Breadcrumb pruning + promotion** — hot breadcrumbs promoted to `memory_facts`
12. **Access log GC** — trim attribution history past 30d
13. **Intent inference** — propose standing intents from recent facts (when `MEMORY_INTENT_POPULATE` is on)

Every step runs once per active tenant (`crm_tenants.active`), inside `tenant_scope(tenant)`, and reads and writes only that tenant's rows. A consolidated fact never merges two tenants' facts, an insight never cites another tenant's facts and is deduplicated only against its own tenant's insights, and an inferred relation never joins two tenants' entities. Each derived row is written to the tenant of its sources. With `ROBOTHOR_RLS_ENABLED` on, the scope binds each step's connections to that tenant, so child tenants are maintained as well as the process's own. Step 1's 600s scoring budget covers the whole pass and is charged only for time spent scoring: each tenant may use an equal share of what is still unspent, so a tenant that needs less leaves more for the rest and no tenant is left unscored because an earlier tenant's other steps were slow. A tenant whose steps fail is logged and skipped; the others are still maintained. autoDream's lighter idle pass works the same way: intra-day consolidation, pruning, insight discovery (whose insights it now stores), the `working_context` refresh and preference mining each run once per tenant, and a tenant that fails is named in the run's recorded error while the pass carries on for the rest.

Knowledge-gap analysis (`robothor/memory/gap_analysis.py`) reads one tenant: the calling run's tenant for the `get_knowledge_gaps` engine tool, `DEFAULT_TENANT` when no tenant is given (the MCP `get_knowledge_gaps` handler and the curiosity-density check currently pass none).

## Outcome-Driven Invalidation

Every `search_memory` call logs fact IDs to `fact_access_log` keyed by `run_id`. When a run fails, `delivery.py` calls `bump_failure_for_run(run_id)` which increments `outcome_failures` on every fact the agent consulted. The decay scorer subtracts a per-failure penalty (capped at 0.4). Three or more failures also drop confidence. Self-correcting memory without dogmatic deletion.

## Generation Provider

All memory *generation* work (fact extraction, episode summaries, insight discovery, conflict classification, intent inference, preference distillation, consolidation) dispatches through one seam: `robothor.memory.generation`. It defaults to local Ollama, but `ROBOTHOR_MEMORY_GENERATION_PROVIDER=openrouter` offloads it to a remote model (`ROBOTHOR_MEMORY_GENERATION_REMOTE_MODEL`, default `openrouter/xiaomi/mimo-v2.5`) — useful when local generation saturates the GPU. Embeddings and reranking always stay local. Remote calls are paced (minimum interval between calls, `ROBOTHOR_MEMORY_GENERATION_MIN_INTERVAL_S`, default 1.5s) and rate-limit responses (429/503) are retried with jittered exponential backoff before giving up. Remote failures then fall back to local Ollama loudly: a WARNING containing `MEMORY_GENERATION_REMOTE_FALLBACK` plus the `generation.remote_fallback_count` counter — the marker fires only after retries are exhausted. See `docs/configuration.md` for the variables.

Degradation escalates off the log and onto the alert path (`robothor.engine.alerts.alert()`), because a log line nobody reads is not a signal:

- Five consecutive remote→local fallbacks (`FALLBACK_STREAK_THRESHOLD`) raise a **warning** alert — remote is effectively down and the whole load is back on the local GPU. Warning level means the digest/briefing, not a page.
- A local failure with no remote leg left raises a **critical** alert ("Memory generation is down") — every caller swallows generation exceptions (`extract_facts` logs `failed after N attempts` and returns `[]`), so this seam is the last place that can still tell the operator that memory writes are being dropped.

Both alerts are latched: one alert per condition per hour (`ALERT_RELATCH_SECONDS`), re-armed immediately by a success on the corresponding leg, so a 429 storm produces one alert instead of thousands.

Token budgets are per caller, not global: fact extraction asks for `EXTRACTION_MAX_TOKENS`, while the small classification calls (importance judging, preference distillation, consolidation) ask for 64-256. A truncated remote response (`finish_reason=length`) therefore names the budget and the caller that actually ran out — the message used to quote the extraction budget whatever had failed, which sent every investigation to the wrong file. When the generation was re-tasked (`asyncio.wait_for` does this on Python 3.11) the caller cannot be identified from the stack, and the line says "a caller" rather than naming the wrong one.

Because remote reasoning tokens are charged against `max_tokens`, `think=True` calls add `REMOTE_THINKING_OVERHEAD` and `think=False` calls add the smaller `REMOTE_NOTHINK_MARGIN`: a reasoning model that does not honour `reasoning: {enabled: false}` must not be able to spend a 64-token caller's whole budget before the answer starts. That margin is bought on the wire and is **not** enforced on the answer — a model that emits no inline `<think>` block can spend it on content, so a reply may run up to `max_tokens + REMOTE_NOTHINK_MARGIN`. It is not sliced back, because a body cut mid-answer is not an answer (the same reason `finish_reason=length` is refused rather than parsed); instead an over-budget reply is logged with `MEMORY_GENERATION_ANSWER_OVER_BUDGET`, naming the caller and its budget, and handed on whole.

## Fact Store

Facts are atomic statements extracted from content via LLM. Each has:

- `fact_text` -- the statement itself
- `category` -- one of: `personal`, `project`, `decision`, `preference`, `event`, `contact`, `technical`
- `entities` -- array of named entities mentioned (text[])
- `confidence` -- 0.0 to 1.0
- `embedding` -- 1024-dim vector for semantic search
- `is_active` -- lifecycle flag (FALSE when superseded)
- `superseded_by` -- FK to the replacing fact

```python
from robothor.memory.facts import extract_facts, store_fact, search_facts

# Extract facts from raw text (LLM-powered)
facts = await extract_facts("Alice decided to use Redis for caching. Bob disagreed.")
# Returns: [
#   {"fact_text": "Alice decided to use Redis for caching", "category": "decision",
#    "entities": ["Alice", "Redis"], "confidence": 0.9},
#   {"fact_text": "Bob disagreed with Alice's caching decision", "category": "decision",
#    "entities": ["Bob", "Alice"], "confidence": 0.85},
# ]

# Store with embedding
fact_id = await store_fact(facts[0], source_content="...", source_type="email")

# Semantic search
results = await search_facts("what caching solution was chosen?", limit=5)
```

## Conflict Resolution

When a new fact arrives, the pipeline checks for similar existing facts:

1. **find_similar_facts** -- pgvector cosine search, threshold 0.7
2. **classify_relationship** -- LLM classifies as `new`, `duplicate`, `update`, or `contradiction`
3. **Act** -- store (new), skip (duplicate), or supersede (update/contradiction)

An operator-confirmed match (`metadata.operator_confirmed`, set by an edit in
the Helm) is never superseded: the new fact is stored superseded by it and the
result's `action` is `kept_confirmed`. Prune, consolidation, dechurn and resolution capture skip
confirmed facts too. `update_fact` re-embeds a changed text, sets
`metadata.embedding_stale` if the embedding fails, and merges `metadata` rather
than replacing it.

```python
from robothor.memory.conflicts import resolve_and_store

result = await resolve_and_store(
    fact={"fact_text": "The team switched from Redis to Memcached", ...},
    source_content="...",
    source_type="email",
)
# result["action"] is one of: "stored", "skipped", "superseded", "reinforced", "kept_confirmed"
# If superseded: result["old_id"] points to the fact that was deactivated
```

## Lifecycle

Every fact has lifecycle columns that drive autonomous maintenance:

| Column | Purpose |
|--------|---------|
| `access_count` | Incremented each time recall returns the fact to an agent |
| `last_accessed` | Set to now each time recall returns the fact (insert time until then) |
| `importance_score` | LLM-judged (0.0-1.0); 0.5 until judged |
| `decay_score` | Computed: recency + access + reinforcement + importance |
| `reinforcement_count` | Incremented when fact is confirmed by new evidence |

**Decay formula:**

```
recency = exp(-hours_since_access * ln(2) / 72)    # 72h half-life
access_boost = min(ln(1 + access_count) / 5, 0.3)
reinforcement_boost = min(ln(1 + reinforcement_count) / 5, 0.2)
importance_floor = importance_score * 0.4
score = max(importance_floor, recency) + access_boost + reinforcement_boost
```

A fact with high importance can never fully decay (importance_floor). Frequently accessed facts resist decay via access_boost.

**What counts as use.** A fact is used when recall returns it to an agent: the
`search_memory` tool (every fact row it returns) and warm-up entity recall
(every fact it injects into the preamble, interactive and goal recall alike).
Each raises `access_count` by one and sets `last_accessed` to now, so recency
is time since last use, not age. `search_facts` itself never writes; the two
call sites do (`robothor.memory.outcomes.record_fact_access`, by fact id, or
for warm-up's v2 recall, which returns no id, by the fact's content hash), and a caller
that must have no side effects passes `record_access=False` to
`_search_memory` or `_build_entity_context`. The eval harness calls
`search_facts` directly and records nothing. Benchmark runs record nothing.

The write never slows or fails recall. The search tool hands it to a tracked
background task that runs it in a worker thread; warm-up hands it to its
deferred writer. A failed write is logged and the results are returned as
they were.

**History from before recall was recorded.** Migration 094's
`access_count_shadow` and `last_accessed_shadow` hold the use history
`scripts/memory_decay_backfill.py` recovers from `fact_access_log`. Decay and
prune read the greater of each live column and its shadow, so that history
counts without double-counting a search hit that is both logged and recorded.
Retrieval ranking reads only the live `access_count`.

**Prune.** A fact is retired when its decay score is below 0.1, its
importance below 0.3, and it has never been recalled (both use counts zero),
outside the protected categories and operator-confirmed facts. One recall
gives an access boost of ln 2 / 5 ≈ 0.14, which keeps a used fact above the
decay threshold.

**Importance scoring.** The judge scores only facts whose importance nobody
has judged or set: no `metadata.importance_scored_at` marker, and a score
still exactly what reinforcement alone gives from the 0.5 default
(`LEAST(0.5 + 0.05 × reinforcement_count, 1.0)`). A score set any other way —
an earlier verdict, a resolution's 0.85, the importance a consolidated fact
inherits — is never overwritten. The judge's score is stored with the
reinforcement the fact has earned added on top (same 1.0 cap), and the fact is
marked. A neutral 0.5 on a fact never reinforced is not marked, because the
judge also returns 0.5 on a timeout; rewriting it is a no-op, and it is
retried. A reinforced fact is marked whatever the verdict. Unjudged events
older than 30 days are scored 0.3, plus their reinforcement, without the
judge, and marked.

```python
from robothor.memory.lifecycle import compute_decay_score, run_lifecycle_maintenance

# Manual decay computation
score = compute_decay_score(
    last_accessed=some_datetime,
    access_count=5,
    reinforcement_count=2,
    importance_score=0.8,
)

# Run full maintenance (score importance, update decay)
stats = await run_lifecycle_maintenance()
# {"facts_scored": 12, "decay_updated": 350}
```

## Consolidation

Similar facts (cosine similarity >= 0.8) are grouped and merged into a single summary fact. The originals are deactivated.

```python
from robothor.memory.lifecycle import find_consolidation_candidates, consolidate_facts

groups = await find_consolidation_candidates(min_group_size=3, similarity_threshold=0.8)
for group in groups:
    result = await consolidate_facts(group)
    print(f"Merged {len(result['source_ids'])} facts into: {result['consolidated_text']}")
```

## Three-Tier Operations

```python
from robothor.memory.tiers import store_short_term, search_all_memory, run_maintenance

# Short-term (48h TTL)
mid = store_short_term("Meeting notes from standup...", content_type="conversation")

# Search across tiers (results sorted by similarity, tagged with tier)
results = search_all_memory("standup decisions", limit=10)

# Maintenance: archive expiring short-term, prune expired, run lifecycle
stats = run_maintenance()
# {"archived": 3, "deleted": 15, "lifecycle": {"facts_scored": 5, "decay_updated": 200}}
```

## Knowledge Vault (verbatim store, RIP 12)

`memory_facts` is LLM-extracted, so values are paraphrased — fine for "Alice
prefers Redis," unsafe for an account number or a credential. The Knowledge
Vault (`robothor/memory/vault.py`) preserves values **byte-for-byte**. It is
**not** the secrets vault (`robothor.vault` / `vault_secrets`); it is a
searchable, tenant-scoped memory store.

Safety invariants:

- Only the **caption** is embedded — the value is never vectorized.
- `memory_vault_search` returns captions + ids only, **never a value**; reading
  a value requires `memory_vault_get`, which writes a `vault_access_log` row.
- `high` sensitivity values are encrypted at rest (AES-256-GCM via the shared
  `robothor.vault.crypto` master key); `low`/`medium` keep `value_exact`
  plaintext. A DB `CHECK` enforces exactly one of `value_exact` / `value_enc`.

```python
from robothor.memory import vault

# Store exact reference data (high → encrypted at rest)
await vault.store_vault_entry("FakeVendorCo support line", "555-0142 ext 7",
                              entry_type="contact_info", sensitivity="low")
# Find by description (no value returned), then read the exact value (audited)
hits = await vault.search_vault("vendor phone number")
vault.get_vault_value(hits[0]["id"])  # {"value": "555-0142 ext 7", ...}
```

Tools: `memory_vault_store`, `memory_vault_search` (readonly), `memory_vault_get`.
Gated by `ROBOTHOR_RIP_12_ENABLED` — tools stay dark and the table inert until
the operator opts in (restart the engine after toggling). Global kill switch:
`ROBOTHOR_DISABLE_ALL_RIPS=1`.

## Intent Memory (prospective, RIP 14)

Everything above is *retrospective*. `memory_intents` (`robothor/memory/intents.py`)
models what the operator is *working toward* and persists it across sessions, so
the main agent's heartbeat can advance standing objectives instead of only
reacting. It's parallel to `session_goal` (per-run, evidence-gated); intents are
longer-lived business objectives.

Confirmation model:

- `stated` intents (the operator/agent declared them) are `active` immediately.
- `inferred` intents (proposed by the nightly `infer_intents_from_facts` LLM pass)
  start as `proposed` and only become `active` via `confirm_intent(id, token)` with
  a valid HMAC token (`ROBOTHOR_INTENT_HMAC_SECRET`) — the agent never
  auto-activates a goal it invented.

The top active intents are injected into the warmup preamble (`active_intents`
section). When a `session_goal` linked to an intent completes, the intent's
`last_advanced_at` is bumped (loop closure); intents idle > 30 days go `dormant`.

Tools: `intent_add`, `intent_search` / `intent_list` (readonly), `intent_advance`.
Gated by `ROBOTHOR_RIP_14_ENABLED`.

## Knowledge Graph

Entities are upserted (mention_count increments on conflict). Relations link entities with typed edges.

```python
from robothor.memory.entities import (
    upsert_entity, add_relation, get_entity, get_all_about,
    extract_and_store_entities,
)

# Manual entity creation
alice_id = await upsert_entity("Alice", "person")
acme_id = await upsert_entity("Acme Corp", "organization")
await add_relation(alice_id, acme_id, "works_at", confidence=0.9)

# Get everything known about an entity
info = await get_all_about("Alice")
# {"entity": {..., "mention_count": 5}, "facts": [...], "relations": [...]}

# Auto-extract from text
stats = await extract_and_store_entities("Alice at Acme uses FastAPI", fact_id=42)
# {"entities_stored": 3, "relations_stored": 2}  # counts what was STORED
```

### Junk names and batch safety

`upsert_entity` returns `int | None`. It returns `None` — never a numeric
sentinel — for a name that cannot be a real entity: a bare UUID (the extractor
occasionally echoes a row id back as a name) or fewer than two characters after
stripping. Callers must test `is not None`, not truthiness: entity id `0` is a
valid id, and a truthy sentinel like `-1` would pass an `if entity_id:` guard
and then be rejected by the `memory_relations` foreign key.

`add_relations_batch` is hardened to match. Rows whose endpoints are not valid
entity ids are dropped before the insert, and a chunk Postgres still rejects is
retried row by row inside a savepoint, so one bad row costs one relation instead
of the whole batch. Every drop is logged at WARNING with a count.

### Graph schema

The memory graph foundation migration (`crm/migrations/*_memory_graph_foundation.sql`) adds the schema that links facts to
entities by identity rather than by name, and gives edges time, evidence and
trust. It is schema only: nothing writes the new tables or columns yet, and
code written before it reads and writes exactly as before. Every new table has
a `tenant_id` referencing `crm_tenants`, the forced `tenant_isolation`
row-level-security policy, and stays with its tenant when a workload moves
(`genus tenant move-workload`).

**`memory_resolution_decisions`** records each decision that a surface form (a
name as it appeared in a fact) refers to an entity: the surface form, the
entity type if known, the entity it resolved to (`resolved_entity_id`, NULL
when it was deliberately left unresolved), the `method`, a `confidence`
between 0 and 1, the `evidence` the decision rested on (JSON), and
`decided_at`. Merging two entities never rewrites ids: a decision found wrong
is reversed by setting `reversed_at`, and points at the decision that replaced
it through `superseded_by`. Methods: `strong_key` (an email, phone or other
unique identifier), `full_name`, `context`, `llm`, `exact`, `operator` and
`backfill`.

**`memory_mentions`** is the fact-to-entity join: one row per mention, with
the fact, the entity (NULL while unresolved, or once its entity is deleted),
the surface form and type, and the decision that resolved it. Deleting a fact
deletes its mentions.

**`memory_relation_evidence`** lists every fact supporting an edge, keyed by
`(relation_id, fact_id)`. The migration copies each edge's existing
`memory_relations.fact_id` into it; edges written afterwards by code that
predates the graph write path still set only `fact_id`.

**`memory_relations`** gains:

| Column | Meaning |
|--------|---------|
| `predicate` | The canonical relation; `relation_type` keeps the phrase as extracted |
| `valid_from`, `valid_to` | When the relation held in the world (event time) |
| `recorded_at`, `expired_at` | When the system started and stopped believing it. `recorded_at` is NULL on edges from before the migration; read it as `created_at` |
| `is_active` | `TRUE` until the edge is retired (defaults `TRUE`) |
| `retired_reason` | `ended` (it held, then stopped), `refuted` (it was wrong) or `evidence_merged` (its evidence moved to another edge) |
| `origin` | Who asserted it (below) |
| `weight` | Learned usefulness for retrieval, `1.0` by default |

Active edges are indexed per endpoint for neighbourhood expansion.

**`memory_facts`** gains `origin`, `event_time` (when the thing happened, as
opposed to `valid_from`, when the fact became true as recorded) and a scope.
None of them is part of the content hash, so no stored hash changes.

**`memory_entities`** gains the CRM record it is about (`crm_person_id`,
`crm_company_id`, cleared if that record is deleted), an `embedding`, a scope,
and an index on `(tenant_id, lower(name))`. When `genus tenant move-account`
moves an account's CRM contact to another tenant, a memory entity linked to
that contact stays with its tenant's memory and its `crm_person_id` is cleared
(the report lists it under `links_cleared`); the link does not refuse the move.

**Scope** (`scope_kind`, `scope_id` on facts and entities): `shared` (the whole
tenant, the default, no `scope_id`), `user` or `agent` (private to the owner
named in `scope_id`, which is then required).

**Origin and authority.** Origin records who asserted a fact or an edge. When
claims disagree, or recall is filtered, they rank in this order, most trusted
first:

1. `operator_confirmed` — the operator confirmed it
2. `user_stated` — a user said it
3. `crm` — it comes from the CRM record
4. `tool` — a tool returned it
5. `agent_generated` — an agent produced it
6. `external` — an outside source (a web page, an inbound message)
7. `inferred` — the system derived it (edges only)

A row from before origins existed has no origin and ranks as
`agent_generated`. Only the first three are trusted for automatic recall. The
lists, the order and the helpers that rank by them (`authority_rank`,
`least_trusted`, `is_trusted_for_auto_recall`) live in
`robothor/memory/graph_schema.py`; a test keeps them equal to the database's
CHECK constraints.

## Ingestion Pipeline

The full pipeline: extract facts, resolve conflicts, build entity graph, track dedup state.

```python
from robothor.memory.ingestion import ingest_content

result = await ingest_content(
    content="The board approved the Q3 budget. CFO Jane Smith presented the numbers.",
    source_channel="email",
    content_type="decision",
    metadata={"email_id": "msg-123", "from": "cfo@example.com"},
)
```

Valid channels: `discord`, `email`, `cli`, `api`, `telegram`, `gchat`, `voice`, `mcp`, `camera`, `conversation`, `crm`.

## Dedup and Watermarks

Content hashing prevents re-processing the same data:

```python
from robothor.memory.ingest_state import content_hash, is_already_ingested, record_ingested

h = content_hash({"subject": "Q3 Budget", "body": "..."}, keys=["subject", "body"])
if not is_already_ingested("email", "msg-123", h):
    # Process and ingest
    record_ingested("email", "msg-123", h, fact_ids=[1, 2, 3])
```

## Memory Blocks

Named text blocks for structured agent working memory. Predefined blocks: `persona`, `user_profile`, `working_context`, `operational_findings`, `contacts_summary`. Each has a `max_chars` limit and usage tracking. Accessed via MCP tools (`memory_block_read`, `memory_block_write`) or direct SQL.
