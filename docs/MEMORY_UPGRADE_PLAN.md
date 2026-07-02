# Memory System Upgrade Plan — Hunyuan / MIRIX Reconciliation

**Status:** Proposed (planning only, no code yet)
**Author:** Memory architecture review, 2026-05-30
**Branch target:** new branch off `main` after current `fix/audit-p2-runner-refactor` settles
**Related:** `docs/memory-system.md`, `brain/memory_system/MEMORY_SYSTEM.md` (instance)

---

## 1. Why this exists

A review compared the Genus OS memory system against the "hy-memory" (Tencent **Hunyuan** Memory)
OpenClaw plugin and its open-source/academic siblings. The popular framing is that these are a
"new six-layer memory system." The honest finding:

- **Our retrieval plumbing already matches or beats them** — hybrid BM25 + vector + RRF (k=60),
  a Qwen3 cross-encoder reranker, pgvector **HNSW** (vs their flat Chroma), a real entity graph,
  decay/consolidation/pruning, outcome-driven invalidation, drift detection, and true multi-tenant
  isolation. We are *not* outdated on infrastructure.
- **We are behind on cognitive layers** — the parts these systems actually sell (L3–L6). Four
  genuine gaps justify work; everything else is parity or better.

### Reference systems (separated, because rip-value is uneven)

| System | What it is | Rip value |
|---|---|---|
| **hy-memory.com** (Tencent Hunyuan) | OpenClaw plugin, 6-layer "cognitive kernel," Chroma + bge-m3, Python sidecar on `:19527`, sync recall + async capture | **Concepts only** (L4–L6 "Mind & Intent"). Install requires `--dangerously-force-unsafe-install` ⚠️ — do not run the package. |
| **Tencent/TencentDB-Agent-Memory** | The open-source sibling. 4-tier local pipeline, **symbolic short-term memory** (tool logs → Mermaid + `node_id` refs, −61% tokens), BM25+vector+RRF, SQLite+sqlite-vec | **High — concrete code & config keys.** MIT. |
| **MIRIX** (arXiv 2507.07957) | The academic 6-memory-type design: Core / Episodic / Semantic / Procedural / Resource / **Knowledge Vault** + a "Meta Memory Manager" | **High — Vault schema is concrete; the Meta Manager is largely aspirational in their docs.** |

### Measured results worth chasing (their numbers, our motivation)

- Symbolic short-term compaction: WideSearch tokens **221M → 86M (−61%)**, success **33% → 50%**.
- SWE-bench long sessions: tokens **−33%**, success **58.4% → 64.2%**.
- PersonaMem long-term accuracy: **48% → 76%**.

We cannot currently reproduce or refute these because **we have no memory benchmark** — only agent
benchmarks. Phase 0 fixes that.

---

## 2. The four gaps (and the meta-gap)

| # | Gap | Source idea | Our current state | Value |
|---|---|---|---|---|
| **0** | No memory eval | LongMemEval / PersonaMem | We grade agents, never memory recall | Unblocks measuring 1–4 |
| **1** | No prospective / intent layer | hy-memory L4–L6 "Mind & Intent" | `session_goal.py` is per-run & engineering-evidence-gated; nothing models standing operator intent | **Highest product value** |
| **2** | No verbatim Knowledge Vault | MIRIX Knowledge Vault | All facts pass through LLM extraction → paraphrased; exact numbers/IDs can drift | **Correctness** (we handle CRM contact data) |
| **3** | Tool-log compaction is dumb | TencentDB symbolic short-term | `session.py` offloads big outputs to temp files, but injects no structured symbol graph | **Cost** (ties into runner refactor) |
| **4** | `search_memory` fans out to everything | MIRIX Meta Memory Manager | Handler hardcodes `expand_entities+insights+episodes=True` every call | Latency/cost + cleaner architecture |

---

## 3. Design principles (non-negotiable)

1. **Additive, not a rewrite.** The foundation is sound. Every phase adds a layer or refines a hot
   path; none replaces `memory_facts`, pgvector, or the hybrid retriever.
2. **Gated behind feature flags**, reusing `robothor/engine/feature_flags.py`. New work takes
   **RIP numbers 12–15** so it inherits the global panic switch (`ROBOTHOR_DISABLE_ALL_RIPS=1`) and
   the observe/alert/enforce mode infra. Descriptive aliases documented per phase.
