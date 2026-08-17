# Memory Eval — Baseline

**Captured 2026-07-25.** This replaces a stale baseline that recorded 5/6 over
6 cases from branch `feat/memory-cognitive-upgrade`, taken before the suite
doubled to 12 cases. Every later phase is measured against the numbers here.

The old baseline's headline finding — `temporal-storage-decision` FAIL, "the
latest decision does not reliably rank first" — now passes, along with the rest
of the temporal stratum. See the caveats below before reading anything into
that: the run did **not** carry `MEMORY_TEMPORAL_COHERENCE`, the flag that was
supposed to fix it.

## How to reproduce

```sh
ROBOTHOR_TENANT_ID=memory-eval \
  python -m robothor.cli memory-eval --suite docs/benchmarks/memory/suite.yaml
```

The `ROBOTHOR_TENANT_ID` override is **required**. Without it the process
carries the production tenant, row-level security (migration 081) rejects the
seed writes with `InsufficientPrivilege`, and the run aborts. The eval had been
unrunnable for this reason; `preflight()` now detects it up front and exits 3
with the fix printed rather than crashing mid-seed.

Exit codes: `0` all cases passed, `2` the suite ran and cases failed, `3` the
suite could not run. A harness that cannot execute must never be mistakable for
either a pass or an ordinary failure.

## Result

```
Memory eval: memory-recall-v1
  passed: 12/12
  recall: 2/2      temporal: 4/4     verbatim: 2/2
  persona: 1/1     noise: 1/1        resolution: 2/2
```

Wall clock: roughly 13 minutes for 12 cases (~65 s/case) against local Ollama,
dominated by the cross-encoder reranker scoring every candidate.

## What this number does *not* yet establish

Read these before treating 12/12 as a gate.

**1. Flag posture is correct — an earlier note here claiming otherwise was
wrong.** A previous revision of this file asserted the eval measured
`MEMORY_TEMPORAL_COHERENCE` / `RERANK_WIDE` / `EPISODE_MERGE` all off while the
engine ran them on. That was a bad inference, drawn from evaluating
`facts._temporal_coherence_enabled()` in a bare Python shell — which is not the
entry point the eval uses.

`robothor.cli`'s `main()` calls `load_instance_env()`
(`robothor/engine/instance_env.py`), which reads both
`/etc/robothor/robothor.env` **and** the engine's systemd drop-in and fills in
anything the caller did not set, with explicit values still winning. Verified:
all three flags resolve to `1` inside a CLI invocation, matching the daemon. So
`robothor memory-eval` measures the production configuration, and the caveat
that used to sit here did not apply.

The module exists precisely because the opposite failure was found once before:
a CLI run outside systemd read every rollout-gated guardrail back as its
default while the daemon was enforcing. Reuse it rather than re-reading the
drop-in.

**2. N is far too small for a gate.** Twelve cases, several strata at n=1
(`persona`, `noise`). At these sizes one flipped case moves the headline by 8
points, and no stratum can produce enough discordant pairs for a paired
significance test. Promotion decisions need the expanded, stratified suite.

**3. Run-to-run variance is unmeasured.** The reranker is a model; borderline
cases can flip between runs. Gate floors must come from repeated runs, not from
this single one — a floor set from one run pages on noise, and a gate that
pages on noise gets muted.

**4. Question phrasing shares a generator with extraction.** The cases were
written against the same local model family used to extract facts, biasing
vocabulary toward the corpus. Regenerating with a different family is required
before the numbers read as model-independent.

**5. It is not scheduled.** Nothing runs this nightly, so the figure is a
point-in-time measurement, not a regression signal. At ~65 s/case an expanded
150-case suite would take roughly 2.7 hours, which does not fit a nightly
window alongside the 03:30 pipeline and the 04:00 fleet run — the seeded arm
and the corpus arm will need separate cadences.

## Side effect worth recording

Before this run, `memory_facts` held **6,036 rows under tenant `memory-eval`** —
fixture debris accumulated in the production database by earlier partial runs.
The per-case cleanup removed all of them; the count is now 0.
