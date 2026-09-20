# Runtime modernization: implementation and evaluation

This change starts from the accepted integration revision `eea3252b15`. The current runtime remains selected. No production service, historical meeting, stopped goal, or deployed configuration was changed.

**Promotion is not approved by these results.** The native confirmed-operation and goal/control contracts pass. The live native synthetic action cohort exposed unsatisfied outcomes with MiMo, and the candidate smoke runs are not a matched native-workload comparison. Candidate checkpoint, delegated authority, memory and provider-fallback parity must be established before selecting a replacement. The 20% replacement gate has not been established for either candidate.

The local acceptance follow-up, fixes, and latest test results are recorded in [runtime UAT](runtime-uat.md). The measurements below preserve the original implementation evaluation.

The current [requirements audit](runtime-acceptance-audit.md) records verified scope and remaining work. In particular, action prototypes do not establish full alternative-runtime parity.

## Product and execution boundaries

`robothor/engine/runtime/contracts.py` defines execution identity, requests, checkpoint envelopes, progress, usage, results and the runtime protocol. `CurrentRuntime` wraps `AgentRunner.execute`; the existing runner owns model retries, compaction and delivery. The goal controller accepts an injected runtime and otherwise uses this adapter. Business state remains in the existing stores.

The native adapter preserves deterministic confirmation: confirmed calendar operations go through existing admission/dispatch and end without another model or repair loop. Current runtime/checkpoint identity and resolved principal attribution are written with every run by migration `127_runtime_contract.sql`. Checkpoints carry an additive runtime envelope. Legacy version-1 native checkpoints remain readable; mismatched or unavailable checkpoints are rejected rather than replayed as fresh requests. Resume checks tenant ownership and durable stop state.

Durable pause/cancel records apply to a run and its descendants. Dispatch checks the ancestor chain in PostgreSQL. The local activity registry cancels an active provider wait promptly after the durable command; it is an acceleration mechanism, not the authority source. Existing interrupt controls now persist a stop before acknowledging it. Authenticated tenant operators can also use `POST /api/goals/runs/{run_id}/control`. Already dispatched external requests can finish after cancellation and require receipt reconciliation. A durable stop is not a claim that an external effect was reversed.

Budgeted goal attempts share provider reservations across delegated runs. Reservations precede provider dispatch and retain a conservative charge on missing usage, cancellation or lost workers. Settlement is idempotent, and a provider overrun closes the local allowance. The existing tenant coordinator lease serializes execution children; this does not introduce distributed parallel child coordinators. Reservations are intentionally conservative for text requests. Multimodal/provider-side billable tools without a bounded token contract fail closed in explicitly token-capped goals. Uncapped goals preserve the previous path.

## Goal workspace

The workspace combines objectives, criteria/evidence, children as execution milestones, linked tasks, budgets, blockers, due reviews and reconciliation state. Operators can review and authorize a child milestone or explicitly revise criteria with a reason. Revisions clear current evidence. Pause/cancel applies to the supported parent/child hierarchy and signals local execution immediately; the coordinator also rechecks controls every second.

New goals require independent evidence or operator review. `calendar-operation:<uuid>` references are checked against completed, verified tenant-owned receipts and must be newer than the goal, criteria revision and previous assessment. Receipt authenticity alone does not establish a broader business criterion: automatic verification requires the criterion to name that exact operation reference. Broader criteria with valid receipts still require operator review. Other evidence is labeled **agent assessed**, and successful completion or a recurring “meeting” assessment waits for operator review. Approval of a recurring assessment starts the next waiting period, clearing its evidence. Existing goals without the new policy flag retain their previous policy; this change does not migrate or resume them.

## Inventory: implementation is not deployment

