# Memory and knowledge graph design

Status: approved design. Implementation is tracked story by story; this page is the reference those stories argue from.

## Intent

Genus OS memory is modelled on human memory: working, short-term and long-term; episodic, semantic and procedural. Most tiers exist. What has been missing is association, the thing that makes one memory bring another to mind. This design makes a knowledge graph the associative backbone of memory, unifies recall into one system, and sets the rule that every part must prove itself by measurement.

The guiding idea is that this is a brain, not a database:

- **Recall comes to mind.** Working on a person, a company or a task activates what is connected to it and brings it into context without the agent asking. Deliberate tool use is the secondary route.
- **Experience is kept; meaning is derived.** Raw episodes are never lost and are recalled directly. Facts, entities and edges are an index into them.
- **Outcomes teach.** Memories and procedures used in runs that succeeded get stronger; those behind failures weaken.
- **Idle time consolidates** as hygiene: resolving identities, removing duplicates, linking. It never rewrites what was experienced.
- **Beliefs update, they are not overwritten,** and the system knows what it does not know.

The graph is reached only by the model. There is no graph view.

## Goals

- One recall system for conversations, documents and CRM records.
- Reliable identity: one node per person or organisation, whatever name was used, never mixed with someone else.
- Associative, multi-step recall that arrives unasked and within a latency budget.
- Time-aware memory: what is true now, what was true then, and when the system learned it.
- Memory that resists poisoning, respects tenant and person scope, and can forget completely.
- Proof: a significant gain on our own evaluation, at equal context size, against simple baselines.

## Non-goals

- A visible graph or any dashboard view of it.
- A separate graph database, or a graph query language written by the model.
- Precomputed community summaries.
- Rewriting memories when they are recalled, or continuous model-driven consolidation.
- Strengthening memories merely because they were retrieved together.
- Parametric or latent memory.

## Anti-goals

The design has failed, even if it works technically, when any of these is true:

- A fact about one person becomes visible to, or attributed to, another.
- One tenant's data shapes or enters another tenant's memory.
- Content from an untrusted source reaches an agent's context automatically.
- A layer is live without evidence that it beats the simpler alternative.

## Where the system stands

- **Graph.** `memory_entities` and `memory_relations` exist and are tenant-scoped. Entities are written by exact match on name and type; lookup is case-insensitive on name and picks the best-connected row. Edges carry no time and no lifecycle and have a single source fact. Facts link to entities by name string. Traversal is one hop. The graph is written only by the content-ingest path and by nightly inference; memories stored by the agent tool, documents and CRM records never reach it.
- **Recall.** Dense and keyword search fused by reciprocal rank, then a yes/no reranker. Query understanding is limited to an optional regex classifier in `robothor/memory/router.py`. There is no time filter. Validity columns are written and never read. A dense-only search over verbatim chat turns exists but no caller enables it.
- **Several retrieval paths.** Memory recall in `robothor/memory/facts.py`; a separate document knowledge base in `robothor/knowledge/`; and an older orchestrator pipeline in `robothor/rag/pipeline.py`.
- **Lifecycle.** Access is logged per run and failed runs penalise the facts they used, but the access counters that decay and pruning read are never updated, so a memory in regular use fades as if it were unused.
- **Evaluation.** `robothor/memory/eval.py` is a careful harness with significance tests, but it measures retrieval over a handful of seeded facts per case and cannot exercise a graph, identity, or long histories.

## What the evidence says

The design was reviewed against current agent-memory systems and research. The conclusions that shaped it:

- A graph helps on multi-step, relationship and change-over-time questions, and on long histories. It adds little to simple lookup, and on short conversational memory the gain from a graph walk is small.
- Recalling raw, topic-segmented conversation with good ranking outperforms recalling only extracted facts, because extraction loses detail. Ranking, not structure, is usually the bottleneck.
- The designs that hold up keep raw episodes, extract off the request path, and invalidate instead of deleting.
- Extraction quality and entity resolution cap the result more than the retrieval algorithm does.
- Agent-driven, multi-step recall is strong on hard questions.
- Published benchmark numbers are mostly self-reported and not comparable. Claims have to be earned on our own evaluation with baselines run in the same harness.

## Principles

1. Nothing becomes the live path without a significant gain on our evaluation, at equal context size, against simple baselines.
2. One recall system. Conversations, documents and CRM records are sources in the same memory.
3. The graph is one signal among several. Answers come from sources.
4. Plain Postgres. Tenant isolation is enforced in the database on every new table.
5. Trust follows origin. Lower-authority content cannot override or outrank higher.
6. Proven behaviour becomes the only code path. Existing switches are removed one at a time, each with its own evidence.

## Architecture

