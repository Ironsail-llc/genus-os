# Memory Eval — Baseline

The memory eval runs in two modes. Both use only evaluation tenants
(`memory-eval` or `memory-eval-<suffix>`) and refuse any other tenant before
the first database call.

| Mode | Command | What it measures |
|------|---------|------------------|
| Suite | `memory-eval --suite docs/benchmarks/memory/suite.yaml` | 267 hand-checked and generated cases. Each seeds a few facts, runs `search_facts` and checks whether the gold string is in the top k. Runs nightly in `memory-eval` with `--record` (`robothor-memory-eval.service`) |
| World | `memory-eval --world [--answers]` | One generated world of about 3,500 facts, CRM rows, documents and transcripts, seeded once into `memory-eval-world` ([WORLD.md](WORLD.md)). Without `--answers` it uses the same substring-in-top-k scoring. With `--answers` a local model answers from each system's context at an equal token budget, and a validated judge grades the answers |

Neither mode replays the questions a real tenant asked. That is
`memory-replay`, a read-only side-by-side of two recall configurations with no
score: see [Replaying real questions before a change goes live](../../memory-system.md#replaying-real-questions-before-a-change-goes-live).

Exit codes for the suite: `0` met the floor, `2` ran and fell below it, `3`
could not run. World mode exits `0` once it has run and `3` if it could not;
there is no world floor yet. The suite needs `ROBOTHOR_TENANT_ID=memory-eval`
and the world needs `ROBOTHOR_TENANT_ID=memory-eval-world`. Without these,
row-level security (migration 081) rejects the seed writes, and `preflight()`
exits 3 with the fix printed.

The recorded world baseline is in [world-baseline.md](world-baseline.md). The
JSON report it comes from, minus per-case records, is
`docs/benchmarks/memory/world-baseline.json`.

## 2026-11-24: world baseline (local run)

Common parameters:
- **Code:** commit `159862d9a` on the answer-arm branch. Its retrieval code
  matches main's.
- **World:** seed 1, size `full`.
- **Database:** local PostgreSQL test database, shared with other work at the
  time, so latencies are higher than on an idle machine.
- **Models:** `qwen3-embedding:0.6b` for embeddings and
  `dengcao/Qwen3-Reranker-0.6B:F16` as the reranker in `search_facts`.

### Retrieval scoring (`--world`, substring in top 5)

These are today's retrieval numbers on main's recall code, measured as "the
gold string appears in a retrieved row". Dev split, 803 scored cases. The 118
abstention cases cannot be scored this way. Time: 705 s (seeding 201 s,
queries 496 s).

| Stratum | Passed |
|---------|--------|
| entity_confusion | 101/117 (86.3%) |
| cross_tenant | 66/79 (83.5%) |
| long_tenure | 82/126 (65.1%) |
| point_in_time | 62/126 (49.2%) |
| knowledge_update | 27/119 (22.7%) |
| multi_hop | 19/126 (15.1%) |
| raw_detail | 0/110 (0%) |
| **Total** | **357/803** |

### Answer-and-judge comparison (`--world --answers`)

Run parameters:

| Parameter | Value |
|-----------|-------|
| Split | dev |
| Cases | at most 40 per stratum (`--limit-per-stratum 40`), 320 in total |
| Systems | `recall`, `keyword_only`, `raw_verbatim`, `full_context` |
| Token budget | 2,048 |
| Repeats | 1 |
| Answer model | `qwen3:8b`, temperature 0, no thinking |
| LLM judge | `qwen3:8b` (27 of 1,280 verdicts needed it; the rest were settled by rules) |
| Concurrency | 4 |
| Time | 2,026 s (seeding 197 s, answering 1,637 s) |
| Errors | none |

Judge validation passed before any case ran. 900 of 900 planted wrong answers
were marked wrong (100%), and 448 of 450 planted correct answers were marked
right (99.6%).

Accuracy with Wilson 95% intervals. CW is the confident-wrong rate: answered,
and wrong. Tokens is the mean context per case.

| Stratum | recall | keyword_only | raw_verbatim | full_context |
|---------|--------|--------------|--------------|--------------|
| abstention | 100% [91–100] | 100% [91–100] | 100% [91–100] | 100% [91–100] |
| cross_tenant | 82.5% [68–91] | 77.5% [62–88] | 50.0% [35–65] | 0% [0–9] |
| entity_confusion | 80.0% [65–90] | 82.5% [68–91] | 65.0% [50–78] | 0% [0–9] |
| knowledge_update | 80.0% [65–90] | 82.5% [68–91] | 60.0% [45–74] | 0% [0–9] |
| long_tenure | 55.0% [40–69] | 47.5% [33–63] | 35.0% [22–50] | 0% [0–9] |
| multi_hop | 0% [0–9] | 5.0% [1–17] | 12.5% [5–26] | 0% [0–9] |
| point_in_time | 12.5% [5–26] | 22.5% [12–38] | 45.0% [31–60] | 0% [0–9] |
| raw_detail | 0% [0–9] | 0% [0–9] | 85.0% [71–93] | 0% [0–9] |
| **hard strata (240)** | **45.4%** [39–52] | **48.8%** [42–55] | **61.3%** [55–67] | **16.7%** [12–22] |
| **all (320)** | **51.2%** [46–57] | **52.2%** [47–58] | **56.6%** [51–62] | **12.5%** [9–17] |
| CW, all | 29.1% | 30.9% | 41.9% | 5.9% |
| Tokens, mean | 253 | 1,000 | 2,034 | 2,048 |
| Retrieval p50 / p95 ms | 242 / 17,536 | 2 / 4 | 65 / 465 | 17 / 26 |
| Answer p50 ms | 536 | 1,196 | 8,684 | 1,065 |

The MDE at n = 40 (two-sided α 0.05, power 0.8) is 14 to 31 points,
depending on the stratum's accuracy. A per-stratum difference smaller than
that is not a result. The hard-strata pool, n = 240, has an MDE of 9.5 to
12.8 points.

Deciding comparison, pooled hard strata, `recall` against `full_context`:
- McNemar: 69 improved, 0 regressed; `promotion_verdict` gives promote,
  p < 0.001.
- Non-inferiority at a 5-point margin holds: the difference is +28.7 points,
  with a 95% interval of +23.0 to +34.5.

How to read it:
- **full_context is a weak baseline.** At 2,048 tokens, the newest history
  almost never holds the answer, so it abstains nearly every time. That is
  correct on abstention and scores 0 everywhere else.
- **recall uses little of its budget.** `search_facts` returned about 250
  tokens per case against the 2,048 budget. At equal budget it does no better
  than plain keyword search (51.2% vs 52.2% over all cases, with overlapping
  intervals).
- **recall is fast but has long tails.** Its p95 retrieval is 17.5 s, which is
  the reranker under shared load.
- **raw_verbatim is the only system that answers raw_detail** (85%). It is
  also best on point_in_time, because transcripts keep superseded values.
  It has the highest confident-wrong rate (41.9%).
- **No system leaked the other tenant's data.** No answer anywhere named a
  shadow-tenant value. Every cross_tenant miss (raw_verbatim 20, recall 7,
  keyword_only 9) named a different primary-tenant value or abstained.
- **multi_hop is near 0 for every system.** No retrieval-only system chains
  two or three facts within the budget.
- `full_context_unbounded` (24,000 tokens, not equal-budget) was **not run**:
  at about 24,000 tokens per prompt on a shared local model, 320 cases did not
  fit the time available.

### Run-to-run spread

A follow-up run used `--limit-per-stratum 10 --repeats 3`: 80 cases, four
systems, three runs, with repeat 0 reused from the main run's progress file.
Accuracy did not move between runs for recall, keyword_only or full_context in
any stratum. raw_verbatim varied by one case in raw_detail (80–90%), which is
1.7 points on the pooled hard strata. The details are in
[world-baseline.md](world-baseline.md). At temperature 0 the noise floor is far
below the MDE, so a difference that clears the MDE is not run-to-run noise.

## History

The suite's previous baseline (2026-07-25: 12/12 on the then 12-case suite)
was retired when the suite grew to 267 cases. Its gate is now the 0.90 floor
in `robothor/memory/eval.py`.
