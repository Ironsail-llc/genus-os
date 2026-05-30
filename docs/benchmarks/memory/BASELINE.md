# Memory Eval — Baseline (pre-upgrade)

Captured on branch `feat/memory-cognitive-upgrade` before any cognitive-layer
upgrade (Knowledge Vault / symbolic compaction / intent memory / router) was
enabled. This is the reference every later phase is measured against.

Run: `robothor memory-eval` (live PostgreSQL + local Ollama embeddings + reranker),
suite `docs/benchmarks/memory/suite.yaml` (`memory-recall-v1`), isolated tenant
`memory-eval`, seeded facts cleaned up after the run.

```
Memory eval: memory-recall-v1
  passed: 5/6
  recall: 2/2
  temporal: 0/1
  verbatim: 2/2
  persona: 1/1
    [PASS] recall-project-owner (recall) score=1.0
    [PASS] recall-contact-role (recall) score=1.0
    [FAIL] temporal-storage-decision (temporal) score=0.0 — gold not found in top-5
    [PASS] verbatim-support-line (verbatim) score=1.0
    [PASS] verbatim-account-id (verbatim) score=1.0
    [PASS] persona-meeting-pref (persona) score=1.0
```

## Reading the baseline

- **verbatim 2/2 PASS** — note this is the *direct-seed* path (`store_facts_batch`),
  which stores exact text without LLM paraphrase, so exactness is preserved here.
  The Knowledge Vault's value shows up on the *ingestion* path (LLM extraction
  paraphrases); a `seed_mode: ingest` verbatim case (LLM-gated) is the faithful
  vault comparison and is left for the Phase 1 gate.
- **temporal 0/1 FAIL** — the real signal. Two competing decisions are seeded as
  independent active facts; `search_facts` ranks by hybrid relevance with no
  recency/supersession weighting, so the latest decision does not reliably rank
  first. Candidate improvement for the router phase (recency-aware ranking) or a
  supersession-linked seed. Tracked, not yet addressed.

## Gates for later phases

| Phase | Expected delta vs this baseline |
|---|---|
| 1 Knowledge Vault | add `seed_mode: ingest` verbatim case → should flip PASS only with vault on |
| 4 Memory router | temporal case should move toward PASS; recall/verbatim must not regress |
