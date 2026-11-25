# Memory Eval — Baseline

The memory eval runs in two modes. Both use only evaluation tenants
(`memory-eval` or `memory-eval-<suffix>`) and refuse any other tenant before
the first database call.

| Mode | Command | What it measures |
|------|---------|------------------|
| Suite | `memory-eval --suite docs/benchmarks/memory/suite.yaml` | 267 hand-checked and generated cases. Each seeds a few facts, runs `search_facts` and checks whether the gold string is in the top k. Runs nightly in `memory-eval` with `--record` (`robothor-memory-eval.service`) |
| World | `memory-eval --world [--answers]` | One generated world of about 3,500 facts, CRM rows, documents and transcripts, seeded once into `memory-eval-world` ([WORLD.md](WORLD.md)). Without `--answers` it uses the same substring-in-top-k scoring. With `--answers` a local model answers from each system's context at an equal token budget, and a validated judge grades the answers |

Exit codes for the suite: `0` met the floor, `2` ran and fell below it, `3`
could not run. World mode exits `0` once it has run and `3` if it could not;
there is no world floor yet. The suite needs `ROBOTHOR_TENANT_ID=memory-eval`
and the world needs `ROBOTHOR_TENANT_ID=memory-eval-world`. Without these,
row-level security (migration 081) rejects the seed writes, and `preflight()`
exits 3 with the fix printed.

The recorded world baseline is in [world-baseline.md](world-baseline.md). The
JSON report it comes from, minus per-case records, is
`docs/benchmarks/memory/world-baseline.json`.

## 2026-11-24: world baseline (local runs)

Common to both runs: world seed 1, size `full`, dev split, embeddings from
`qwen3-embedding:0.6b`, and `dengcao/Qwen3-Reranker-0.6B:F16` as the reranker
inside `search_facts`. Retrieval code matches main's.

### Retrieval scoring (`--world`, substring in top 5)

These are today's retrieval numbers on main's recall code, measured as "the
gold string appears in a retrieved row". The run used commit `dc3128d42` on
the earlier Docker-hosted test database. 803 cases were scored; the 118
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

This replaces the comparison first recorded earlier the same day. What
changed after review, and why:
- **Reference system.** The reference is now `keyword_only`. `full_context` at
  this budget almost always abstains, so beating it said nothing.
- **Second recall variant.** `recall_budget` was added. The product path
  (`recall`) returns at most 16 reranked facts, about 250 tokens. The variant
  keeps that ranking and fills the rest of the budget from `search_facts`'
  own unreranked candidates. The table now shows both "what the product
  returns today" and "what recall can do at equal budget".
- **Hedges.** An answer that names the gold beside a rival value of the same
  kind is now wrong by rule. When hedges were left to the LLM judge,
  validation caught it accepting 55 of 150 planted hedges.
- **Validation per kind.** Every planted kind must now clear 95% on its own.
- **One outcome per case.** Repeats no longer count as extra evidence in the
  comparison or the intervals.
- **Models.** They are named and run on local Ollama.

The re-run's answers contain no hedges, so the judge change moved no score.
Differences from the first recording (raw_verbatim cross_tenant 50.0% →
47.5%, for example) are answer-to-answer variation of the model on a
different, busier server.

Run parameters:

| Parameter | Value |
|-----------|-------|
| Code | commit `b85d9a2c7` (answer-arm branch), native PostgreSQL 16 test database |
| Cases | at most 40 per stratum (`--limit-per-stratum 40`), 320 in total |
| Systems | `recall`, `recall_budget`, `keyword_only` (reference), `raw_verbatim`, `full_context` |
| Token budget | 2,048 |
| Repeats | 1 |
| Answer model | `qwen3:8b` on local Ollama, temperature 0, no thinking |
| LLM judge | `qwen3:8b` on local Ollama; 27 of 1,600 verdicts needed it, the rest were settled by rules |
| Concurrency | 4 |
| Time | 2,603 s (seeding 75 s, answering 2,324 s); every embedding came from the cache |
| Errors | none |

Judge validation passed before any case ran:
- Every planted kind cleared 95%. Hedges were 150/150, wrong_llm 150/150 and
  paraphrase_llm 148/150.
- Pooled: 1,050 of 1,050 planted wrong answers marked wrong, and 448 of 450
  planted correct answers marked right.

Accuracy, with Wilson 95% intervals; one outcome per case.