3. **TDD — RED → GREEN → REFACTOR** per CLAUDE.md rule 8. Failing test first, every phase.
4. **No regressions on the plumbing we already win on.** Do **not** adopt: the Python sidecar/HTTP
   hop (`:19527`), Chroma, sqlite-vec, bge-m3, or a Hunyuan model dependency. We stay in-process
   async Python on Postgres + local Ollama.
5. **Platform/instance discipline** (CLAUDE.md). All new tables are `tenant_id`-scoped with FK to
   `crm_tenants`. The Vault holds **instance data** — it must never leak into platform code, fixtures,
   or git. No personal data in tests (use Alice/Bob/`agent@example.com`).
6. **DB access via `get_connection()`** from `robothor/db/connection.py` (ThreadedConnectionPool),
   `RealDictCursor`, explicit `conn.commit()`. Async memory modules wrap sync DB in `asyncio.to_thread`
   where they already do.
7. **Migrations are forward-only**, next free number is **071**. Naming `NNN_slug.sql`.

---

## 4. Phase 0 — Memory evaluation harness (prerequisite)

**Goal:** be able to prove any later phase helped. ~½ day. No flag (test infra).

- New `robothor/memory/eval.py`: a recall harness distinct from the agent benchmark runner
  (`robothor/engine/tools/handlers/benchmark.py`), because memory eval is *seed facts → query →
  check recall@k / exactness*, not *prompt → agent → pattern match*.
- New suite `docs/benchmarks/memory/suite.yaml` with case types:
  - `recall@k` — seed N facts, query, assert the gold fact is in top-k.
  - `temporal` — "what did I decide about X *most recently*" (supersession correctness).
  - `verbatim` — exact-string retrieval (drives Phase 2 acceptance).
  - `persona` — multi-session preference consistency (PersonaMem-style).
- De-personalized fixtures only (Alice/Bob/FakeVendorCo), per the benchmark-sandbox precedent.
- Emit a baseline report (`recall@5`, exact-match rate, mean latency, tokens/query) before Phase 1.

**Acceptance:** baseline numbers committed; harness runnable via a CLI subcommand and in CI (cheap,
local Ollama only, no paid models).

---

## 5. Phase 1 — Knowledge Vault (verbatim store)  ·  RIP 12

**Problem:** `store_fact()` runs every input through LLM extraction, which paraphrases. Safe for
"Philip prefers concise summaries"; unsafe for an account number, a contact's exact phone, a
routing reference, a license key. MIRIX's Vault preserves critical strings byte-exact.

**Relationship to SOPS:** the Vault is **not** a secret store and does not replace SOPS. SOPS owns
runtime secrets (decrypted to tmpfs). The Vault owns *verbatim reference data the agent must recall
exactly* — contact identifiers, account/case numbers, addresses, bookmarks — with a `high`
sensitivity tier that is encrypted at rest for the rare credential-like entry.

### Schema — migration `071_memory_vault.sql`

```sql
CREATE TABLE memory_vault (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES crm_tenants(id),
  entry_type    TEXT NOT NULL,        -- contact_info | account_id | address | bookmark | credential | api_key
  caption       TEXT NOT NULL,        -- human description, embedded for retrieval
  value_exact   TEXT,                 -- verbatim, NULL when encrypted
  value_enc     BYTEA,                -- set iff sensitivity='high' (encrypted at rest)
  sensitivity   TEXT NOT NULL DEFAULT 'medium',  -- low | medium | high
  source        TEXT,                 -- user_provided | crm | email | ...
  entity_id     INTEGER REFERENCES memory_entities(id),
  person_id     UUID REFERENCES crm_people(id) ON DELETE SET NULL,
  caption_embedding vector(1024),     -- search on caption, never on the secret
  metadata      JSONB DEFAULT '{}'::jsonb,
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ( (value_exact IS NOT NULL) <> (value_enc IS NOT NULL) )
);
CREATE INDEX idx_vault_tenant      ON memory_vault (tenant_id) WHERE is_active;
CREATE INDEX idx_vault_type        ON memory_vault (tenant_id, entry_type) WHERE is_active;
CREATE INDEX idx_vault_emb         ON memory_vault USING hnsw (caption_embedding vector_cosine_ops)
                                     WITH (m=16, ef_construction=200);
CREATE UNIQUE INDEX idx_vault_dedup ON memory_vault (tenant_id, entry_type, md5(caption)) WHERE is_active;
```

