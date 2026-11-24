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
superseded vectors out of the candidate budget. Every vector search applies
shared session tuning via `robothor/memory/vector_tuning.py`
`apply_hnsw_session(cur)`: `hnsw.ef_search=100`, plus an **iterative index
scan** (`hnsw.iterative_scan=strict_order`, `hnsw.max_scan_tuples=20000`)
whenever the installed pgvector is 0.8 or later. The version is read once per
process from `pg_extension`; an older pgvector is never sent the setting, and
`MEMORY_HNSW_ITERATIVE=0` turns it off. Without it, a search under a narrow
tenant or person filter spends the candidate budget on other rows and comes
back with fewer rows than exist.

## Retrieval

`search_facts(query, …)` ranks every source through one path. The contract a
source ("leg") implements is in `robothor/memory/recall.py`.

1. **Legs.** Each returns `Candidate`s, best first, up to `max(30, limit × 3)`:
   - `vector` — HNSW cosine over `memory_facts`. The query is embedded with the
     instruction the embedding model expects on queries
     (`Instruct: …\nQuery:…` for Qwen3-Embedding); stored documents are
     embedded bare, so nothing is re-embedded.
   - `keyword` — PostgreSQL full text (`websearch_to_tsquery`, GIN index on
     `tsv`), scored by `ts_rank_cd` with length normalisation into 0..1. It
     requires every term first and falls back to any term when that matches
     nothing, so a natural-language question still finds the fact that shares
     most of its words.
   - `graph` (on unless `include_graph=False`) — graph activation from the
     entities the query names and the entities the top hits mention (see
     [Associative recall](#associative-recall)). It replaced the one-hop
     `entity_expansion` leg; `expand_entities=True` is still accepted and
     means "graph leg on".
   - `insight`, `episode`, `chat_turn` (`include_insights`,
     `include_episodes`, `include_chat_turns`).
2. **Fusion** — reciprocal rank fusion over every leg, k=60 (`fuse`); each row
   records its rank in every leg that found it (`leg_ranks`).
3. **Reranker** (Qwen3-Reranker-0.6B, local) — on by default, off with
   `MEMORY_RERANK_ENABLED=0`. It scores the first 16 fused candidates
   (`MEMORY_RERANK_MAX_CANDIDATES`) with a graded relevance in 0..1: the
   probability of "yes" against "no", read from the logprobs Ollama returns
   (an Ollama without logprobs gives 1 or 0). Candidates are ordered by that
   score alone, whichever leg found them. On any reranker error the fused
   order stands.
4. **Blend** — `relevance` (the rerank score, or `10 / (10 + fused rank)` when
   there is none — one 0..1 scale for every leg; cosine similarity is not used)
   + recency + importance + access count, with supersession demotion; then
   near-duplicates are dropped.
5. **Reserved places** — a leg named in `RESERVED_PLACES` (`facts.py`) keeps at
   least that many places in the top `limit` whenever it returned anything,
   chosen by its own score: `graph` keeps 2.
   A related-but-not-similar fact is exactly what a query-document reranker
   discards.

Every leg shares `limit`. Returned rows keep their fields and add `kind`,
`leg_ranks`, `leg_scores`, `rrf_score`, `fused_rank`, `rerank_score` and
`relevance`. The search path is read-only.

Flags that still shape this path: `MEMORY_RANK_BLEND=0` skips the blend (fused
or reranked order, cut to `limit`); `MEMORY_DEDUP=0` keeps near-duplicates;
`MEMORY_TEMPORAL_COHERENCE=1` turns on supersession inference among decisions;
`MEMORY_RERANK_WIDE=1` widens the leg fetch to 45 and the reranker's survivor
pool, and gives rows scoring at least 0.5 a small blend bonus.
`MEMORY_EPISODE_MERGE` is no longer read: the auxiliary stores are fused legs on
every path. `MEMORY_NARROW_SEARCH` is read by the `search_memory` tool, which
decides the `include_*` arguments.

### Memory router (RIP 15)

Off unless `ROBOTHOR_RIP_15_ENABLED` is set. When it is, the `search_memory`
tool routes through `robothor/memory/router.py`, which classifies the query
(heuristics) and queries only the stores that fit:

| Class | Stores | Notes |
|-------|--------|-------|
| `exact_lookup` | Knowledge Vault captions + facts | numbers, ids, addresses |
| `temporal` | facts + episodes | results recency-reordered (latest first) |
| `how_to` | procedures + facts | |
| `who_is` | facts (with the graph leg) | |
| `intent` | standing intents + facts | |
| `default` | facts + insights | no episode/entity fan-out |

The facts it gets come from `search_facts` (ranked as above); the stores are
then merged with `rrf_fuse` (k=60, `robothor/memory/fusion.py`) and
budget-capped. Without the flag the tool calls `search_facts` directly.

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

1. Importance scoring (LLM-judged, 200/run, 600s budget)
2. Decay computation (recency × access × reinforcement × importance × **outcome penalty**)
3. Garbage pruning (low decay + low importance + zero accesses)
4. Consolidation (similar facts merged, superseded via `is_active=FALSE`)
5. Unconsolidated sweep
6. Cross-domain insight discovery (72h window)
7. Cross-entity relationship inference
8. **Episode building** — cluster recent facts by time + entity overlap, LLM-title+summarize, embed
9. **Preference tracking** — extract from high-importance facts, detect drift (stale flag)
10. **Chat TTL** — delete 90d+ un-pinned, un-referenced verbatim turns
11. **Breadcrumb pruning + promotion** — hot breadcrumbs promoted to `memory_facts`
12. **Access log GC** — trim attribution history past 30d

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
| `access_count` | Incremented on search hits |
| `last_accessed` | Updated on search hits |
| `importance_score` | LLM-judged (0.0-1.0) |
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

### Associative recall

When an agent works on a person, company or task, what is connected to it
comes to mind without being asked. `robothor/memory/activation.py` links text to
the entities it names and spreads activation from them over the graph; the
walk itself is the database function `memory_activate` (migration
`crm/migrations/*_memory_graph_activation.sql`).

**Seeds.** `seeds_from_text` links text to entities without a model call: exact
full names and aliases, case-insensitively, and e-mail addresses or @handles
matched against `contact_identifiers`. A single token never resolves on its
own, so a first name alone seeds nothing. Warm-up also seeds the sender's own
entity (the entity whose `crm_person_id` is the sender's CRM person);
`search_facts` also seeds the entities its top hits mention (`memory_mentions`).

**The walk.** A bounded, forward-push approximation of Personalized PageRank.
Each seed starts with its weight divided by the square root of one plus its
visible degree. Each hop, every frontier node passes a damped share of its
activation along at most `FANOUT` of its strongest edges (by `weight`, then
recency), split by edge weight over all of its visible, eligible edges. Edges
are followed in both directions. At most `MAX_HOPS` hops (2 by default; the
function never walks more than 3, whatever it is asked). Each hop is one
set-based query over the active-edge indexes. A fact's graph score is the
activation of the entities it mentions; each fact carries its hop distance and
the path of entity names that reached it, so an agent can see why it was
recalled. The constants at the top of `activation.py` are starting values for
the evaluation to tune.

**Visibility.** The walk crosses an edge only when at least one of its
supporting facts is visible to the reader: active, in the tenant, and shared or
scoped to the reader. Only those edges count toward a node's degree and weight,
so a path through evidence the reader cannot see changes no score. The
"own data + shared" person scope applies on top. The facts returned for an
entity obey the same rule. With no known reader, only shared evidence counts.
An edge's evidence is `memory_relation_evidence` together with the older
`memory_relations.fact_id` column.

**Trust.** Automatic recall (warm-up) runs trusted-only: an edge counts only
when a supporting fact is `operator_confirmed`, `user_stated` or `crm`, an
edge whose own origin is `inferred` or `external` is never followed, and only
facts of those origins are returned. A fact with no origin counts as
agent-generated, so it is not trusted. **Every fact written before origins
existed has none, so on existing data trusted-only activation finds nothing and
warm-up uses its name-matching context instead.** A deliberate search
(`search_facts`, the `search_memory` tool) walks every origin and labels each
row with its `origin` and the path that reached it (`origin` and `via` in the
tool's results).

**Hub control.** The operator's own entity (found through `get_owner_person`)
is never a seed and never a transit node. Seeds are weighted by inverse degree,
and fan-out is capped, so a node linked to everything cannot flood the result.

**Tenancy.** `memory_activate` is `SECURITY DEFINER`, like the warm-up's entity
recall function, so the planner can use its indexes under row-level security.
It filters every table by the tenant passed to it: both ends of an edge, the
edge, its evidence, the facts, the mentions. A session scoped to another tenant
gets nothing.

**Warm-up.** The entity context section takes its connected context from
trusted-only activation, with a hard budget (`GRAPH_WARMUP_BUDGET_SECONDS` in
`robothor/engine/warmup.py`, 150 ms to start). On a timeout, an error or an
empty result it falls back to the name-matching section exactly as before. The
section keeps its character cap. There is no model call and no reranker. The
fallback path does not filter by origin.

The search path writes nothing. Rows found only by the graph are not logged as
fact access.

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