| Stratum | recall | recall_budget | keyword_only | raw_verbatim | full_context |
|---------|--------|---------------|--------------|--------------|--------------|
| abstention | 100% [91–100] | 100% [91–100] | 100% [91–100] | 100% [91–100] | 100% [91–100] |
| cross_tenant | 82.5% [68–91] | 82.5% [68–91] | 77.5% [62–88] | 47.5% [33–63] | 0% [0–9] |
| entity_confusion | 80.0% [65–90] | 85.0% [71–93] | 82.5% [68–91] | 65.0% [50–78] | 0% [0–9] |
| knowledge_update | 80.0% [65–90] | 80.0% [65–90] | 82.5% [68–91] | 60.0% [45–74] | 0% [0–9] |
| long_tenure | 55.0% [40–69] | 60.0% [45–74] | 47.5% [33–63] | 35.0% [22–50] | 0% [0–9] |
| multi_hop | 0% [0–9] | 12.5% [5–26] | 5.0% [1–17] | 12.5% [5–26] | 0% [0–9] |
| point_in_time | 12.5% [5–26] | 22.5% [12–38] | 22.5% [12–38] | 45.0% [31–60] | 0% [0–9] |
| raw_detail | 0% [0–9] | 0% [0–9] | 0% [0–9] | 85.0% [71–93] | 0% [0–9] |
| **hard strata (240)** | **45.4%** [39–52] | **50.0%** [44–56] | **48.8%** [42–55] | **61.3%** [55–67] | **16.7%** [12–22] |
| **all (320)** | **51.2%** [46–57] | **55.3%** [50–61] | **52.2%** [47–58] | **56.2%** [51–62] | **12.5%** [9–17] |
| Confident-wrong, all | 29.1% | 28.7% | 30.9% | 42.2% | 5.9% |
| Tokens, mean | 253 | 985 | 1,000 | 2,034 | 2,048 |
| Retrieval p50 / p95 ms | 439 / 18,050 | 689 / 38,311 | 2 / 4 | 33 / 40 | 13 / 17 |
| Answer p50 ms | 590 | 1,214 | 1,261 | 8,919 | 580 |

The MDE at n = 40 (two-sided α 0.05, power 0.8) is 14 to 31 points,
depending on the stratum's accuracy. A per-stratum difference smaller than
that is not a result. The hard-strata pool, n = 240, has an MDE of 9.5 to
12.8 points.

**Deciding comparison**, pooled hard strata, one pair per case: `recall`
against `keyword_only`.
- 7 improved, 15 regressed; `promotion_verdict` gives no promote (delta −3.3
  points).
- Not non-inferior at the 5-point margin: the difference is −3.3, with a 95%
  interval of −7.1 to +0.5.

Other pairings, computed from the same records and shown for context, not as
deciding comparisons:

| Candidate vs reference | Improved / regressed of 240 | p | Promote | Difference [95% CI] | Non-inferior |
|---|---|---|---|---|---|
| recall_budget vs keyword_only | 13 / 10 | 0.68 | no | +1.3 [−2.7, +5.2] | yes |
| recall_budget vs recall | 15 / 4 | 0.019 | yes | +4.6 [+1.1, +8.1] | yes |
| raw_verbatim vs keyword_only | 55 / 25 | 0.001 | no: the stratum guard blocks it, because entity_confusion regressed (3 improved, 10 regressed, p = 0.092) | +12.5 [+5.4, +19.6] | yes |

How to read it:
- **The product path loses to keyword search at equal budget.** recall
  returns about 250 tokens against the 2,048 budget. It is not non-inferior to
  plain full-text search over the same facts.
- **Filling the budget fixes most of that.** recall_budget is significantly
  better than the product and non-inferior to keyword_only, but not better
  than it. The reranker cap on how much the product returns costs measurable
  accuracy.
- **raw_verbatim answers what facts cannot.** It is the only system that
  answers raw_detail (85%) and the best on point_in_time, because transcripts
  keep superseded values. It also has the highest confident-wrong rate
  (42.2%), and it is weaker on cross_tenant and knowledge_update.
- **No system leaked the other tenant's data.** No answer named a
  shadow-tenant value; every cross_tenant miss named a different
  primary-tenant value or abstained.
- **multi_hop stays near 0 for every system.** No retrieval-only system chains
  two or three facts within the budget.
- **full_context fails at this budget.** The newest 2,048 tokens of history
  almost never hold the answer.
- **`full_context_unbounded` (24,000 tokens, not equal-budget) was not run.**
  At about 24,000 tokens per prompt on a shared local model, 320 cases did
  not fit the time available.

### Run-to-run spread

Measured once, on the first recording (four systems, before the review
fixes): `--limit-per-stratum 10 --repeats 3`, 80 cases.
- Accuracy did not move between runs for recall, keyword_only or full_context
  in any stratum.
- raw_verbatim varied by one case in raw_detail (80–90%), which is 1.7 points
  on the pooled hard strata.

At temperature 0 this noise floor sits far below the MDE. It was not repeated
for recall_budget. Under the per-case scoring now in place, repeats change
only the spread column, never `n`, the intervals or the deciding comparison.

## History

The suite's previous baseline (2026-07-25: 12/12 on the then 12-case suite)
was retired when the suite grew to 267 cases. Its gate is now the 0.90 floor
in `robothor/memory/eval.py`.