**Key design choices**
- **Search is on `caption_embedding` only** — the secret value is never embedded (no leak into the
  vector store / logs). You find an entry by its description, then read the exact value.
- **`high` sensitivity ⇒ `value_enc`**, encrypted at rest with a key sourced from the existing SOPS
  pipeline (env-injected, never in git). `low`/`medium` ⇒ plaintext `value_exact`. The `CHECK`
  enforces exactly one is set.
- Access to `high` entries is logged (reuse `fact_access_log` pattern → a `vault_access_log` or a
  `metadata` audit append).

### Module & tools
- `robothor/memory/vault.py`: `store_vault_entry()`, `search_vault(query, entry_type=None)`,
  `get_vault_value(id)` (decrypts on read, audited), `list_vault(entry_type)`, `deactivate_entry()`.
- New engine tools in `robothor/engine/tools/handlers/memory.py`: `vault_store`, `vault_search`,
  `vault_get`. Registered in `robothor/api/mcp.py` schemas + `registry.py`.
- **Retrieval integration:** the router (Phase 4) queries the vault when the query looks like an
  exact-lookup ("what's X's account number / phone / address"). Until Phase 4 ships, `search_memory`
  gets an opt-in `include_vault` flag (default off) returning caption matches only; exact values
  require an explicit `vault_get`.

**Tests (RED first):** verbatim round-trip (no paraphrase), `high` entry never returned in plaintext
by search, encryption-at-rest (DB row has no plaintext), tenant isolation, dedup, audit log on
`vault_get`. Phase-0 `verbatim` suite must pass with Vault on.

**Flag:** `ROBOTHOR_RIP_12_ENABLED` (alias `ROBOTHOR_MEMORY_VAULT_ENABLED`). Off → tools hidden,
table inert.

---

## 6. Phase 2 — Symbolic short-term compaction  ·  RIP 13

**Problem:** long tool-heavy runs blow the context budget. We already offload large tool outputs to
temp files (`session.py` `_offload_tool_result`, `_tool_offload_threshold`), but we inject only a
truncated blob + path. TencentDB's win is injecting a **dense symbol graph** of task state instead,
with `node_id`s the agent greps to retrieve full raw text on demand (−61% tokens, +success).

**This is an enhancement of existing offload, not a new subsystem** — and it dovetails with the
in-flight `runner.py` god-object refactor.

### Mechanism (ripped, adapted to our stack)
- Keep offloading full tool output to `refs/<run_id>/<node_id>.md` (we already write temp files;
  formalize the path + retention).
- Maintain a per-run **Mermaid task-state graph** (`flowchart`) where each node is a tool
  call/result keyed by `node_id`, edges are causal/temporal. Inject only the graph + captions into
  context, capped at a token ratio of the window.
- The agent reasons over the graph; to see a full result it calls a `recall_node(node_id)` tool (or
  greps the ref file) — "drill down on demand."

### Triggers (ratio-based, ported from their config)
| Our key | Their key | Default |
|---|---|---|
| `MEMORY_OFFLOAD_MILD_RATIO` | `offload.mildOffloadRatio` | 0.5 (start compacting at 50% window) |
| `MEMORY_OFFLOAD_AGGRESSIVE_RATIO` | `offload.aggressiveCompressRatio` | 0.85 |
| `MEMORY_MMD_MAX_TOKEN_RATIO` | `offload.mmdMaxTokenRatio` | 0.2 (graph ≤20% of budget) |

### Hook points
- `robothor/engine/session.py` `record_tool_call()` (≈249–320): where outputs enter history — emit
  a graph node alongside the existing offload.
- `robothor/engine/runner.py` loop (≈1900–1945): assemble the symbol graph into the next iteration's
  messages when ratios trip; add the `recall_node` tool.
- New `robothor/engine/symbolic_memory.py` (or a module under the runner-refactor structure) holding
  graph build/serialize + node store.

**Tests:** graph stays under `mmdMaxTokenRatio`; `recall_node` returns byte-exact original; a
synthetic 40-tool run shows measured token reduction vs flag-off; no behavior change when flag off.