| Capability | Implemented | Default/enablement | Verified here |
|---|---|---|---|
| Native runtime interface | Yes | Current remains default | Native runner and goal contracts |
| Durable run/descendant stop | Yes | Requires additive migration on rollout | Private PostgreSQL + active cancellation |
| Goal-family provider reservations | Yes | Only explicitly token-capped goals | Concurrent reservations, unknown usage, stale lease/recovery |
| New-goal evidence review policy | Yes | New goals only; pursuit still opt-in | Lifecycle/API and restored-goal tests |
| Goal revisions and execution milestones | Yes | Operator controls | Six UI tests; TypeScript check |
| Progress cadence | 10 seconds; acceptance event | Existing status sinks | Event/cancellation tests; network UI delivery SLO not certified |
| Pydantic AI / Deep Agents | Bounded experimental action adapters | Laboratory only | Synthetic, live cloud smoke, one upgrade each |
| OpenCode | Public SDK transport feasibility | Laboratory only | Serialization/abort request; server enforcement unverified |

## Reproducible evidence

Artifacts are under [`bench/runtime`](../../bench/runtime). Every live business tool in these experiments was synthetic. Configured model names were read from the existing main manifest; no model selection was changed. Models were measured separately. Python was 3.14.2; native and accepted-revision measurements used the same dependency environment. PostgreSQL tests start a disposable local cluster with TCP disabled.

- `accepted-integration-baseline.json`: 100 samples per calendar execution path on the untouched integration revision.
- `current-runtime-final.json`: 100 samples per path through the new native adapter, including a real durable-stop query against private PostgreSQL.
- `store-capacity.jsonl`: 30 iterations per synthetic tenant, at 1, 5 and 20 concurrent tenants; waiting-goal claim/detail/steering only.
- `mixed-runtime.jsonl`: 30 interactive requests and 30 active goal turns per tenant at 1, 5 and 20 concurrent tenants, using the native runner, real goal coordinator/private store, and deterministic provider/business tools. Deployment admission queues and network/provider throughput are excluded.
- `stop-contract.json`: 30 durable parent stops with an active descendant.
- `screening.json`, `screening-initial-failures.json`: candidate synthetic results; the initial Pydantic usage-access integration failure is retained.
- `upgrade-before.json`, `upgrade-after.json`: Pydantic AI 2.45.0 → 2.46.0 and Deep Agents 0.7.14 → 0.7.15, 30 action samples each before and after. This is an action/API upgrade drill, not proof of checkpoint migration.
- `live-screening.jsonl` and its summary: 30 samples per candidate/model across the three configured cloud models, 180 outcomes retained.
- `live-native.jsonl` and its summary: 30 native-runner samples per configured cloud model, including failures and unsatisfied fixture state. The native prompt differs from the bounded candidates' prompt, so these are **not** interchangeable comparison cohorts.
- `live-native-diagnostic.jsonl`: five separately labeled MiMo diagnostic runs with synthetic tool traces; unnecessary clarification is visible in failed outcomes. These do not replace or increase the screening cohort.
- `opencode/result.json`: SDK 1.18.31 requests through a synthetic transport. The host CLI reports 0.0.55 and was not started or reconfigured.

Nearest-rank p95 and seeded bootstrap 95% intervals are reported. Thirty samples are screening evidence; even 100 samples do not prove the tail of an arbitrary workload. Missing telemetry is not replaced with zero. Native live cost fields are engine accounting estimates, not a billing reconciliation.

Measured native confirmed-operation p95 was approximately **123 ms**, against **123 ms** on the accepted integration revision. Both use zero model calls and no post-completion tools. The 20-tenant store workload measured approximately **31 ms p95**; durable stop acknowledgement and active descendant cancellation measured approximately **8 ms p95**. These limits describe these short fixture workloads, not full service capacity or shared-GPU throughput.

The mixed native/coordinator workload completed all **1,560 verified synthetic actions**. Interactive p95 was **149 ms / 864 ms / 2,818 ms** at **1 / 5 / 20** tenants respectively; the largest tested load below two seconds was five tenants. Twenty tenants remained under the 30-second simple-action target but exceeded the two-second warm-harness target under contention. This is an operating limit for this fixture, not a deployment sizing recommendation. The initial 20-tenant batch exceeded pytest's default 30-second whole-test allowance; its failure is retained separately, and the final batch allowance is 300 seconds. The individual request targets were evaluated separately from the batch allowance.