```
sources: chat turns · email · CRM rows · documents      kept verbatim, segmented by topic, recalled directly
   │ derived_from (lineage on every derived row)
   ▼
facts (event time + recorded time, origin, scope)
   │ mentions = resolution decisions (surface form → node, method, decision)
   ▼
entities (canonical; CRM-seeded) ──edges (typed, both time axes, origin, many-fact evidence)──► entities
   │
   ├─ current-state store: one live value per single-valued slot
   └─ entity observations: evidence-linked, rebuilt when evidence changes (a measured candidate)

recall = dense + keyword + raw segments + graph activation
         → rerank → reserved places for graph candidates → blend → abstain when evidence is weak
```

### One recall core

- **Legs.** Dense, keyword, raw-conversation segments and graph activation are fused under one contract. A leg returns candidates with its own rank and score; fusion handles any number of legs.
- **Raw segments.** Conversations are segmented by topic when ingested and indexed for dense and keyword search. A segment names its source and turn range.
- **Documents.** Documents become sources in the same memory: structure-aware chunks with stable identity and their title and section as headers. The separate knowledge-base search becomes a filter over the one recall.
- **The older pipeline.** The orchestrator's query pipeline (`robothor/rag/pipeline.py` and the profile, search and context modules beside it) is retired once its endpoints are confirmed unused. The reranker and web search in the same package stay.
- **Ranking.** A graded reranker score on one relevance scale; candidates are never re-sorted by vector similarity; context is cut where scores fall away.
- **Reserved places.** A multi-step memory is, by definition, not similar to the question, so a question-document reranker will discard it. Candidates from the graph leg keep a reserved number of places after reranking, chosen by the leg's own score.
- **Time.** Time expressions in a question become filters. A point-in-time argument reaches every leg.
- **Abstention.** When evidence is weak or conflicting, recall says so.
- **Agent-driven recall.** A bounded search, read-the-source, follow-up loop is offered as a tool for hard questions.

### Identity

Identity is the part that caps quality, and a wrong merge is a privacy failure, so it is conservative by design.

- Nodes live in `memory_entities`. A CRM person or company is the canonical node for that person or company, through a link on the entity row. Entities with no CRM record (projects, places, events, people not in the CRM) are ordinary nodes. Memory never writes to the CRM.
- One extraction emits facts with typed mentions and relations per fact. Every way of storing a memory uses the same write path.
- A mention resolves automatically only on a strong key (a CRM id, an email address, a channel identifier) or a full name with supporting context (the same organisation, email domain or conversation as existing evidence for that node). A single-token name never merges automatically; it stays an unresolved mention until context resolves it.
- A merge never rewrites ids. Each mention records its surface form, the node it resolved to, the method and the decision. Reversing a decision re-resolves its mentions and marks derived rows for rebuild.
- Bringing existing data into the new model is a resumable background job with a mode that reports what it would do before doing it. It is not a migration.

### Edges

- Two time axes: when the relation held in the world, and when the system recorded and stopped believing it.
- A retirement reason: ended, refuted, or evidence merged.
- Many supporting facts per edge, an origin, and a canonical predicate beside the original phrase.
- An edge closes only on high-confidence evidence of change about the same resolved entity. Otherwise both stay open and the conflict is recorded.
- Consolidation moves evidence to the consolidated fact. Pruning and deduplication never close an edge.

### Graph activation

- Bounded Personalized PageRank from the entities in the current context, computed in a database function that enforces tenant and scope itself. There is no per-process cache to invalidate.
- The walk itself is restricted to what the reader can see, not only its results: it crosses only edges with at least one supporting fact visible to the reader, and degree weights count only those edges. A candidate's score never depends on a path through evidence the reader cannot see.
- Hub control: the operator's own node is excluded as a seed and as a transit node, seeds are weighted by inverse degree, and fan-out is capped.
- Automatic recall fires once per inbound message or scheduled run, in warm-up. Seeds are entities matched in the message by strong key or exact full name, and the entities the run is about (the sender's contact, the task's subject).
- Automatic recall uses only evidence that is operator-confirmed, user-stated or from the CRM. Tool output, agent-generated and external content are reachable by a deliberate tool call, and are labelled.
- Warm-up has a latency budget set from the measured baseline, makes no model call, and on a hard timeout returns the recall core's result without the graph leg. The amount injected is capped, because excess context harms decisions.

### Trust and scope