**Flag:** `ROBOTHOR_RIP_13_ENABLED` (alias `ROBOTHOR_MEMORY_SYMBOLIC_ENABLED`). Roll out in
**observe** mode first (build graph, log token delta, but still inject old format) before enforcing.

**Coordination:** land *after* the runner refactor PR to avoid conflicts on `session.py`/`runner.py`.

---

## 7. Phase 3 — Prospective / Intent memory  ·  RIP 14

**Problem:** everything we store is retrospective. hy-memory's L4–L6 sells *prospective intent* —
what the operator is trying to do next, modeled and advanced across sessions. `session_goal.py`
exists but is per-run and gated on engineering evidence (requires a `test_run` + `commit` to
complete) — wrong shape for standing business intents like "grow Valhalla revenue" or "reduce ops
toil." Build a parallel store; don't overload the goal system.

### Schema — migration `072_memory_intents.sql`

```sql
CREATE TABLE memory_intents (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES crm_tenants(id),
  title         TEXT NOT NULL,
  description   TEXT NOT NULL,
  horizon       TEXT NOT NULL DEFAULT 'ongoing',  -- ongoing | this_quarter | this_week | dated
  due_at        TIMESTAMPTZ,
  status        TEXT NOT NULL DEFAULT 'active',    -- active | dormant | achieved | dropped
  priority      SMALLINT NOT NULL DEFAULT 3,       -- 1 high .. 5 low
  source        TEXT,                              -- stated | inferred
  confidence    FLOAT8 DEFAULT 0.5,                -- for inferred intents
  embedding     vector(1024),
  linked_goal_ids   INTEGER[],   -- crm_tasks session-goals advancing this intent
  linked_fact_ids   INTEGER[],
  last_advanced_at  TIMESTAMPTZ,
  metadata      JSONB DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_intents_active ON memory_intents (tenant_id, priority) WHERE status='active';
CREATE INDEX idx_intents_emb    ON memory_intents USING hnsw (embedding vector_cosine_ops)
                                  WITH (m=16, ef_construction=200);
```

### Behavior
- `robothor/memory/intents.py`: `upsert_intent()`, `infer_intents_from_facts()` (nightly LLM pass
  over recent facts/episodes → proposes `inferred` intents at low confidence for confirmation),
  `mark_advanced()`, `link_goal()`, `search_intents()`.
- **Warmup injection:** extend `robothor/engine/warmup.py` (`build_warmth_preamble`, the `agent_goal`
  section at ~173) with an `active_intents` section — top-priority active intents, ≤600 chars,
  so the main agent's heartbeat/drain cycle can proactively advance them (ties into the scout/drain
  supervisor architecture).
- **Closing the loop:** when a `session_goal` completes, attribute it to a linked intent and bump
  `last_advanced_at`; intents untouched for a long window go `dormant` and surface for review.
- Inferred intents are **proposal-only** until confirmed (reuse the Delphi HMAC-gated approve/reject
  pattern via Telegram) — never let the agent invent and act on goals unsupervised.

**Tests:** stated intent persists across sessions and appears in warmup; inferred intents stay
proposal-only; goal→intent attribution updates `last_advanced_at`; dormancy transition; tenant
isolation; Phase-0 `persona`/multi-session suite improves.

**Flag:** `ROBOTHOR_RIP_14_ENABLED` (alias `ROBOTHOR_MEMORY_INTENT_ENABLED`).

---

## 8. Phase 4 — Memory router (replace the flag-soup)  ·  RIP 15

**Problem:** `search_facts()` carries 8 optional flags, and the `search_memory` handler hardcodes
`expand_entities=True, include_insights=True, include_episodes=True` on **every** call — so a simple
lookup pays for a full fan-out. With Vault + Intents added, the flag count only grows.

**Honest scope note:** MIRIX markets a "Meta Memory Manager" but its public docs don't specify the
routing algorithm. So this phase is a *concrete refactor we'd want anyway*, not a faithful port: a
lightweight query classifier that picks which stores to hit.