The live native cohorts each stayed below 20 seconds p95. Both configured DeepSeek models achieved 30/30 verified synthetic outcomes. MiMo achieved **15/30**, including one failed runtime result. These failures remain in the artifact and prevent claiming that the live everyday-action acceptance suite passes across the complete configured chain. Both candidate smoke adapters achieved 30/30 on each configured cloud model, but their simpler prompt/tool environment cannot establish superiority over the native runner.

The initial broad engine run passed **10,828 tests**, with 29 skipped, 177 deselected and **10 failures**, all reproduced on the untouched integration checkout. New unset-tenant diagnostics and optional-runner initialization regressions were corrected, and native setup was extracted to keep the runner smaller. The final focused goal/runtime/budget suite passes 101 tests; the separate mixed-workload suite passes three tests, and the goal workspace passes six UI tests and TypeScript checking. `verification.json` records scope and remaining failures; inherited failing checks are not waived by this document.

Example commands from a clean checkout:

```bash
python -m pytest robothor/goals/tests robothor/engine/tests/test_runtime_contracts.py robothor/engine/tests/test_runtime_controls.py
python -m bench.interactive.run_runner --samples 100 --output /tmp/native-runtime.json
python -m bench.runtime.screen_candidates --samples 30 --output /tmp/candidate-screen.json
```

Install the candidate pins in a separate environment using `bench/runtime/candidates.lock`. Live probes are explicit commands/tests and never part of ordinary CI. `.github/workflows/runtime-contracts.yml` adds pull-request and monthly synthetic compatibility checks without production credentials.

## Selection and rollout gates

Retain the improved current engine for new admissions. Do not infer a general replacement from the candidate smoke results. Before a candidate can be selected, implement and pass the shared checkpoint/resume, delegated-control, goal-family accounting, memory and provider-fallback contracts through its adapter, then compare identical prompts/tools/resources for at least 100 samples per finalist scenario. Require at least 20% overall p95 improvement and no critical scenario more than 10% slower. Re-run mixed interactive/background workloads using the actual deployment worker and provider topology; the store test is not that certification.

The new benchmark comparator reports individual scenarios and preserves failures/timeouts. It enforces sample counts, matching cohorts, the overall latency threshold and per-scenario regression limits. Its latency gate alone never authorizes promotion.

For deployment review, apply the additive migration before enabling the new code in the coordinated deployment slot. Review new sessions first. Keep existing checkpoints on compatible runtimes. Rollback means routing **new admissions** back to the compatible current runtime while preserving control, operation and usage records. Do not roll stopped runs back to an older engine that lacks durable-stop enforcement. Never replay an uncertain write through another runtime. The private-database rollback contract verifies that a new admission does not clear an older run's stop.

Production rollout, a live staged rollback drill and promotion are still unperformed and require the separately reviewed artifact specified in the plan. Stop expansion on unauthorized/duplicate effects, false verification, ineffective stop, lost state or failed latency/correctness gates. Review upstream releases monthly and security changes promptly; a dependency upgrade must pass contracts before admission changes.

## Upstream extension references

The prototypes use public APIs rather than framework source modifications: [Pydantic tools](https://pydantic.dev/docs/ai/tools-toolsets/tools/), [Pydantic agent iteration/usage](https://pydantic.dev/docs/ai/api/pydantic-ai/agent/), [Deep Agents factory and middleware](https://reference.langchain.com/python/deepagents/graph/create_deep_agent), and [OpenCode SDK](https://opencode.ai/docs/sdk/), [permissions](https://opencode.ai/docs/permissions/), [custom tools](https://opencode.ai/docs/custom-tools/). Documentation availability is not evidence that a runtime has passed Robothor's controls.