- Authority order, highest first: operator-confirmed, user-stated, CRM or tool, agent-generated, external. Inferred edges rank below all of these.
- Mapping from today's data: a fact marked operator-confirmed is operator-confirmed; what a person says in a conversation turn is user-stated; CRM rows are CRM; tool results are tool; what the assistant says or writes to memory is agent-generated; inbound email, web pages and uploaded documents are external.
- Extraction is per role and per source. A derived row takes the authority of its least-trusted contributor, so paraphrasing untrusted content does not launder it.
- Lower authority cannot supersede higher, cannot seed activation, and earns no reinforcement.
- Scope is carried on facts, entities, edges and mentions from the start: shared within the tenant, private to one person (today's person scope), or private to one agent (new). An edge is visible to a reader who can see at least one of its supporting facts.
- A derived row takes the narrowest scope among its inputs, as it takes the lowest authority. A consolidated fact, an entity observation or an insight built from any private fact is private to the same reader; where both shared and private evidence exist, the row is derived separately for each scope.
- The state of an edge is likewise per reader. Its time bounds and retirement are computed from the supporting and contradicting facts that reader can see, so a private fact that ends or refutes a relation changes nothing for a reader who cannot see that fact.

### Learning

- Recall records access. A memory counts as used when the answer cites it. A run that ends without failure gives a smaller credit to what it recalled, and a failed run gives a penalty, as today.
- Utility rises and falls with outcomes and feeds ranking and decay. Repetition alone cannot raise a memory.
- Procedures are learned from successful and failed runs, linked into the graph, and recalled for similar work.
- What a run retained during compaction is written at run end as provenance-tagged candidates. It never overwrites.
- Every write decision is logged with its downstream outcome, so a learned memory policy can be trained later. The log is tenant data: it is tenant-scoped, covered by erasure, and never pooled across tenants.

### Forgetting

"Never lost" describes the system's own processing: consolidation, decay and supersession retire derived rows and never destroy a source. An explicit request to forget is different. It deletes the source and follows its lineage. Removed or rebuilt: facts; mentions and the resolution decisions that record their surface form; merges whose supporting context came from the erased source, which are re-resolved; edges and their evidence; segments; summaries, entity observations, insights and preferences; the write-decision log; and the existing conflict-decision audit rows in `memory_conflict_decisions`, which hold the text of the facts they compare.

## Approaches considered

1. **Graph-first retrieval** in the style of precomputed community summaries. Rejected: indexing cost scales with ingest volume, summaries go stale under constant updates, and the gain is confined to broad sense-making questions.
2. **A temporal knowledge graph in a dedicated graph database.** Rejected: a second store to operate and isolate per tenant, model-adjudicated writes on every ingest, and no independent evidence of an advantage at this scale. The useful ideas (two time axes, invalidate instead of delete, provenance) are adopted on Postgres.
3. **No structure: long context and agent search over raw history.** Not rejected outright. It is competitive on short histories and is one of the baselines the graph has to beat. It degrades on long tenure and cannot enforce identity, scope or trust.
4. **The chosen design:** raw episodes as a recall path, a passage-anchored entity graph searched by bounded activation and fused as one leg, CRM-anchored identity, and an evaluation that decides what goes live.

## Evaluation

The evaluation decides what becomes the live path.

- A persistent, generic synthetic world: hundreds of entities with alias collisions and a hub, thousands of dated facts with sources and origins, CRM rows, documents and long multi-session conversations, plus a second tenant that shares names with the first.
- An answer-and-judge arm, with the judge validated against planted wrong answers.
- Strata: multi-step, entity confusion, point-in-time, knowledge update, abstention, raw detail, documents, long tenure, tasks that depend on earlier sessions, poisoning, cross-tenant leakage, and erasure.
- Baselines in the same harness: full context, keyword only, raw verbatim recall, an observation-log memory, and an agent searching raw history. Public long-memory benchmarks and open competitor systems run through the same adapter.
- A development and a held-out split, repeated runs to set the noise floor, a stated minimum detectable effect per gate, and cost and latency in every report.
- The tracked corpus is generic. Any corpus drawn from real data belongs to the instance and is not tracked.

## Delivery

The work is built in passes. Each pass leaves the whole system working end to end.

- **Pass 1, thin and whole.** The safety fixes in full (listed under Gates), and the thinnest working version of every layer: the evaluation world and answer-and-judge arm, the raw-segment leg, the graph schema with CRM-seeded identity, a first graph activation, outcome-based strengthening, and the replay dry run.
- **Pass 2, make it right.** Ranking, resolution quality, edge lifecycle, trust rules, abstention, agent-driven recall, and documents folded into the one recall.
- **Pass 3, get ahead.** Procedural learning, entity observations, public benchmarks and competitor systems in our harness.

Gates decide what becomes the live path, not what gets built. A layer is built and tested as soon as its interfaces exist. It replaces today's behaviour only when its gate is met, and from then it is the only path.

### Dry runs

There is no switch in the request path. Dry runs are commands:

- The evaluation runs the full system in an isolated evaluation tenant against the synthetic world and every baseline.
- A replay command runs recent recall questions through today's recall and the new recall side by side and reports the differences, the cost and the latency. It writes nothing.
- Jobs that change data report what they would do before they do it.
- New tables are additive and are filled from the first pass, so dry runs exercise real data.

### How new code coexists with live code

- Writes go live as they land. New tables are additive, and nothing reads them until a reader is merged.
- A read path changes by a code change at its gate, not by a setting. Until then the new reader is exercised by the evaluation and the replay command.
- The existing environment switches on memory behaviour are each resolved the same way: measured, then made the only path or deleted.

### Gates

Terms used in the table:

- **Significant gain.** A paired McNemar test at p < 0.05 on the held-out split, using `robothor/memory/eval_stats.py`, with an effect larger than the measured run-to-run spread.
- **One deciding comparison per gate.** Each gate names a single primary comparison: our system against the previous live path, on the pooled cases of the strata the gate lists. Per-stratum and per-baseline results are reported but are not each tested at p < 0.05, so no correction for multiple comparisons is needed.
- **Hard strata.** Multi-step, entity confusion, point-in-time, knowledge update, abstention and raw detail.
- **No regression.** The existing promotion rule in `eval_stats.promotion_verdict` applies unchanged: any single stratum that regresses at p < 0.20 blocks the gate.
- **At least level with a baseline.** Non-inferiority with a margin of five percentage points of answer accuracy: the lower bound of the 95% interval on our accuracy minus the baseline's is above minus five points.
- **Lower latency.** p95 of end-to-end recall at or below the recorded p95 of the path being replaced, measured in the same run on the same hardware.
- **Equal context size.** The same token budget handed to the answering model.

The Measure workstream records the baselines and fixes the remaining numeric targets (resolution precision and recall, the warm-up budget) before the readers they govern are merged.

### Unsafe paths closed by the Safety workstream

The Safety gate is met when each of these has a test that fails before its fix and passes after:

- Nightly maintenance (importance scoring, decay, pruning, consolidation, insight discovery and deduplication, relation inference) reading across tenants and writing derived rows to a default tenant.
- Gap analysis and the density metrics reading across tenants.
- Memory entry points that drop the acting tenant and fall back to the default: the deep-reasoning helpers, the MCP memory tools, and the orchestrator's ingest, stats and query endpoints.
- Maintenance skipping child tenants when row-level security is on.
- Facts in use being pruned because access was never recorded.
- New facts carrying no person scope, because nothing writes it.
- Auxiliary recall stores (insights, episodes, chat turns) that are not filtered by person scope.
- Instance data in tracked files.

Poisoning and cross-scope leaks through the new graph are closed by construction in the Identity and graph and Associative recall workstreams, and are tested there.

| Workstream | Gate |
|---|---|
| Safety | An isolation test per unsafe path that fails before the fix and passes after |
| Measure | A recorded baseline on every stratum, for our system and for every baseline |
| Recall core | A significant gain on the hard strata, no regression, lower latency, and at least level with every simple baseline |
| Identity and graph | Numeric precision and recall targets for resolution; no cross-person exposure in the scope tests, including a private fact that closes or changes a shared edge, and a derived row built from private evidence |
| Associative recall | A significant gain on multi-step and long-tenure strata over the recall core and over the observation-log and agent-search baselines at equal context; planted untrusted content never reaches warm-up; a candidate reachable only through a private edge is not recalled for a reader who cannot see that edge. If this gate fails, the graph serves identity and current state only |
| Learning | Task-success and update strata improve; reinforcement cannot lift an irrelevant memory; erasure leaves no derived trace |

## Constraints

- Engine internals are asynchronous. No new blocking database or network call on the event loop.
- Migrations are additive and idempotent. A migration runs in one transaction while application pods wait, so no backfill, large index build or validating scan of a large table belongs in one.
- An older release must run unchanged against the migrated database.
- Every new tenant table carries a tenant column, a row-level-security policy and a tenant-move classification.
- Unit tests mock models. Fixtures are generic.

## Terms

- **Single-valued slot.** A relation that has one live value at a time for an entity, such as a person's current role at one organisation. Declared per predicate, never assumed.
- **Entity observation.** A short derived statement about one entity, linked to the facts it summarises and rebuilt when they change.
- **Observation-log memory.** A baseline in which a model keeps a running, compressed log of what happened and that log is the whole memory.
- **Abstention.** Recall returns its candidates with an explicit marker that the evidence is insufficient or conflicting, and the reason.

## Left to the builder

- Thresholds for segmentation, activation damping and fan-out, and the reserved-place count: start from sensible defaults and tune on the development split.
- The choice of embedding and reranking models: decided by a bake-off on the held-out split and by latency.
- The exact decay function once access and outcome signals are real.

## Later, each its own decision

- A parent tenant reading its children's graphs.
- Entity and edge types that a tenant can extend.
- A learned memory policy trained from the logged write decisions.