### Design
- New `robothor/memory/router.py`: `recall(query, tenant_id, budget=...) -> RecallResult`.
  1. Classify the query cheaply (heuristics first, optional small-LLM fallback):
     `exact_lookup` → Vault + facts; `temporal/episodic` → episodes + facts; `how_to` → procedures;
     `who_is` → entity graph + facts; `intent/planning` → intents; `default` → facts + insights.
  2. Query only the selected stores (in parallel), then RRF-merge into one ranked list with the
     existing reranker as the final precision pass.
  3. Respect a char/token budget (their `recall.maxTotalRecallChars` idea) so recall never blows the
     prompt.
- `search_facts()` keeps its signature (back-compat) but the `search_memory` **tool** routes through
  `router.recall()`. Old flags become internal.

**Tests:** each query class hits the expected store set (assert on a spy); exact-lookup no longer
fans out to episodes/insights; latency/token drop vs the hardcoded-fan-out baseline on Phase-0
suite; recall@k does not regress.

**Flag:** `ROBOTHOR_RIP_15_ENABLED` (alias `ROBOTHOR_MEMORY_ROUTER_ENABLED`); off → current behavior.

---

## 9. Explicit non-goals

- ❌ No Python sidecar / HTTP memory server (`:19527`). We're in-process async.
- ❌ No Chroma / sqlite-vec / bge-m3 swap. pgvector+HNSW+Qwen3 is equal-or-better and already wired.
- ❌ No Hunyuan model dependency, no running the `hy-memory` package (unsafe install flag).
- ❌ No new tier tables resurrecting short_term/long_term (dropped in migration 023 deliberately).

---

## 10. Sequencing, migrations, flags

| Phase | Flag (RIP) | Migration | Depends on | Rough effort |
|---|---|---|---|---|
| 0 Eval harness | — | — | — | ~0.5 day |
| 1 Knowledge Vault | RIP 12 | 071 | 0 | ~1.5 days |
| 2 Symbolic compaction | RIP 13 | — (file refs) | runner refactor PR | ~2–3 days |
| 3 Intent memory | RIP 14 | 072 | 0 | ~2 days |
| 4 Memory router | RIP 15 | — | 1 & 3 (to route into them) | ~1.5 days |

**Rollout order:** 0 → 1 → 3 → 4, with 2 slotted whenever the runner refactor lands. Each flag ships
**off**, validated against the Phase-0 suite, then enabled in observe mode where applicable before
enforce. Global kill switch `ROBOTHOR_DISABLE_ALL_RIPS=1` covers all four.

**Doc updates required on each phase** (CLAUDE.md rule 9, `docs/DOC_MAINTENANCE.md`):
`docs/memory-system.md`, `brain/memory_system/MEMORY_SYSTEM.md` (instance), schema notes, and the
`/model`/tool inventories where new tools are added.

---

## 11. Risks & mitigations

- **Vault leakage** (worst case): a verbatim value lands in logs/embeddings/git. Mitigation: embed
  only captions; `high` tier encrypted at rest; audited reads; platform/instance review; secrets
  scanner already in pre-commit (gitleaks).
- **Symbolic graph confuses the model**: it reasons over symbols and hallucinates detail.
  Mitigation: observe mode first, `recall_node` for ground truth, token-delta + success metrics
  gate enforce.
- **Inferred intents go rogue**: agent invents goals and acts. Mitigation: proposal-only +
  HMAC-gated confirmation; never auto-active.
- **Router degrades recall** by skipping a store. Mitigation: `default` class is the current
  fan-out; classifier biases toward including facts; Phase-0 recall@k is the gate.
- **Merge conflicts with runner refactor**: Phase 2 touches the same hot files. Mitigation: land
  Phase 2 last/after that PR.

---

## 12. Open questions for the operator

1. **Vault encryption key source** — reuse the SOPS-injected key, or a dedicated vault key? (Affects
   migration 071 + key rotation story.)
2. **Intent confirmation channel** — Telegram approve/reject like Delphi proposals, or auto-accept
   `stated` intents and only gate `inferred` ones? (Plan assumes the latter.)
3. **Phase 2 vs runner refactor** — block Phase 2 on that PR merging, or develop in parallel on a
   shared branch?
4. **Scope cut** — if time-boxed, the highest-ROI pair is **Phase 1 (Vault) + Phase 2 (compaction)**:
   correctness + cost, lowest design risk. Phases 3–4 are higher value but more design surface.
