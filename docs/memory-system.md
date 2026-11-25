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
| `memory_segments` | Raw conversation split into topical segments, vector + keyword indexed, same 90-day TTL (added 2026-11) |
| `fact_access_log` | Per-run retrieval audit for outcome attribution (added 2026-04) |
| `ingested_items` | Dedup tracking (content hash per source+item+tenant) |
| `ingestion_watermarks` | Per-source progress and error tracking |

All embedding columns are `vector(1024)` with HNSW indexes (m=16, ef_construction=200).
Active-row **partial** HNSW indexes (`idx_*_active`, migrations 073/074) keep
superseded vectors out of the candidate budget. Every pooled connection gets
an **iterative index scan** at checkout (`hnsw.iterative_scan=relaxed_order`,
`ANN_ITERATIVE_SCAN_MODE` in `robothor/db/connection.py`, which records why
relaxed rather than strict order). Without it, a search under a narrow tenant
or person filter spends the candidate budget on other rows and comes back
with fewer rows than exist. Relaxed order may return rows slightly out of
distance order, so the fact vector leg re-sorts by similarity before ranking.
Every vector search also applies `robothor/memory/vector_tuning.py`
`apply_hnsw_session(cur)`: `hnsw.ef_search=100`, and with
`MEMORY_HNSW_ITERATIVE=1` the same scan mode again inside the transaction
plus `hnsw.max_scan_tuples` (20000).

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
   - `graph` (**off by default**; `include_graph=True`) — graph activation
     from the entities the query names and the entities the top hits mention
     (see [Associative recall](#associative-recall)). Off because it has not
     earned its place: see [Is the graph leg on?](#is-the-graph-leg-on).
   - `entity_expansion` (`expand_entities`; with the graph leg on, only when it
     returned nothing) — facts about entities related to the top hits'
     entities.
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
     scope, read by the same reader as the fact legs, which the scope only
     narrows.
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
   chosen by its own score: `graph` 2, `entity_expansion` 1.
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

The `search_memory` tool passes the reader on both paths (direct and RIP 15),
and the reader of a private conversation or a private fact is decided by the
run, not inferred from its principal or its identity: `ToolContext.private_reader`
(`robothor/engine/private_recall.py`), which each tool turn publishes from its run. A run has
one only when it serves a verified person in their own one-to-one
conversation: a webchat turn in their own `agent:{agent}:user:{uid}` session
(or the main session, for the operator), or a Telegram direct message. It has
none inside a workspace (room) run, and none for cron, hook, event,
workflow, sub-agent, federation, manual, IDE, Slack or plugin-channel runs,
whoever they execute as — a report workspace that runs as a member does not
recall that member's private chats into its edition. With no private reader
the run reads shared segments only. This is applied whatever
`ROBOTHOR_DATA_SCOPING` is set to. `room_ids` are, inside a workspace run, that
workspace alone; otherwise the workspaces of the member the run serves (its
private reader), and none for a run that serves no one. Callers that name no
reader — the bridge's `/api/memory/search`, MCP `search_memory`, deep
reasoning — receive no private conversation and no private fact. A restricted
data scope only narrows that reader, never creates one
(`visibility.served_reader`, applied once to every leg): `search_facts` keeps
the reader only when its person is the scope's person, and its user id only
when that user is the same person (an active `user_accounts` row, or a
`tenant_users` row, linking them); with no reader, or a reader who is someone
else, it returns no private segment and no private fact. Interactive warm-up
reads the same way: the runner decides the run's private reader before the
preamble's worker thread starts, and the graph lines read as that person
only; the sender seeds the walk but is not a reader. The Memory page's
fact search (`GET /api/memory/facts?q=`) asks for no segments and keeps only
`kind == "fact"` rows, because it edits and forgets facts by id. A segment row in the
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
| `who_is` | facts (with the one-hop entity expansion) | |
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

1. Importance scoring (LLM-judged, 200/run, 600s budget) of facts not yet marked scored
2. Decay computation (recency of use × access × reinforcement × importance × **outcome penalty**)
3. Garbage pruning (low decay + low importance + never recalled)
4. Consolidation (similar facts merged, superseded via `is_active=FALSE`)
5. Unconsolidated sweep
6. Cross-domain insight discovery (72h window)
7. Cross-entity relationship inference
8. **Episode building** — cluster recent facts by time + entity overlap, LLM-title+summarize, embed
9. **Preference tracking** — extract from high-importance facts, detect drift (stale flag)
10. **Chat TTL** — delete 90d+ un-pinned, un-referenced verbatim turns, and conversation segments that started 90d+ ago
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

# Extract facts from raw text (LLM-powered, one call, see "Write path")
facts = await extract_facts("Alice Moreno chose Redis for caching on 2026-03-02.")
# Returns: [
#   {"fact_text": "Alice Moreno chose Redis for caching on 2026-03-02.",
#    "category": "decision", "confidence": 0.9, "event_time": "2026-03-02",
#    "mentions": [{"surface_form": "Alice Moreno", "entity_type": "person"},
#                 {"surface_form": "Redis", "entity_type": "technology"}],
#    "relations": [{"source": "Alice Moreno", "relation": "chose", "target": "Redis"}],
#    "entities": ["Alice Moreno", "Redis"]},
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
| `decay_score` | Computed: recency + fading access + reinforcement + importance + utility |
| `reinforcement_count` | Incremented when fact is confirmed by new evidence |
| `outcome_failures` | Failed runs whose recalls returned the fact |
| `outcome_successes` | Successful runs whose recalls returned the fact (migration 180) |
| `outcome_citations` | Successful runs whose answer used the fact (migration 180) |
| `utility` | Computed nightly from the three outcome counts, in (-1, 1) (migration 180) |

**Decay formula:**

```
days_since_use = hours_since_use / 24
recency = 0.5 ** (hours_since_use / 72)                        # 72h half-life
access_boost = min(ln(1 + access_count) / 5, 0.3) * 0.5 ** (days_since_use / 30)
reinforcement_boost = min(ln(1 + reinforcement_count) / 5, 0.2)
importance_floor = importance_score * 0.4
utility_term = 0.3 * utility             if utility >= 0       # at most +0.3
               max(-0.4, 0.6 * utility)  if utility <  0       # at most -0.4
score = clamp(max(importance_floor, recency) + access_boost + reinforcement_boost
              + utility_term, 0, 1)
```

A fact with high importance can never fully decay (importance_floor). Use
resists decay, but use fades: the access boost halves every 30 days since the
last use, so a fact recalled once a year ago scores as if it had never been
recalled. Utility replaces the old failure-only penalty with one signed term,
so failures are not counted twice. The failure side is never milder than the
old 0.1 per failure and reaches the old 0.4 cap at four failures, as before.

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
Retrieval ranking reads neither: it reads `utility` (below).

**Prune.** A fact is retired when its decay score is below 0.1, its
importance below 0.3, and it has not been used recently enough to matter —
never recalled, or last recalled more than 90 days ago (three access-boost
half-lives) — outside the protected categories (decision, preference,
resolution) and operator-confirmed facts, which are never pruned. A fact
recalled last week keeps its recency and boost and stays. Positive utility
protects in proportion: it lifts the decay score by up to 0.3, so a fact that
has helped runs stays above the threshold however long ago it was last
recalled, while a fact with low utility gets no such lift.

The first deep maintenance pass after upgrading to this rule retires, in one
go, every low-importance fact last used more than 90 days ago that the old
never-fading boost had kept. The retirement is a soft `is_active = false`, as
for every prune, and can be reversed.

### How memories strengthen and fade

A memory grows stronger when a successful run's answer uses it, weaker when
the runs that recalled it fail, and not at all for merely being recalled —
even in runs that went well.

1. **Recall is logged per run.** Each fact `search_memory` returns is written
   to `fact_access_log` with the run's id (warm-up injections count as use but
   are not run-attributed).
2. **The run's end decides the credit** (`robothor.engine.run_outcome_attribution`,
   called by `delivery.deliver` for scheduled, hook and workflow runs):
   - a run that failed adds one to `outcome_failures` of every fact it
     recalled;
   - a run that *succeeded* — status `completed`, no error, not stopped on its
     budget, non-empty output — adds one to `outcome_citations` for each fact
     its answer **used**. It also adds one to `outcome_successes` of every
     fact it recalled, but that count is a record, not credit: a search
     returns about ten facts and the answer uses few of them, so crediting all
     of them would let a fact rise by being returned;
   - a timeout, cancellation, skip or pending run adds nothing.

   A fact counts as **used** when one of its distinctive values appears as a
   whole word in the run's final output and was not already in the task text
   (`robothor.memory.utility.is_cited`). Distinctive values are email
   addresses, URLs, identifiers mixing letters and digits of four or more
   characters (an invoice, ticket or order number, a version), and bare
   numbers of five or more digits. Ordinary words, shorter numbers, numbers
   with a unit or ordinal, clock times, calendar dates and bare years never
   count. A value the run read from a tool result also matches; only the task
   text is excluded. Each run counts once per fact, however
   many times it recalled it. Benchmark runs credit nothing.
3. **The nightly decay pass computes utility** per tenant from the counts
   (`robothor.memory.utility.compute_utility`):

   ```
   utility = (citations - failures) / (citations + failures + 2)
   ```

   No evidence gives 0. Utility always lies strictly between -1 and 1. Every
   added use or failure moves it less than the one before. Plain successes do
   not enter it, so they neither raise a fact nor offset its failures.
   The counts live on the fact, so they survive the access-log roll-up.
4. **Trust.** A fact whose `origin` is `external` or `inferred` earns no
   positive utility; its failures still count. Until the graph schema adds
   the `origin` column, every fact is eligible.
5. **Ranking.** In the recall blend, utility took over the 0.05 weight raw
   `access_count` used to have
   (`0.55 relevance + 0.25 recency + 0.15 importance + 0.05 × utility / 2`).
   Ranking by retrieval count made popular facts more popular; it no longer
   counts. Relevance stays dominant: utility spans 0.05 of blend at most, so
   no utility lifts a fact over one more relevant by more than
   `0.05 / 0.55 ≈ 0.09` (`UTILITY_RANK_MARGIN`), other signals equal.

The constants live in `robothor/memory/utility.py`, `lifecycle.py`
(`ACCESS_BOOST_HALF_LIFE_DAYS`, `PRUNE_RECENT_USE_DAYS`) and `facts.py`
(`BLEND_WEIGHTS`). `utility.apply_edge_utility` maps a fact's utility change to
a bounded weight change for the graph edges it supports; the graph wires it.

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
    utility=0.25,
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
trust. Code written before it reads and writes exactly as before. The first
writer of the new tables is [Identity](#identity): it links CRM records to
entities and records resolution decisions and mentions. Every new table has
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
deletes its mentions. A fact has at most one mention per surface form (case
ignored): the graph keys migration (`crm/migrations/*_memory_graph_keys.sql`)
makes `(fact_id, lower(surface_form))` unique, so writing a mention twice is a
no-op.

**`memory_relation_evidence`** lists every fact supporting an edge, keyed by
`(relation_id, fact_id)`. The migration copies each edge's existing
`memory_relations.fact_id` into it; the [write path](#write-path) adds a row for
every fact that states the relation.

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
A shared fact's content hash is unchanged; a private fact's includes its scope
(see [Write path](#write-path)), so no stored hash changes.

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

### Identity

`robothor/memory/identity.py` decides which entity a name in a fact refers to.
A wrong merge would attribute one person's facts to another, so it is
conservative: when the evidence does not single out one entity, the name stays
**unresolved**, and the decision records the candidates so a later pass or the
operator can settle it.

**CRM records are the canonical nodes.** `sync_crm_entities(tenant_id)` gives
every live CRM person and company in a tenant exactly one entity, linked by
`crm_person_id` / `crm_company_id`, and named with the CRM full name or
company name. No CRM contact data is copied into memory: no email, channel
handle or domain becomes an alias, because `get_entity` applies no data scope
and the CRM withholds those fields from restricted readers. Nickname variants
are never added either. An unlinked entity with exactly the same name and type
is linked rather than duplicated (recorded as a decision with method `exact`),
unless two CRM records share the name. When the name is already taken by
another record's entity, the new one is named
`<name> #<first 8 characters of the CRM id>` and carries the plain name as an
alias, so a bare mention of that name stays ambiguous. A CRM person whose name
is not a full name (one token, or an initial at either end) never takes over an
existing entity and is always named that way, without the bare name, so an
entity built from every "Alex" is never anchored to one CRM record. When a
record is renamed, its previous full name stays as an alias, because facts
already use it. The CRM data layer keeps the link current on every create,
update and merge, through `robothor/crm/hooks.py`, after its own commit. A
failure there never fails the CRM write. The nightly lifecycle pass runs the
full sync for every tenant. It catches writes that bypassed the data layer,
and people merges whose hook failed, which it finds from the merge note the
data layer writes. Company merges leave no note, so a failed company-merge
hook is not repaired. Memory never writes to the CRM.

**Resolution** (`resolve_mentions(tenant_id, mentions, *, cur=None)`) is
synchronous, makes no model call, and can run inside the caller's
transaction. Rules, first match wins:

| Rule | Resolves when | Method | Confidence |
|------|---------------|--------|------------|
| Strong key | A CRM id, an email the CRM holds, or a `contact_identifiers` channel identifier (`"<channel>:<identifier>"`) names one record. An email written as the name counts. Keys that disagree leave it unresolved | `strong_key` | 1.0 |
| Full name | Two or more tokens, neither end an initial, matching exactly one entity of a compatible type by name or alias, ignoring case and whitespace | `full_name` | 0.95 |
| Context | A full name matching several entities, or only a nickname variant ("Bob Rivera" for "Robert Rivera"), with exactly one candidate supported by the same organisation (in the context or named in the same fact), the same email domain (not a free-mail domain), or an earlier mention in the same conversation | `context` | 0.85–0.9 |
| Single token | A first name, nickname, initials, or a name with an initial at either end: only by a strong key, or when exactly one candidate already appears in the same conversation and it is not the operator | `context` | 0.75 |
| Provisional | No candidate at all, and a full name or a known non-person type: a new unlinked entity. Never a person from a single token, never an entity of unknown type | `exact` | 1.0 |

A single-token organisation name ("Globex") that matches exactly one
organisation resolves with method `exact`. A person name never resolves to an
organisation, and the reverse is also true. A name of unknown type that both a
person and an organisation carry stays unresolved. Entities whose CRM record
was deleted are not candidates, and they also stop a duplicate from being
created. The operator, found through `get_owner_person`, is reached by strong
key or full name, never by a first name.

"The same conversation" means an active fact with the same
`metadata->>'session_key'` that mentions the candidate under a live
`strong_key`, `full_name` or `operator` decision. Only facts the write itself
may see count: shared ones, private ones of the write's own scope, and none
with another person's `person_id`. The write's scope comes from the mention's
context (`scope_kind`, `scope_id`, `person_id`). Without it, only shared,
unscoped evidence is used. This module never sets a fact's `person_id` or
scope.

**Decisions are recorded once.** Every mention gets a decision row, or reuses
a live one with the same tenant, surface form (ignoring case and whitespace),
entity type, resolved entity, method and basis. The basis is the evidence the
decision rests on: the candidates and the matching context, or the reason it
was left unresolved and the conversation. Re-ingesting the same text adds no
rows. `write_mentions` writes the `memory_mentions` rows for a fact, at most
one per surface form.

**Reversal.** `reverse_decision(tenant_id, decision_id, *, reason,
new_entity_id=None, fact_ids=None, dry_run=False)` works at one of two scopes.
A decision is shared by every mention it resolved the same way, so the scope
matters:

- **Some facts** (`fact_ids`). Only this decision's mentions on those facts
  move, onto a new `operator` decision that applies to them alone. The shared
  decision stays live for every other mention, and future mentions are not
  affected. This is how an unresolved name is settled.
- **The whole decision.** It sets `reversed_at`. When `new_entity_id` is given,
  it records an `operator` decision and points `superseded_by` at it. Every
  mention under the decision moves to that entity, or to NULL. From then on the
  name never resolves to the rejected entity, unless a later live operator
  decision chooses it again. Three kinds of decision cannot be redirected this
  way and must be corrected per fact: one that left its name unresolved, a
  `context` decision (it rested on one conversation or organisation), and one
  whose name is not a full name (a first name may only resolve on
  conversation evidence).

`dry_run` returns the same report and writes nothing. The report lists the
mentions moved, their facts, and the relations those facts support: the
derived rows to rebuild. The same list is stored on the decision it writes.

A CRM merge is recorded the same way: a decision resolves the loser's entity
to the keeper's, and nothing rewrites mention ids. A later decision records
how the merge shaped it, judged on the match the rules decided on:

- `via_merge`: it reached the keeper only through the loser's name, alias or
  key. The mention named the loser.
- `merge_collapsed`: the keeper's own name or key matched equally, and the
  merge removed the loser as a competitor. This is the case of two CRM
  records with the same name.
- Nothing: the keeper matched on its own, and the merge added at most a weaker
  match (a nickname variant of the loser's name).

Reversing the merge decision moves `via_merge` mentions back to the loser's
entity and leaves `merge_collapsed` mentions unresolved, because the name no
longer singles anyone out. Everything else stays with the keeper. These moves
are per-fact operator decisions, so they neither bind nor block later
mentions. Those resolve by the ordinary rules: a bare name of two same-named
people whose merge was reversed goes to the one record still live in the CRM.
Identity therefore lives only in decisions and mentions. Entity rows are never
merged, and there is no canonical-entity lookup.

**Backfill.** `backfill_mentions(tenant_id, *, dry_run, batch_size=500,
resume_from=None)` turns the names in existing `memory_facts.entities` into
mentions. It walks active facts in id order, one transaction per batch, under
a per-tenant advisory lock. Context comes from the other names in the fact, its
conversation and its scope. Its `person_id` is a strong key for the one
person-typed name it carries, and only when that name is the CRM person's full
name. The job returns `last_fact_id` so that it can resume. A dry run writes
nothing: each batch is read-only. It reports counts by method, every unresolved
name with its candidates, the provisional entities it would create, and the
evidence of each `full_name` and `context` resolution. The schema has no unique
key on a fact's mentions, so the backfill enforces one per surface form in
code. Run `sync` first: the report counts CRM records still without an entity.

```bash
genus memory-identity sync --tenant <tenant> --dry-run
genus memory-identity backfill --tenant <tenant> --dry-run --json
genus memory-identity backfill --tenant <tenant> --resume-from 12000
genus memory-identity reverse --tenant <tenant> --decision 42 --reason "different person" --entity 7 --fact 1301
genus memory-identity reverse --tenant <tenant> --decision 42 --reason "different person" --all --dry-run
```

`reverse` needs either `--fact` (repeatable) or `--all`.

**Read side.** `find_entity(tenant_id, name, entity_type=None)` is a pure
lookup by the same rules, and records nothing. It matches an exact name or
alias of one shared entity, and returns several matches as an `Ambiguity`. It
does not answer an email: a reader would learn which CRM person owns it. `get_entity` uses it first and falls back to the
best-connected entity of that name. It names its columns, so the embedding is
never returned.

### Write path

Every way of storing a memory goes through one function,
`robothor.memory.write_path.write_facts(facts, *, source_content, context,
conflict_check=True, deadline=None)`, or `ingest_text(content, context, ...)`,
which extracts first and then calls it. The writers routed through it:
`ingest_content` (and through it conversation ingest, the bridge's
`/memory/store` and the orchestrator's `/ingest`), the `store_memory` tool (and
the MCP `store_memory`), the deferred write jobs (`MEMORY_ASYNC_WRITE`),
`record_resolution`, breadcrumb promotion, and nightly and intra-day
consolidation (`write_derived_fact`). For each fact:

1. the quality gate, as `store_fact` has always applied it;
2. conflict handling (`resolve_and_store`: dedup, reinforce, classification,
   supersession, operator-confirmed protection), searching only facts of the
   same scope. `record_resolution` and breadcrumb promotion opt out, as before;
3. the row, with its `origin`, `event_time` and scope;
4. its mentions, resolved by [Identity](#identity) and written to
   `memory_mentions`;
5. each relation it states between two resolved mentions: the edge is upserted
   (`relation_type` is the phrase, `predicate` its lowercased, underscored
   form; a new edge's `fact_id` is this fact) and an evidence row ties it to
   this fact. A relation with an unresolved endpoint creates nothing and is
   counted (`relations_unresolved`).

**One extraction.** A single model call returns, per fact, its text, category,
confidence, typed mentions (the six entity types), the relations it states, and
`event_time` when the text states or clearly implies when it happened (an ISO
date or date-time, otherwise null). The schema is strict. There is no separate
entity-extraction call on any write path (`extract_entities` and
`extract_entities_batch` remain for their direct callers). A writer that does
not extract (`record_resolution`, breadcrumbs) gets as mentions the names of
known shared entities its text contains. `memory_facts.entities` is still
filled with the mention names.

**Extraction failure.** When extraction fails (a timeout, an answer that is not
the JSON asked for, a model error), the result reports `extraction_failed` and,
for content someone submitted to be kept (`store_memory`, the write jobs, the
ingest routes), the raw content is stored as one fact
(`metadata.extraction = "fallback_raw"`, with `extraction_error`). Conversation
ingest stores no raw transcript: the turns are left for the next run to extract
again (the watermark does not move). When the model answers and finds nothing,
nothing is stored, except by `store_memory` and the write jobs, which keep the
raw content as they always did.

**Storage failure.** A fact the quality gate declines is `facts_refused`. A fact
that could not be stored because something broke (the embedding service, the
database) is `facts_failed`, with the reason in `errors`, and no caller records
the write as done: `ingest_content` raises `MemoryWriteError` (conversation
ingest then leaves its watermark, so the turns are retried), a write job is
marked `failed` with the error, a breadcrumb is kept for the next promotion, and
`store_memory` answers with an `error` when nothing was kept. A stored fact
whose mentions or relations could not be written is counted in `graph_errors`,
which `ingest_content` and `store_memory` return.

**Origin**, from the source, by `origin_for(source_channel, content_type,
role=)`; a conversation is extracted once per role so each fact's origin is its
speaker's (the other role is shown to the model as read-only context):

| Source | Origin |
|---|---|
| A person's turn (`role="user"`) | `user_stated` |
| The assistant's turn, or what an agent writes (`role="assistant"`, `store_memory`, `record_resolution`, breadcrumbs) | `agent_generated` |
| A tool result (`role="tool"`) | `tool` |
| Channel `crm`, no speaker | `crm` |
| Channels `email`, `discord`, `telegram`, `gchat`, `voice`, `camera`, no speaker | `external` |
| Channels `cli`, `api`, `mcp`, `conversation`, or any other, no speaker | `agent_generated` |
| Content type `email`, `web`, `webpage`, `web_page`, `document`, `upload`, `uploaded_document`, `pdf`, `attachment`, `external`, whoever relays it | `external` |

When two rows apply, the less trusted wins, so relaying never raises trust. A
derived row takes the least trusted origin of its inputs: a consolidated fact
built from a user-stated and an external fact is external.

Trust is never taken from the caller. No write is `operator_confirmed`: that is
the operator's own edit of a stored fact (the bridge's memory-fact edit route),
and an `operator_confirmed` key in a writer's metadata is dropped, so ingest
cannot mark a fact confirmed either. The orchestrator's `/ingest` route is not
authenticated, so the channel it is told is only a claim: what it stores is at
most `external` (`origin_cap` on `ingest_content`).

**Edge origin.** An edge's `origin` is the most trusted origin among the facts
supporting it (active ones first), recomputed whenever evidence is added: a
relation a user stated stays user-stated when an email repeats it. Each
evidence row keeps its own fact, so a reader that must be strict checks the
origin of every supporting fact.

**Scope.** Facts are stored `shared`, exactly as before. Making the facts of a
one-to-one conversation private to its person would stop scheduled agents and
other team members recalling them, so it is left to the operator to decide; a
private scope is written only when a caller passes one (`scope=` on
`ingest_content`, `store_memory_content` and `WriteContext.for_source`), and no
caller does yet. `conversation_scope` gives a conversation's owner the way the
conversation segments do (`person:{id}`, `user:{uid}`). What the mechanism
guarantees, tested:

- *Hash.* The content hash (`drift.compute_fact_hash`, unique among active
  facts per tenant) covers a non-shared fact's scope: its person slot holds
  `scope:{kind}:{scope_id}`, which no `person_id` can equal, so a private fact
  never shares a digest with any shared row, even one whose `person_id` names
  the same person. A shared fact hashes as it always did. `update_fact` and the
  bridge's edit route hash with the row's scope, so an edited private fact stays
  private. An older release editing a private fact recomputes without the scope
  and records drift (refusing the edit in enforce mode), which fails safe. So the
  same text stored shared and private, or private to two people, is two facts.
- *Conflicts.* The similarity search behind reinforcement, deduplication and
  supersession only sees facts of the same scope.
- *Consolidation.* A consolidation group never mixes tenants or scopes; the
  consolidated fact takes the group's scope. When the consolidated text is one
  of the members' own (consolidation falls back to the first member's text if
  the model fails), nothing is derived and no member is retired.
- *The graph.* No reader yet filters edges by the scope of their evidence, so a
  private fact creates no node from its own text (its mentions resolve to
  existing nodes or stay unresolved) and no edge; the relations it stated are
  kept on the fact (`metadata.graph_pending_relations`) and counted
  (`relations_private_deferred`).

**Latency.** `store_memory` makes the one extraction call (70 s budget) and,
only for a fact with a similar existing one, the one conflict-classification
call; past the call's 105 s budget a fact is stored as new
(`conflict_checks_timed_out`, `metadata.conflict_check = "timed_out"`), and
nothing is superseded without a classification. Every database step runs off
the event loop.

### Who can read a fact

A fact has an owner, `memory_facts.scope_kind` / `scope_id`, written by the
write path: `shared` (everyone in the tenant), or private to one person
(`user`, `person:{crm id}`), one member (`user`, `user:{uid}`) or one agent
(`agent`, `agent:{id}`). The ids are the ones `segments.conversation_scope`
gives a conversation's owner.

One rule decides who reads it, in `robothor/memory/visibility.py`: a reader
sees shared facts and the facts owned by one of their own ids. A reader with
no ids sees shared facts only. The reader is the one conversation segments
use: the person the run serves (`ToolContext.private_reader`, see above),
never the run's identity alone. A restricted data scope only narrows it.

The callers pass a person and a member id today, never an agent id, so a fact
scoped to an agent is read by no caller yet: the rule fails closed.

It is applied in every reader that returns fact text:

- `search_facts`: the vector, keyword and one-hop expansion legs. The graph
  walk (`memory_activate`) reads as one id, the person's, else the member's
  (`visibility.graph_reader`); a bare person id is read as `person:{id}`.
- `entities.get_all_about` and warm-up's name-matched lines (migration
  `crm/migrations/181_memory_fact_scope_readers.sql` gives
  `entity_facts_for_candidates` the shared-only rule, same signature).
- Everything derived for every reader reads shared facts only: insight
  discovery, episodes, preferences, the working context, intents, gap
  analysis, the memory projection, `crm.dal.get_person_memory`, and the
  nightly relation inference (`entities.find_cooccurring_entity_pairs` and
  the fact text `lifecycle.infer_entity_relationships` prompts with), whose
  edges every reader sees.
- De-churn (`robothor/memory/dechurn.py`) compares near-duplicates within one
  owner scope, so a private fact never deactivates a shared one.

Not filtered: the bridge's admin fact list (`/facts` in
`crm/bridge/routers/memory_facts.py`), browse and search alike. It is the
tenant admin's view of what the tenant holds and the place a fact is forgotten
from, so it shows private facts too. Its search calls
`search_facts(..., admin_erasure_view=True)`: a keyword-only switch, off by
default, that drops the owner rule from the fact legs and skips the graph leg.
Only that route, behind `require_tenant_admin`, passes it; no agent tool, MCP
tool or tool argument reaches it, which
`robothor/memory/tests/test_erasure_view_reach.py` checks. Forget acts on a
private fact like any other.

### Associative recall

When an agent works on a person, company or task, what is connected to it
comes to mind without being asked. `robothor/memory/activation.py` links text to
the entities it names and spreads activation from them over the graph; the
walk itself is the database function `memory_activate` (migration
`crm/migrations/*_memory_graph_activation.sql`).

#### Is the graph leg on?

No, not in recall and not in warm-up, unless a caller asks:
`search_facts(include_graph=True)`, `_build_entity_context(include_graph=True)`.
`robothor.memory.facts.GRAPH_LEG_DEFAULT` and
`robothor.engine.warmup.WARMUP_GRAPH_DEFAULT` are `False`.

The memory evaluation's synthetic world decided it (dev split, top-5 hits):
multi-hop was 21 of 126 with the leg on and 20 without (34 against 37 at
top-10), and raw detail fell from 40 to 27 of 110 with it on, because its
candidates crowd conversation segments out of the reranked head. Reserved
places (2, 1 or none), reserving only when the question itself names an
entity, seeding from possessives, 10 or 20 facts per entity, a wider walk and
reranking the graph's candidates by the question changed none of that. The
walk does reach the answer of most multi-hop questions, but ranks it by
activation mass (10th to 48th), which knows nothing of the question. The
graph keeps serving identity and current state; recall's associative leg
waits for a design that ranks what the walk reaches by the question.

**It needs mentions.** The walk reads which entities a fact is about from
`memory_mentions`, and nothing writes that table yet: existing memory links
facts to entities only by name. Until mentions are written, the graph leg
returns nothing. `search_facts` with `expand_entities=True` (the
`search_memory` tool's default) runs the one-hop `entity_expansion` leg: facts
about entities related, through `memory_relations`, to the entities its top
hits name. With the graph leg on, the expansion runs only when it returned
nothing; it keeps one reserved place.

**Seeds.** `seeds_from_text` links text to entities without a model call: exact
full names and aliases, case-insensitively, and e-mail addresses or @handles
matched against `contact_identifiers`. A single token never resolves on its
own, so a first name alone seeds nothing. Warm-up takes its phrases from the
name candidates its name-matching step already finds (runs of two or more
adjacent ones), and also seeds the sender's own entity (the entity whose
`crm_person_id` is the sender's CRM person). `search_facts` also seeds the
entities that its top hits mention (`memory_mentions`), counting only hits the
reader may see.

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
user-scoped to the reader (`scope_kind = 'user'` and `scope_id` the reader's
person id; a row private to an agent is never a person's, whatever its id). Only those edges count toward a node's degree and weight,
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

**Hub control.** The operator's own entities (those linked to the CRM person
`get_owner_person` returns, resolved inside the walk) are never seeds and never
transit nodes. If that lookup fails, nothing is walked and a warning is logged:
the operator's node is usually the largest hub. Seeds are weighted by inverse degree,
and fan-out is capped, so a node linked to everything cannot flood the result.

**Tenancy.** `memory_activate` is `SECURITY DEFINER`, like the warm-up's entity
recall function, so the planner can use its indexes under row-level security.
It filters every table by the tenant passed to it: both ends of an edge, the
edge, its evidence, the facts, the mentions. A session scoped to another tenant
gets nothing.

**Warm-up.** The entity context section starts with connected context from
trusted-only activation, then fills the rest of its character cap with the
name-matched facts as before, each fact once. Activation has a hard budget
(`GRAPH_WARMUP_BUDGET_SECONDS` in `robothor/engine/warmup.py`, 150 ms to
start), enforced by Postgres: before its first statement, the walk's own
connection gets a `statement_timeout` of the time left, reset before the
connection goes back to the pool, so the seed lookup and the walk stop at the
budget and nothing ever cancels a query from outside. On a timeout, an error or an empty result the section is
exactly the name-matched one. There is no model
call and no reranker. The name-matched facts are not filtered by origin.

What a turn pays: a message with no two adjacent name candidates, no e-mail
address or @handle and no known sender makes no extra query and no thread
hand-off. Otherwise one read-only connection arms the time limit, runs the
seed lookup and resets the limit (three statements); when a seed is found the
operator lookup and the walk run in between (five, or six when no owner row is
linked and the `owner.yaml` fallback runs).

The search path writes nothing. Rows found only by the graph are not logged as
fact access.

## Replaying real questions before a change goes live

A change to recall reaches agents only by a code change at its gate; until
then `genus memory-replay` shows what it would do to the questions a tenant
really asked. It runs each question through two recall configurations and
reports, per question, what each returned (rank, kind, an excerpt, the legs
that found it, relevance), the overlap of the two result sets (Jaccard on row
identity), the rows only one side found, the rows that moved, the retrieval
latency and the estimated context tokens of each side, then a summary: mean
overlap, how often the top result changed, how often each side returns a
conversation segment, p50/p95 latency per side and the legs of the rows only
the second side found.

```bash
genus memory-replay --tenant <tenant> --from-runs --since 7d
genus memory-replay --tenant <tenant> --from-chat --sample --seed 3 --limit 40
genus memory-replay --tenant <tenant> --questions questions.txt --as-person <crm-person-id> \
    --compare new+graph,new-segments --json --out replay.json --redact
```

**Questions.** `--from-runs` replays the `query` of every `search_memory` tool
call the engine persisted (`agent_run_steps.tool_input`, direct or wrapped in
`tool_call`), with the call's own `limit`. A question never reads more than
its run could: it is read as a person only when the run record shows the run
served that person in their own one-to-one conversation (a webchat turn or
plan step in their own session, the operator's main session, or a Telegram
direct message). Every other run (scheduled, hook, event, manual, workflow,
sub-agent, workspace, channel) is replayed with no reader, shared rows only,
whoever it ran as; the run's linked person alone is not enough.
`--from-chat` uses recent user turns as stand-in questions, each read as the
owner of its conversation (the rule segments are stored under); a turn whose
conversation has no known owner is skipped and counted, never replayed as an
unscoped reader. `--questions FILE` reads one question per line, as
`--as-person` / `--as-user` or as no reader (shared rows only). Automatic
warm-up recall and searches made outside a tool call record no query text,
and `fact_access_log` links runs to facts without the question, so neither
can be replayed. `--limit` (default 50) caps the questions; `--sample` takes a
seeded sample instead of the newest, the same for the same `--seed`.

**Configurations.** The presets are `PRESETS` in `robothor/memory/replay.py`,
the one place to add another:

| Preset | Segments | Graph leg |
| --- | --- | --- |
| `today` (recall as main serves it) | off | off |
| `new` (the product default on this tree) | on | `GRAPH_LEG_DEFAULT` |
| `new+graph` | on | on |
| `new-segments` | off | `GRAPH_LEG_DEFAULT` |

All four keep the tool's fan-out (entity expansion, insights, episodes) unless
`MEMORY_NARROW_SEARCH` is set, and all rerank. `--compare A,B` picks two;
the default is `today,new`. The two sides alternate which runs first, so a
warm cache favours neither. Latency is end to end per call: embedding the
question, the legs and the rerank, against the same local model server the
product uses.

**It writes nothing.** The replay calls `search_facts` directly, not the
`search_memory` handler, so no access count, access-log row or utility credit
is written, and no segment is indexed. Every connection it checks out is
inside `robothor.db.connection.read_only_scope`, which sets
`default_transaction_read_only` on the session and clears it before the
connection returns to the pool: a write that slipped in fails with
`ReadOnlySqlTransaction` instead of landing. Only the named tenant is read;
an unknown tenant is refused and there is no default.

**The report is sensitive.** It holds recalled memory and the questions
people asked, so its first line says so. `--redact` replaces every excerpt and
question with its id and length.

## Ingestion Pipeline

The full pipeline: extract facts with their mentions and relations, resolve
conflicts, write the graph with evidence, track dedup state.

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

`role=` (`user`, `assistant`, `tool`) names the speaker and so the facts' origin;
`read_only_context=` is background the model sees but never extracts from;
`scope=` asks for a private write (see [Write path](#write-path)). The result
reports `facts_processed`, `fact_ids`, `mentions_written`, `relations_stored`,
`relations_unresolved` and, when extraction failed, `extraction_failed` and
`extraction_error`.

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
