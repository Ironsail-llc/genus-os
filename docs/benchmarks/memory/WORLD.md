# Memory Eval — The Synthetic World

The case-by-case suite (`suite.yaml`) seeds a few facts per case into an empty
tenant. Retrieval fetches about 30 candidates per leg, so in that corpus the
vector leg returns nearly everything, and the failures the memory rebuild is
aimed at never come up: questions that need two or three hops, two people
called Alice, one person who appears in half of all facts, and histories that
change over time.

The world is one generated corpus that is big enough for those failures to
happen. The generator is `robothor/memory/eval_world.py`. Seeding, cleanup and
the run are in `robothor/memory/eval_world_seed.py`.

## How to run

```sh
ROBOTHOR_TENANT_ID=memory-eval-world \
  python -m robothor.cli memory-eval --world --world-seed 1 --world-size full --split dev
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--world` | off | Use the generated world instead of `--suite` |
| `--world-seed` | `1` | Generator seed. The same seed always gives the same world |
| `--world-size` | `full` | `small` (unit tests, about 600 facts) or `full` (about 3,500 facts) |
| `--split` | `dev` | `dev`, `held_out` or `all` |
| `--tenant` | `memory-eval-world` | Primary world tenant, `memory-eval-<suffix>`. The second tenant is `<tenant>-shadow` |
| `--keep` | off | Skip cleanup, to look at the seeded rows (see below for removing them) |
| `--json` | off | Print the full report as JSON |

The run seeds the world once, sends each case's question through
`search_facts` (k = 5), scores it with the existing retrieval scorers, prints
results per stratum and then deletes every seeded row. The exit code is 0 when
the run completed and 3 when it could not run or scored nothing. There is no
pass-rate floor for the world yet; the suite's 0.90 floor would fail every run,
because `raw_detail` cannot pass through `search_facts`. `--record` has no effect
in world mode: the fleet reads the latest `memory` row in `benchmark_results`
with no suite filter, so a recorded world score would replace the suite's score.

The world never shares a tenant with the nightly suite. `memory-eval` belongs
to the suite (`robothor-memory-eval.service`). A world cleanup there would wipe
the suite's distractor corpus, and world rows left there would block the
suite's own cleanup, because `memory_relations.fact_id` references
`memory_facts` with no `ON DELETE`. So `seed_world`, `cleanup_world`,
`world_row_counts` and `run_world` raise `EvalPreconditionError` before any
database call when a tenant is `memory-eval` itself or is not
`memory-eval-<suffix>`. The CLI exits 3 for `--world --tenant memory-eval`. As
a second safeguard, the suite's cleanup now deletes the tenant's
`memory_relations` rows before its `memory_facts` rows.

The classic suite has the same guard. `run_suite`, `_cleanup_tenant` and the
CLI all call `robothor.memory.eval.require_eval_tenant`, which uses the one
predicate both modes share (`is_eval_tenant`: `memory-eval` or
`memory-eval-<suffix>`). They refuse any other tenant before the first
database call. `memory-eval --tenant <a real tenant>` exits 3 and leaves every
row in that tenant in place. The nightly unit's `memory-eval` is unaffected.

Rows left by `--keep` or by a killed run stay in the world tenants until the
next world run, which cleans up before it seeds. To remove them by hand:

```sh
ROBOTHOR_TENANT_ID=memory-eval-world python -c "from robothor.memory.eval_world_seed import cleanup_world; print(cleanup_world(['memory-eval-world', 'memory-eval-world-shadow']))"
```

Seeding never calls an LLM, because the world already contains its facts and
entities. The only model call is for embeddings: `seed_world(...,
embed_batch=...)` takes any `async (list[str]) -> list[list[float]]`, and
tests pass a deterministic fake.

