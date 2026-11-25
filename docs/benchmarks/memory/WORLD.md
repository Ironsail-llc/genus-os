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

`raw_detail` cases cannot pass through `search_facts`, which only reads
`memory_facts`. They measure the gap that raw-conversation recall is meant to
close. Abstention cases are reported as `unscored` until the answer-and-judge
arm exists.

## Split rule

`split_for(case_id)` returns `dev` when the first byte of
`sha256(case_id)` is even, and `held_out` otherwise. It depends only on the
case id, which has the form `<stratum>:<subject world id>:<variant>`, so a
case keeps its split across seeds, sizes and code changes for as long as its
id stays the same. The split is about 50/50, and the tests check that it stays
between 40% and 60%.

## Fixture schema (`WORLD_SCHEMA_VERSION = 1`)

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
ROBOTHOR_TENANT_ID=memory-eval python -m robothor.cli memory-eval --world --answers \
  --world-size full --split dev --limit-per-stratum 30 \
  --systems recall,keyword_only,raw_verbatim,full_context --reference full_context \
  --token-budget 2048 --repeats 3 --report /tmp/memory-world.json
```

1. **Retrieve.** Each system (`robothor/memory/eval_systems.py`) turns the case
   into a `ContextBundle`: ordered items with world ids and kinds, filled best
   first up to `--token-budget` (tokens are `block_budget.estimate_tokens`,
   chars / 4). An item that does not fit is skipped and a smaller later one may
   still go in. A bundle never exceeds its budget.
2. **Answer.** `eval_answer.answer` sends the bundle and the question to the
   generation module (`--answer-model`, default the configured generation
   model) with a fixed prompt: answer only from the context, say "unknown" when
   it does not hold the answer, cite the item ids. JSON output, temperature 0,
   no thinking.
3. **Judge.** `eval_answer.judge` applies rules first, in this order:
   - an abstention case is correct only when the answer abstains;
   - "unknown" on an answerable case is wrong but not confident-wrong;
   - a forbidden (other-tenant) value is wrong, even next to the gold;
   - a normalised whole-word gold or alias is correct;
   - any other value the world knows is confident-wrong.

   Only an answer none of these settle goes to the LLM judge
   (`--judge-model`), whose prompt is fixed in the module.

The judge is validated before any case runs, on planted answers generated
from the world:
- **wrong:** the confuser's value, a superseded value, the other tenant's
  value, a random other entity, an answered abstention, and a reordered name
  or organisation root that belongs to someone else;
- **right:** the gold in several sentence shapes, and the gold as a
  reordered name or a bare organisation root.

The reordered and root forms can only be settled by the LLM judge, so
validation always exercises it. If fewer than 95% of planted wrong answers
are marked wrong, or fewer than 95% of planted right answers are marked
right, the run aborts with `judge failed validation`.

### Systems

| Name | What it retrieves |
|------|-------------------|
| `recall` | The product path: `search_facts` with whatever legs the checked-out code has (vector, BM25, reranker, ...), budget filled in rank order |
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
- accuracy with a Wilson 95% interval;
- confident-wrong rate;
- abstention precision and recall;
- mean context tokens;
- retrieval p50/p95 and answer p50/p95 latency;
- run-to-run spread across `--repeats`;
- the minimum detectable effect at the stratum's n: two-sided alpha 0.05,
  power 0.8, `(z_a + z_b) * sqrt(2p(1-p)/n)`.

The deciding comparison pools the hard strata and compares the first system
in `--systems` with `--reference`, using the existing McNemar
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
