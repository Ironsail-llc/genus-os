# Memory System

The memory system is Genus OS's core — a multi-store architecture where facts are extracted, deduplicated, scored, clustered into episodes, abstracted into procedures, and organized into a knowledge graph. Memory decays, strengthens, gets superseded, consolidates, and — when agents act on it unsuccessfully — self-invalidates.

## Stores

| Store | Purpose |
|-------|---------|
| `memory_facts` | Atomic distilled facts with lifecycle scoring (importance, decay, outcome_failures) |
| `memory_entities` | Knowledge graph nodes (person, project, tech, …) |
| `memory_relations` | Knowledge graph edges (uses, works_at, manages, …) |
| `memory_insights` | LLM-discovered cross-domain connections |
| `memory_episodes` | Time-bucketed event clusters (added 2026-04) |
| `memory_procedures` | Skill library — named playbooks with success/failure tracking (added 2026-04) |
| `memory_vault` | Verbatim Knowledge Vault — exact reference data, never paraphrased (added 2026-05, RIP 12) |
| `vault_access_log` | Audit trail for every Knowledge Vault value read (added 2026-05) |
| `memory_intents` | Prospective/intent memory — standing objectives the operator works toward (added 2026-05, RIP 14) |
| `agent_memory_blocks` | Named text blocks (persona, user_profile, preferences, self_model, …) |
| `agent_breadcrumbs` | 7-day cross-run scratchpad per agent (added 2026-04) |
| `chat_messages` | Verbatim conversation turns with embeddings + 90-day TTL (added 2026-04) |
| `memory_segments` | Raw conversation split into topical segments, vector + keyword indexed, same 90-day TTL (added 2026-11) |
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
     most of its words. The first pass reads the query in web-search syntax:
     `"quoted words"` match as a phrase, `-word` excludes, and `or` is an
     operator.
   - `entity_expansion` (`expand_entities`) — facts about entities related to
     the top hits' entities.
   - `segment` (`include_segments`, **on by default**) — raw conversation
     segments from `search_segments` (see
     [Conversation segments](#conversation-segments)), kind `segment`. Each
     row's `fact_text` is the whole verbatim text; the reranker reads only a
     `SEGMENT_RERANK_CHARS` (1,200) excerpt around the best keyword match
     (`rerank_excerpt`). Which segments come back depends on the reader:
     `reader_person_id`, `reader_user_id` and `room_ids`, applied on every
     call. A caller that names no reader gets no private conversation at all.
     A keyword-only segment row has no `similarity` key (there is no cosine).
   - `insight`, `episode`, `chat_turn` (`include_insights`,
     `include_episodes`, `include_chat_turns`). These stores cannot filter by
     person, so a restricted data scope leaves them out. The dense-only
     `chat_turn` leg also does not run while the `segment` leg is on, even when
     asked for: both read the same conversation, and the segment leg adds a
     keyword leg and the reader rule. `search_chat_turns` itself is unchanged.
     The `segment` leg can filter by person, so it stays on under a restricted
     scope, and its reader is then the scope's person and nothing else.
2. **Fusion** — reciprocal rank fusion over every leg, k=60 (`fuse`); each row
   records its rank in every leg that found it (`leg_ranks`).
3. **Reranker** (Qwen3-Reranker-0.6B, local) — on by default, off with
   `MEMORY_RERANK_ENABLED=0`. It scores the first 16 fused candidates
   (`MEMORY_RERANK_MAX_CANDIDATES`) with a graded relevance in 0..1: the
   probability of "yes" against "no", read from the top 20 logprobs Ollama
   returns. When one of the two is not listed, its logprob is bounded by the
   lowest one listed, so a confident answer still gets a graded score. (An
   Ollama without logprobs gives 1 or 0.) Candidates are ordered by that
   score alone, whichever leg found them. On any reranker error the fused
   order stands.
4. **Blend** — `relevance` (the rerank score, or `10 / (10 + fused rank)` when
   there is none — one 0..1 scale for every leg; cosine similarity is not used)
   + recency + importance + access count, with supersession demotion; then
   near-duplicates are dropped.
5. **Reserved places** — a leg named in `RESERVED_PLACES` (`facts.py`) keeps at
   least that many places in the top `limit` whenever it returned anything,
   chosen by its own score: `entity_expansion` 1, and `graph` 2 for a graph leg.
   A related-but-not-similar fact is exactly what a query-document reranker
   discards. Reserved picks take at most `(limit - 1) // 2` places in total
   (none at a `limit` of 1 or 2), never the top result, and never a
   near-duplicate of a row already returned.

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

The `search_memory` tool passes the reader on both paths (direct and RIP 15):
`reader_person_id` is the run identity's `person_id`, whatever
`ROBOTHOR_DATA_SCOPING` is set to, `reader_user_id` is the run's user id (the
one a member's webchat session key carries), and `room_ids` are the room of
the current room run plus every room the calling user is a member of. Callers
that name no reader — the bridge's `/api/memory/search`, MCP `search_memory`,
deep reasoning, cron and system runs — receive no private conversation. The
Memory page's fact search (`GET /api/memory/facts?q=`) asks for no segments
and keeps only `kind == "fact"` rows, because it edits and forgets facts by id. A segment row in the
tool's output adds `kind`, `topic`, `source_kind`, `source_ref`, `start_ref`,
`end_ref`, `part`, `started_at` and `ended_at` to the usual fields; a fact row
keeps its shape. Segment rows carry `source: "segment"`, so fact-access logging
for outcome attribution skips them.

### Memory router (RIP 15)

Off unless `ROBOTHOR_RIP_15_ENABLED` is set. When it is, the `search_memory`
tool routes through `robothor/memory/router.py`, which classifies the query
(heuristics) and queries only the stores that fit:

| Class | Stores | Notes |
|-------|--------|-------|
| `exact_lookup` | Knowledge Vault captions + facts | numbers, ids, addresses |
| `temporal` | facts + episodes | results recency-reordered (latest first) |
| `how_to` | procedures + facts | |
| `who_is` | facts + entity-graph expansion | |
| `intent` | standing intents + facts | |
| `default` | facts + insights | no episode/entity fan-out |

The facts it gets come from `search_facts` (ranked as above); the stores are
then merged with `rrf_fuse` (k=60, `robothor/memory/fusion.py`) and
budget-capped. Without the flag the tool calls `search_facts` directly.

## Conversation segments

A fact is what the extractor chose to keep, and extraction loses detail: the
code someone read out, the exact wording of a decision. `memory_segments`
(migration 177, `robothor/memory/segments.py`) keeps the conversation itself as
a recall unit. Each chat session is split into contiguous segments, one subject
each, and every segment is embedded and keyword indexed.

**Splitting** (`segment_turns`) is deterministic and makes no LLM call. Only
user and assistant turns with text count. A new segment starts:

| Rule | Constant | Starting value |
|------|----------|----------------|
| Silence between two turns | `SEGMENT_GAP_SECONDS` | 30 minutes |
| Change of subject at a user turn: cosine between the mean vectors of the turns before and after it | `SEGMENT_SIMILARITY_BREAK`, `SEGMENT_WINDOW_TURNS` | below 0.5, 2 turns each side |
| Size budget, so a hit fits a context budget | `SEGMENT_MAX_CHARS`, `SEGMENT_MAX_TURNS` | 4,000 characters, 24 turns |

A subject break needs the open segment to hold `SEGMENT_MIN_TURNS` (2) turns
first. A fragment of fewer than 2 turns and under `SEGMENT_MIN_CHARS` (200)
characters, such as a lone "thanks", joins its neighbour; a conversation that
is only a fragment is not indexed yet. The values are starting points, to be
tuned by the memory eval.

A single turn over the size budget is never clipped. It is split into
consecutive segments, cut after the last paragraph break, else sentence end,
else space in the second half of each budget-sized window. Each piece has the
turn's message id as both `start_ref` and `end_ref` and a `part` of 0, 1, 2 …;
a piece never shares a segment with another turn. With the `User: ` /
`Assistant: ` label removed, the pieces' texts in `part` order concatenate to
the turn exactly.

**Text** is the turns verbatim, one per line, `User: …` / `Assistant: …`,
redacted with the same `redact()` chat_store applies before embedding — also
for rows written before the store redacted at the door. `topic` is the opening
of the segment's first user turn. Each row names its source: `source_kind`
(`conversation`; `document` and `email` are reserved), `source_ref` (the
session key), `start_ref` and `end_ref` (the first and last `chat_messages.id`
it covers), `part`, and `started_at` / `ended_at` (event time). `person_id` is
the session's person.

**Who may read a segment** (`scope_kind`, `scope_id`, `conversation_scope`).
The rule fails closed: a conversation is `shared` only when it is known to be
multi-party, and today that is a workspace (`agent:{agent}:room:{id}`, even
when it records a person), whose segments reach only readers who pass that
workspace in `room_ids`. Every other conversation is `user`, owned by the
first of:

1. the session's recorded person (`chat_sessions.person_id`) — `person:{id}`;
2. the member a webchat session key names (`agent:{agent}:user:{uid}`, the key
   `chat.derive_user_session_key` writes; the session row itself carries no
   person) — `user:{uid}`;
3. for the operator's main session (`EngineConfig.main_session_key`), the
   operator's person from `get_owner_person` — `person:{id}`.

A conversation with no owner — a direct message from an unrecognised sender, a
TUI or service session, the main session of an instance whose operator is not
linked to a person, and for now group chats — is `user` with no `scope_id`:
readable by no one. A reader sees shared segments plus the user segments whose
`scope_id` is one of theirs (`person:` their person, `user:` their user id).
Scope belongs to the conversation: every indexing run re-scopes all of a
session's stored segments, so a session whose person becomes known later is
re-scoped in full. This rule is applied on every search, independent of the
`ROBOTHOR_DATA_SCOPING` rollout, because verbatim conversation is more
sensitive than an extracted fact.

**Indexing** (`index_conversation(session_key, tenant_id=…)`) runs in the
background after every saved exchange: `chat_store.save_exchange_async`
spawns it once the turn is committed, beside the turn embedding, for every
channel that saves through it. It re-segments from the start of the session's
last segment, so an open segment grows as the conversation continues, and
upserts on `(tenant_id, source_kind, source_ref, start_ref, part)`: running it twice
writes nothing the second time. Database work runs in a worker thread. A
failure (embedder down, database error) is logged and never reaches the turn;
the next exchange retries the same turns. One run reads the last segment's
turns plus at most `SEGMENT_INDEX_MAX_TURNS` (200) new ones, so the first
index of a long history proceeds over several exchanges rather than in one
batch, and a run for a conversation that already has one in progress in the
process is skipped.

**Searching** (`search_segments(query, tenant_id=…, limit=…)`) fuses a dense
leg (HNSW cosine) and a keyword leg (`ts_rank_cd` over the generated
`tsvector`) with `rrf_fuse` (k=60). The keyword leg ORs the query's terms, so a
long natural question still matches on the words that matter. The tenant is a
required argument with no default. The dense leg uses `apply_hnsw_session`,
whose iterative scan (on wherever pgvector supports it) lets a small tenant
beside a large one still get `limit` rows. The reader is `reader_person_id`,
`reader_user_id` and `room_ids` (the rule above); `since` / `until` bound event time. When the
embedder is down the keyword leg answers alone (`embed_missing=False` skips
the embedding call for a caller whose own already failed). Each hit carries
`id`, `kind: "segment"`, `source: "segment"`, `text`, `topic`,
`source_kind`, `source_ref`, `start_ref`, `end_ref`, `part`, `started_at`,
`ended_at`, `scope_kind`, `similarity` (or `None`), `keyword_rank` (or
`None`) and `rrf_score`.

**Retention.** A segment never outlives the chat it copies. It references its
`chat_sessions` row with `ON DELETE CASCADE`, so clearing a session or the
stale-session sweep removes its segments with its messages. Lifecycle step 10
deletes segments that started more than `SEGMENT_RETENTION_DAYS` (90, no more
than `CHAT_TURN_TTL_DAYS`) ago, tenant by tenant, and turns older than that are
never indexed. Segments move with their session in `genus tenant
move-workload`.

Segments are the `segment` leg of `search_facts` and so of the `search_memory`
tool (see [Retrieval](#retrieval)).

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
10. **Chat TTL** — delete 90d+ un-pinned, un-referenced verbatim turns, and conversation segments that started 90d+ ago
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