The world scores recall against known answers. To see what a recall change
does to the questions a real tenant asked, which have no known answers, use
the read-only replay instead: [Replaying real questions before a change goes
live](../../memory-system.md#replaying-real-questions-before-a-change-goes-live).
It also runs against a seeded world tenant with `--questions` built from the
world's case questions.

## Determinism

`generate_world(seed, size)` depends only on its arguments. Each tenant has
one `random.Random` seeded with an integer. Calendar bounds are fixed
(2019-01-01 to 2026-06-30), the wall clock is never read, and the generator
never iterates over an unordered container. `world_to_json` sorts keys. The
tests check that two generations produce identical bytes, including under
different `PYTHONHASHSEED` values.

## What the world contains

| | small | full |
|---|---|---|
| People / organisations / projects / locations / events | 60 / 8 / 6 / 8 / 6 | 320 / 36 / 30 / 16 / 40 |
| Facts (of which superseded) | ~590 (~150) | ~3,560 (~1,220) |
| CRM people / companies | ~42 / 7 | ~218 / 23 |
| Documents (of which planted, untrusted) | 17 (3) | 78 (12) |
| Conversations / sessions / turns | 3 / ~170 / ~1,000 | 12 / ~920 / ~6,500 |
| `conversation_recall` conversations / sessions / turns | 6 / 24 / 192 | 40 / 160 / 1,280 |
| `conversation_buried` companies / facts / conversations / sessions | 4 / 400 / 4 / 12 | 50 / 5,000 / 50 / 150 |
| Transcript-only details | 24 | ~240 |
| Shadow tenant people (names shared with primary) | 16 (10) | 100 (~80) |

These counts are for seed 7 and vary slightly with the seed.

The world is built to be hard in these ways:

- **Identity.** First names repeat (50 first names across 320 people). A fact
  can refer to a person by full name, first name only, nickname, initials,
  email address or a lower-case variant. One organisation and one project
  share a name exactly. Full names are unique within a tenant.
- **One hub.** A single person, `World.hub_id`, standing in for the operator,
  is co-mentioned with at least `HUB_MIN_SHARE` (0.5) of every other entity.
  Every other person stays below half that share.
- **Change over time.** Jobs (one to four), homes (up to eight) and managers
  change. Each change supersedes the fact it replaces. Superseded facts are
  stored with `is_active = FALSE`, `superseded_by` set, and
  `valid_from`/`valid_to` taken from the world's calendar.
- **Provenance.** Every fact has an `origin` and a `source` that points at the
  conversation turn, document section or CRM row it came from.
- **Raw recall.** Some details (a dog's name, a parking bay, a desk, a locker)
  appear in a single transcript turn and in no fact or document.
- **Poisoning.** Planted untrusted documents (`trusted = false`, origin
  `external`) make false claims about employment or headquarters. The people
  and organisations they name are left out of the answerable strata, so a
  gold answer is never contradicted by a plant.
- **Leakage.** A second tenant reuses about 80 person names from the primary
  tenant. It has its own organisations and cities, so its answers can be told
  apart from the primary's.

All names come from the word lists in `eval_world.py`. All addresses are
`example.com` or a subdomain of it. The tests check both.

## Strata

Each stratum has at least 150 cases at full size, capped at 240 per stratum.

| Stratum | Question shape | Gold | Scored by |
|---------|----------------|------|-----------|
| `multi_hop` | Manager's manager; city of a person's employer; city of the employer of a project's lead (2 or 3 facts) | Name or city | Any gold in top-k |
| `entity_confusion` | Job title or manager of a person who shares a first name with someone else (also asked by email) | Role or name | Any gold in top-k |
| `point_in_time` | Employer on date D, where D falls inside a superseded job | Organisation | Any gold in top-k |
| `knowledge_update` | Current employer after at least one change | Organisation | Gold ranks first |
| `abstention` | Blood type, university, shoe size, favourite colour: never stated anywhere | `unknown` | Not scored by retrieval |
| `raw_detail` | A detail found only in a transcript turn | The detail | Any gold in top-k |
| `long_tenure` | Current home, with `history_length` from 1 to 8 | City | Gold ranks first |
| `cross_tenant` | Employer, city or manager of a person whose name also exists in the shadow tenant | Primary tenant's value | Any gold in top-k and no `forbidden` value in top-k |
| `multi_hop_paraphrase` | The `multi_hop` cases reworded (`PARAPHRASES`, one of three wordings per kind) so the question shares no content word with its supporting facts except the names | As `multi_hop` | Any gold in top-k |
| `conversation_recall` | A detail said only in a long multi-session conversation: `near` (beside the question's words in a long message), `far` (over 1,200 characters from them), `update` (changed in a later session) or `multi_session` (the lowest of three prices, one per session) | The detail | Any gold in top-k |
| `conversation_buried` | A detail said only in a company's conversation, while about a hundred extracted facts name the company: `near`, `far`, or `update` (the conversation changed a value an active fact still states) | The detail | Any gold in top-k |

Every `multi_hop` question shares its one rare word with the second-hop fact
("headquartered", "report to"), so a retriever that matches words can look
like one that follows links. `multi_hop_paraphrase` asks the same pairs without
that word; a twin takes the split and the per-stratum cap of the case it
rewords. `multi_hop` is kept as it was, so earlier numbers still compare; the
`hard` pool of the answer arm does not include the reworded stratum.

`raw_detail` cases cannot pass through `search_facts`, which only reads
`memory_facts`. They measure the gap that raw-conversation recall is meant to
close. Abstention cases are reported as `unscored` until the answer-and-judge
arm exists.

`conversation_recall` exists because the world's own transcripts are short:
one detail per exchange, so a `raw_detail` answer is in a segment of a few
hundred characters next to the question's words, and no question needed a
long segment, a later session or several sessions. Each of its conversations
(`cvr-01` ...) is a shop fit-out talked through over four sessions; each
session is one pasted note of about 1,500 characters and three short
exchanges, about 2,400 characters in all. Session 1 gives the dock door and a
shelving price, session 2 an order reference and a second price, session 3 a
gate code more than 1,200 characters after the words that ask for it, and
session 4 the new dock door and the third price. None of it is a fact: the
answers exist only as conversation, seeded as every world conversation is
(`save_exchange`, then `segments.index_conversation`). The stratum has its
own random stream and comes last in `STRATA`, so every other stratum's cases,
every fact and the original conversations are byte for byte what they were;
`test_eval_world_conversation_recall.py` pins their digests. Full size has
160 cases, 40 or more in each split, and it is not in the `hard` pool.

`conversation_buried` exists because in `conversation_recall` nothing else
names the shop, so its conversation reaches the context unopposed. In a real
history the subject of a question is usually named by many extracted facts,
and those facts, found by both fact legs, fill the reranker's head and the
context before any conversation. Each of its 50 companies (`org-cvb-01` ...,
names from word lists no other world text uses) is named by 100 facts: its
headquarters, the dock door deliveries go to, and 98 distinct deliveries,
invoices, inspections and meetings, no two near-duplicates by recall's own
rule. Its one conversation (`cvb-01` ...) has three sessions after every one
of those facts: an order reference beside the question's words, a gate code
more than 1,200 characters after them, and a new dock door that replaces the
one the active fact still states (so the old door is a rival, judged wrong).
Its own random stream, after `conversation_recall`, so every earlier case,
fact and conversation is byte for byte what it was (both test files pin
their digests). Full size has 150 cases, 40 or more in each split, and it is
not in the `hard` pool. On the base ranking, the answer was in the context
for 1 of the 40 dev cases.

## Split rule

`split_for(case_id)` returns `dev` when the first byte of
`sha256(case_id)` is even, and `held_out` otherwise. It depends only on the
case id, which has the form `<stratum>:<subject world id>:<variant>`, so a
case keeps its split across seeds, sizes and code changes for as long as its
id stays the same. The split is about 50/50, and the tests check that it stays
between 40% and 60%.

## Fixture schema (`WORLD_SCHEMA_VERSION = 3`)

All types are frozen dataclasses in `robothor/memory/eval_world.py`. Dates are
ISO strings (`YYYY-MM-DD`), turn times are ISO timestamps in UTC, and sequences
are tuples. World ids are stable for a given seed and size. Shadow-tenant ids
carry the prefix `sh-`. A change to any field is a schema change: bump
`WORLD_SCHEMA_VERSION` and update this page.

```text
World
  seed: int; size: "small"|"full"; tenant_key: "primary"|"shadow"
  hub_id: str | None                      # None in the shadow world
  people, organisations, projects, locations, events
  employments, residences, reporting_lines, memberships
  facts, crm_people, crm_companies, documents, conversations
  raw_details, planted_claims
  cases                                   # empty in the shadow world
  shadow: World | None                    # the second tenant
  conversation_recall                     # () in the shadow world (version 2)
  conversation_buried                     # () in the shadow world (version 3)

Person        id "per-0001", first_name, last_name, full_name, nickname | None,
              initials "A.V.", email "first.last@example.com", is_hub, has_crm_row
Organisation  id "org-001", name, domain "<root>.example.com", hq_location_id,
              hq_fact_id, has_crm_row
Project       id "prj-001", name, org_id, lead_id | None, start, fact_id
Location      id "loc-01", name
Event         id "evt-001", name, date, location_id, host_org_id, attendee_ids, fact_id
Employment    person_id, org_id, role, start, end | None (None = current), fact_id
Residence     person_id, location_id, start, end | None, fact_id
ReportingLine person_id, manager_id, start, end | None, fact_id
ProjectMembership person_id, project_id, start, fact_id

Fact
  id "fct-00001"; text; category (facts.VALID_CATEGORIES)
  mentions: (Mention(entity_id, surface), ...)   # surface is the exact text used
  event_date                                     # when it became true
  source: SourceRef(kind, container_id, segment_id)
      kind "conversation": container = conversation id, segment = turn id
      kind "document":     container = document id,     segment = section id
      kind "crm":          container = "crm",           segment = CRM row id
  origin: operator_confirmed | user_stated | crm | tool | agent_generated | external
  supersedes | None, superseded_by | None
  valid_from (= event_date), valid_to | None (= successor's event_date)

CrmPerson     id "crm-per-0001", person_id, first_name, last_name, email, job_title,
              company_org_id | None (None when the employer has no CRM row), city
CrmCompany    id "crm-org-001", org_id, name, domain, city
Document      id "doc-001" (planted: "doc-x01"), title, date, trusted, origin,
              sections: (DocumentSection(id "doc-001#s1", heading, body), ...)
Conversation  id "cnv-01", title, sessions:
              (ChatSession(id "cnv-01/s001", date, turns:
                  (Turn(id "cnv-01/s001/t001", role "user"|"assistant", text, at), ...)), ...)
              # turns alternate user, assistant
RawDetail     id "raw-0001", person_id, kind (pet|parking|desk|locker), value,
              turn_id, question
ConversationRecall id "cvr-01:far", conversation_id "cvr-01",
              kind (near|far|update|multi_session), question, gold, rivals
              (the superseded door; the two dearer suppliers), turn_ids
              # conversation_buried uses the same type: id "cvb-01:far",
              # kind (near|far|update), rivals (the door the fact still states)
PlantedClaim  id "clm-001", document_id, fact_id, subject_id,
              relation (works_at|headquartered_in), claimed_object, true_object

Case
  id "<stratum>:<subject>:<variant>"; stratum (STRATA); question
  gold: tuple[str, ...]          # any one is correct; ("unknown",) for abstention
  supporting_ids: tuple[str,...] # fact / turn / section / CRM row ids; () for abstention
  as_of: str | None              # point_in_time
  split: "dev" | "held_out"
  subject_id                     # world id the question is about
  confuser_id | None             # entity_confusion: same first name
  history_length | None          # long_tenure: how many homes so far
  forbidden: tuple[str, ...]     # cross_tenant: the shadow tenant's answer
  probe | None                   # abstention: the attribute that is never stated
```

## How a world maps onto the tables

| World | Table | Written by |
|-------|-------|------------|
| `facts` | `memory_facts` | `facts._insert_fact` (the row writer `store_facts_batch` uses), with the injected embedder. Then `created_at`, `valid_from`, `valid_to`, `superseded_by` and `is_active` are set from the world. `metadata.eval_world` holds the world fact id, origin, source and mentions. `entities` holds the surface forms |
| people, organisations, projects, locations, events | `memory_entities` | `entities.upsert_entity`. A person's aliases are their email, initials, lower-case name and nickname |
| relations | `memory_relations` | `entities.add_relations_batch`: `works_at`, `lives_in`, `reports_to`, `member_of`, `run_by`, `leads`, `headquartered_in`, `held_in`, `attended`, each linked to its fact |
| `crm_companies`, `crm_people` | `crm_companies`, `crm_people` (and `audit_log`) | `crm.dal.create_company`, `create_person` and `update_person` (job title, city, company) |
| `documents` | `kb_documents`, `kb_chunks` | `knowledge.store.upsert_document`, collection `memory-eval/world` |
| `conversations` | `chat_sessions`, `chat_messages` | `engine.chat_store.save_exchange`, one session per world session (`eval-world:<session id>`), then the turn timestamps and each turn's embedding (the content, as the engine embeds chat turns) |
| the tenants | `crm_tenants` | created if absent |

`cleanup_world([tenant, shadow])` deletes every row in all of those tables for
both tenants, children before parents, and removes the tenant row when nothing
else references it. The integration test in
`robothor/memory/tests/test_eval_world_seed.py` seeds a small world into newly
named eval tenants on real PostgreSQL. It checks that every table gained rows,
that every row belongs to those tenants, and that the row counts return to
their starting values after cleanup.

## Answer-and-judge mode (`--answers`)

Substring scoring cannot measure abstention, cannot compare against a
full-context baseline, and flatters a system that retrieves a lot. With
`--answers` every case is answered end to end:

```sh
ROBOTHOR_TENANT_ID=memory-eval-world python -m robothor.cli memory-eval --world --answers \
  --world-size full --split dev --limit-per-stratum 40 \
  --systems recall,recall_budget,keyword_only,raw_verbatim,full_context \
  --token-budget 2048 --answer-model qwen3:8b --judge-model qwen3:8b \
  --progress /tmp/memory-world.jsonl --report /tmp/memory-world.json
```

The baselines run by default through `--systems`; there is no separate
`--baselines` flag. `--reference` defaults to `keyword_only`. Answer mode runs in
`memory-eval-world`, like world mode.

1. **Retrieve.** Each system (`robothor/memory/eval_systems.py`) turns the case
   into a `ContextBundle`: ordered items with world ids and kinds, filled best
   first up to `--token-budget` (tokens are `block_budget.estimate_tokens`,
   chars / 4). An item that does not fit is skipped and a smaller later one may
   still go in. A bundle never exceeds its budget.
2. **Answer.** `eval_answer.answer` sends the bundle and the question with a
   fixed prompt: answer only from the context, say "unknown" when it does not
   hold the answer, cite the item ids. JSON output, temperature 0, no thinking.
   The answer and judge models always run on the local Ollama client
   (`--answer-model`, `--judge-model`, both defaulting to the configured
   generation model), and the report names them. The memory generation module
   ignores `model` on its remote path and falls back to local per call, so
   through it a run could be labelled with one model and answered by another.
   With remote memory generation enabled, both flags are required and the run
   exits 3 without them.
3. **Judge.** `eval_answer.judge` applies rules first, in this order:
   - an abstention case is correct only when the answer abstains;
   - "unknown" on an answerable case is wrong but not confident-wrong;
   - a forbidden (other-tenant) value is wrong, even next to the gold;
   - a normalised whole-word gold or alias is correct, unless the answer also
     names another known value of the gold's own kind (another organisation,
     city, person or role). "Kelvaro Systems or Brivane Labs" is a hedge, and
     "Brivane Labs (previously Kelvaro Systems)" states a superseded value as
     current. Both are confident-wrong by rule, as the rubric says an answer
     that hedges between several things is wrong. When hedges were left to
     the LLM judge, validation caught it accepting 55 of 150 planted hedges.
     "Kelvaro Systems, in Velmere" stays correct, and so does restating what
     the question is about: for a question about Alice Vance, "Alice Vance's
     manager is Bob Rivera" names one candidate, while "Bob Rivera or Carol
     Diaz" is a hedge;
   - any other value the world knows is confident-wrong, except a value the
     question itself names. Both rules share that exclusion, so "Alice
     Vance's manager is Rivera, Bob" goes to the LLM judge rather than being
     marked wrong for restating Alice Vance.

   Only an answer none of these settle goes to the LLM judge
   (`--judge-model`), whose prompt is fixed in the module.

The judge is validated before any case runs, on planted answers generated
from the world:
- **wrong:** the confuser's value, a superseded value, the other tenant's
  value, a random other entity, an answered abstention, a hedge (the gold
  beside a rival of its own kind), and a reordered name or organisation root
  that belongs to someone else. That last kind never shares a word with the
  gold or nearly spells one: invented roots such as Drodrel and Drotrel
  differ by a single letter;
- **right:** the gold in several sentence shapes, and the gold as a
  reordered name or a bare organisation root.

Only the LLM judge can settle the reordered and root forms, so validation
always exercises it. Each planted kind must clear 95% on its own,
in addition to the pooled wrong and right rates. Most planted answers are
settled by rules, so a pooled gate alone would let a weak model judge through.
If any rate falls short, or the judge raises on a planted case, the run aborts
with `judge failed validation`.

### Systems

| Name | What it retrieves |
|------|-------------------|
| `recall` | The product path: `search_facts` with whatever legs the checked-out code has (vector, BM25, reranker, ...), budget filled in rank order. What the product returns today: the reranker pool is capped at `MEMORY_RERANK_MAX_CANDIDATES` (16), so it fills about an eighth of a 2,048-token budget |
| `recall_budget` | The same product ranking as the head, then `search_facts`' own unreranked fused order (`use_reranker=False`) behind it until the budget is full. This shows what recall's retrieval holds at an equal budget. `search_facts` itself is unchanged; its legs fetch about 30 candidates each, which bounds the tail |
| `keyword_only` | Postgres full-text (`to_tsquery` OR over the question's words, `ts_rank_cd`) over the tenant's active facts. No vectors, no reranker |
| `raw_verbatim` | Raw transcript turns (dense `search_chat_turns` plus full-text over the turn content) and the seeded documents (`knowledge.search` over `memory-eval/world`), fused by reciprocal rank. No extracted facts, no reranker |
| `full_context` | No retrieval: the newest transcript turns that fit the budget, in chronological order |
| `full_context_unbounded` | The same, filled to 24,000 tokens. Reported with `equal_budget: false`, so it is never a fair comparison |

To add a system (for example an observation-log memory, or an agent that
searches raw history with tools), implement `MemorySystem` (`name`,
`equal_budget`, `async retrieve(case, *, world_handle, token_budget) ->
ContextBundle`) and call `register_system(name, factory)`. The runner, judge
and report need no change.

### Report

The JSON report (and a Markdown summary beside it) gives, per system and
stratum and pooled over `all` and the `hard` strata (multi_hop,
entity_confusion, point_in_time, knowledge_update, abstention, raw_detail):
- accuracy with a Wilson 95% interval, per case: with `--repeats`, a case
  counts once, scored by the majority of its repeats (ties go to the first).
  At temperature 0, repeats are near-copies and must not count as more cases;
- confident-wrong rate;
- abstention precision and recall;
- mean context tokens;
- retrieval p50/p95 and answer p50/p95 latency;
- run-to-run spread across `--repeats`;
- the minimum detectable effect at the stratum's n: two-sided alpha 0.05,
  power 0.8, `(z_a + z_b) * sqrt(2p(1-p)/n)`.

The deciding comparison pools the hard strata and compares the first system
in `--systems` with `--reference` (default `keyword_only`), one pair per case,
using the existing McNemar
`promotion_verdict` and a non-inferiority check: the lower bound of the 95%
interval on the paired accuracy difference must be above -5 points.

`--record-baseline` also writes `world-baseline.json` and
`world-baseline.md` here. Answer mode never writes `benchmark_results`.

### Embedding cache

Seeding the full world embeds about 3,500 facts, the document chunks and about
6,500 turns. Vectors are cached as float32 per (embedding model, world schema
version, seed, size) under `~/.cache/genus-os/memory-eval`. Override the
location with `--cache-dir`. A change to
any of the four keys reads a different file, so a stale vector is never
served.
