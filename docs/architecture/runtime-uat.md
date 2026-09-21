# Local runtime acceptance follow-up

The accepted integration baseline is `eea3252b15`; the first modernization implementation is `a0bcefed70`. This follow-up uses the separate `feat/runtime-modernization` worktree. It does not deploy production code, alter model selections, resume stopped goals, or modify historical meetings.

## Findings and fixes

- Plugin discovery enumerated installed entry points twelve times per load. One fresh snapshot per load preserves hot reload and discovery order while removing repeated metadata scans. Plugin integrity/discovery tests pass.
- Chat ignored acceptance/progress events and Stop only disconnected the browser. The UI now renders activity and elapsed time. Stop commits a tenant/principal/session-scoped request stop before acknowledging it. Early stops survive later admission; descendant runs retain the request identity. Failed persistence is reported as unconfirmed. Already dispatched requests may still finish.
- Tests cover cancellation-resistant work, stop-before-admission, restart persistence, unrelated requests in the same tenant, and delayed acknowledgements arriving after the next browser request starts.
- Planning repeatedly issued its research instruction when the model kept answering without tools. A per-session latch issues it once; the existing independent alignment check still applies. Four formerly slow research-turn tests now finish locally with provider calls mocked.
- A telemetry exception could append both success and failure for one benchmark task. Results now have one row per task; the regression test also checks charged cost remains accounted for.
- The ten failures previously reproduced on the accepted revision are repaired. Fixes include sandbox tool classification and tool search ranking; stale structural assertions now check the actual current task helper and slash-command implementation. Small extractions keep existing function-size limits intact.
- Completed goals no longer display a stale instruction to start pursuing the objective.

## Verification scope

`bench/runtime/uat-verification.json` records current measurements and checks. Broad engine unit coverage, the full frontend unit suite, private-database goal tests, browser lifecycle scenarios, lint, and TypeScript checking pass. Infrastructure/live-provider tests excluded from the broad engine command are not silently counted as passed.

Browser checks mount the real GoalsView and goal router over a fresh private PostgreSQL database: parent pause/cancel propagation, explicitly authorized milestones and family budgets, evidence invalidation after revision, and operator approval of judgment-based completion. These are automated product checks, not a claim that the user has accepted the experience.

Calendar timing uses 100 samples per execution path and preserves every sample. Both paths verify the attendee change, existing RSVP and time, a single write, and no post-completion tools. Confirmed execution still makes zero model calls. Mixed workloads use 30 repetitions per scenario per tenant, at 1, 5 and 20 concurrent synthetic tenants. Confidence intervals are in the artifacts. These fixtures do not certify production provider throughput or network queueing.

The production application build passes. Its isolated browser run passed 61 of 62 scenarios; the sole failure expected the pre-change chat request body without `request_id`. That expectation was updated, and the two agent-switching/escalation scenarios then passed. The browser harness now refuses to reuse an existing local server, ensuring the configured isolated backend URLs and test authentication apply.

Explicit runtime deadlines are now enforced at admission and across provider/tool dispatch, with one owner for nested deadlines. Expiry cancels the native runner, preserves its cancellation finalization, and raises `RuntimeDeadlineError` with an unresolved-status explanation if a callee suppresses cancellation. Unrelated provider/workflow timeouts retain their original cause. Deadline expiry bypasses model retries/fallback and shared retry helpers; 159 deadline, streaming, retry and workflow checks pass. A child cannot extend the parent deadline; enforcement uses a monotonic clock after admission. Focused deadline/workflow/budget checks pass. The subsequent broad engine run passed 10,857 tests; its sole failure was a module-size limit, repaired by moving the new classifier into the deadline module. The affected contracts and size checks were rerun successfully. A fresh 100-sample calendar run after deadline enforcement still passes all state and zero-model confirmation checks (`uat-deadline-calendar.json`). This does not establish automatic classification of every simple conversational action into the 60-second policy.

Thirty-five additional MiMo first-turn diagnostics retained the original prompt and varied individual components. Removing skills did not fix the failure (2/5 correct tool proposals); moving the account note to system context was not reliable (4/5). Minimal prompts achieved 5/5 in these small probes, but removing product policies is not an acceptable fix. None of these variations was adopted. Xiaomi's [API documentation](https://mimo.mi.com/docs/en-US/quick-start/faq/api-integration) documents developer messages, so these results do not justify claiming that the model lacks developer-role support. These are diagnostics, not new acceptance cohorts.

## Measured latency

| Fixture | Accepted baseline / initial implementation p95 | Final UAT p95 |
|---|---:|---:|
| Ordinary calendar operation, 100 samples | 138.2 ms (accepted baseline) | 53.9 ms |
| Confirmed calendar operation, 100 samples | 122.5 ms (accepted baseline) | 34.3 ms |
| Interactive alongside goals, 1 tenant | 148.6 ms (initial implementation) | 53.2 ms |
| Interactive alongside goals, 5 tenants | 863.7 ms (initial implementation) | 280.2 ms |
| Interactive alongside goals, 20 tenants | 2,818.4 ms (initial implementation) | 1,052.4 ms |

All 1,560 mixed-workload actions verified. Twenty tenants is now the largest tested fixture load below the two-second harness target. It is not a production capacity guarantee. Raw samples and seeded bootstrap intervals remain in the referenced artifacts.

## Remaining acceptance work

The full acceptance goal remains open. In the original native live screening, both configured DeepSeek models verified 30/30 outcomes, while MiMo verified 15/30. A matched native MiMo check on the accepted baseline verified 14/30. The failure therefore predates modernization; it is still an unresolved reliability limitation. A stronger clarification instruction was tried, achieved only 13/30, and was reverted. Both the failed trial and baseline results are retained. No model configuration was changed.

The configured local Qwen model completed 30/30 synthetic actions with one write each. Its p95 was 20.4s, but one repetition took 74.7s and 20 model calls, exceeding the 60-second bound. That cohort supplied a 60-second manifest timeout, not an explicit runtime deadline. We retain this as failed deadline acceptance; its trace did not capture when the write occurred, so stopping after success still needs direct verification. Production-shaped network acknowledgement/progress/queue SLOs and end-to-end fallback under live load remain unqualified. Candidate adapters have not demonstrated full checkpoint, memory, authority and goal-budget parity or a matched finalist workload; no replacement was selected. A deployed rollback drill and promotion remain outside local verification and require the separate deployment review. Manual acceptance with the user remains pending.

## Reproduction

From the repository root, using the isolated development environment:

```bash
.venv/bin/python -m pytest -q --no-cov -m 'not slow and not integration and not llm and not e2e and not smoke' robothor/engine/tests
.venv/bin/python -m pytest -q --no-cov robothor/goals/tests robothor/engine/tests/test_runtime_controls.py
.venv/bin/python -m bench.interactive.run_runner --samples 100 --output /tmp/runtime-calendar.json
ROBOTHOR_RUNTIME_MIXED_OUTPUT=/tmp/runtime-mixed.jsonl .venv/bin/python -m pytest -q --no-cov robothor/engine/tests/test_runtime_mixed.py
```

From `app`, with the pinned pnpm installed:

```bash
pnpm test
pnpm exec tsc --noEmit
RUNTIME_UAT_MANAGED=1 pnpm exec playwright test --config uat/runtime/playwright.config.ts
```

The managed browser command starts and stops a loopback-only UI/API and a disposable PostgreSQL cluster, using ports 5323/5324 by default. Set `RUNTIME_UAT_UI_PORT` and `RUNTIME_UAT_API_PORT` if those ports are occupied. It enables no scheduler, business connectors, or model calls. The monthly compatibility workflow now runs these browser contracts as well as native/candidate contracts.


The diagnostic prompt variants can be reproduced with `python -m bench.runtime.prompt_probe --help`, using a synthetic `provider_messages` capture from the opt-in native benchmark. The utility refuses to overwrite earlier results and makes no tool dispatches. Its small first-turn samples must not be treated as a runtime qualification.

## Follow-up verification

The latest broad engine selection passes **10,862 tests**, with 29 skipped and 173 deselected (333.54s). The previously reported size failure is resolved. Subsequent focused checks pass for chat failure presentation (30 checks including size limits), native verified outcomes, deadlines and runtime contracts (27 checks), and mixed task/goal workloads against the corrected synthetic gateway (3 checks). No production deployment occurred.

The native runner no longer re-arms a runtime deadline already owned by the adapter. Its independent manifest and deterministic-operation caps remain intact. Ordinary chat now replaces unverified streamed text with the actual failed, timed-out or cancelled outcome, and records that outcome in conversation history. Successful responses keep their existing text.

The existing trusted `workflow_completion_scope` and `output_validation_scope` were exercised through the native runtime, without changing model configuration or weakening prompts. Deterministic checks cover an unnecessary clarification followed by one verified write and immediate completion, and repeated clarification exhausting its bounded corrections with a failed result. These scopes were already available; the benchmark explicitly installs them for a host-known action. This is not evidence that arbitrary conversation has a general-purpose independent outcome verifier.

`uat-host-contract-screen.jsonl` / `uat-host-contract-summary.json` record 30 repetitions per configured model with an explicit runtime deadline and trusted completion predicate. Both DeepSeek models and local Qwen verified 30/30, with zero subsequent provider/tool calls and no deadline overruns. Qwen p95 was 8.9s. MiMo verified 16/30, so that cohort fails acceptance. All samples remain available, including failures.

`uat-validated-contract-screen.jsonl` / `uat-validated-contract-summary.json` add bounded outcome validation. Both DeepSeek models and Qwen again verified 30/30. MiMo verified 28/30; one attempt wrote an extra key because the synthetic gateway allowed more than the requested operation. The gateway now rejects every key/value pair except the authorized `report=delivered` operation before mutation, and repeated identical writes remain idempotent. This fixes the test gateway's authorization boundary; it does not establish production authorization parity. The original failed cohort is preserved. Another failed MiMo attempt made no write: the captured test log identifies missing tool arguments raising a Python TypeError in the mocked dispatch. The gateway now returns a normal validation error for missing or extra arguments, permitting the existing native correction path to operate. A deterministic native test verifies malformed arguments followed by a correct write and immediate completion. Subsequent benchmark rows also always retain native error messages.

The user's positive experience with the running task and goal system is an acceptance baseline to preserve. Manual UAT should use a familiar workflow supplied by the user, not require them to interpret unexplained synthetic goal names. The running installation remains separate from this worktree.

The final corrected-gateway MiMo screening (`uat-validated-gateway-mimo-screen.jsonl` and matching summary) completed 29/30 actions with one write each. One attempt reached the runtime deadline at 60.009s with no writes and an explicit `RuntimeDeadlineError`; it did not report success. There were zero post-success provider or tool calls across the cohort. The p95 was 21.3s, with a bootstrap 95% interval of 16.6–60.0s including the failed sample. The strict all-actions-complete screening therefore remains failed. MiMo was tested alone with provider fallback disabled; this does not measure the configured primary-to-fallback chain. No additional model or prompt changes were made to chase a passing sample. The intermediate exact-authorization cohort (29/30, before missing-argument handling) is also preserved in `uat-authorized-mimo-screen.jsonl`.

## Local HTTP and fallback follow-up

`uat-native-http.json` measures 30 requests through a real loopback TCP server, the chat router, native AgentRunner, and private PostgreSQL durable-stop store. The provider is a synthetic cancellable wait; no business effects or external provider calls occur. Acknowledgement p95 is 43.5ms (bootstrap 95% interval 43.0–43.7ms), and durable Stop acknowledgement p95 is 15.3ms (13.9–16.0ms). Every stream ends as cancelled/aborted, and all 30 request stops remain in the database. Progress arrives at 10.085s and 20.086s, a 10.001s interval. The check allows one second of transport/scheduling overhead around the ten-second emitter; it does not claim a strict production-network delivery guarantee or a measured progress p95 from two events.

The native verified-outcome test also injects a primary-provider failure: the configured fallback path makes exactly one authorized write, returns independently verified completion, and makes no further provider call. This is deterministic fault injection through the real runner, not live-provider failover certification. These checks and the function-size checks pass together (10 tests, 29.92s). Both new native contracts are included in the monthly/PR compatibility workflow.

The [requirements audit](runtime-acceptance-audit.md) separates verified native behavior from remaining candidate integration and manual acceptance work. The comparator now checks explicit model settings and resource configuration. Different temperature, output-token limits, concurrency or deadlines cannot be pooled into a claimed improvement. Historical artifacts without these objects remain readable but cannot qualify performance gates; their configuration was not retroactively invented. Native benchmark records now capture their actual synthetic-provider settings and resource limits. Sixteen comparator, producer and size checks pass, and the existing 200-row calendar artifact remains readable with configuration marked unknown.

## Unfinished-work acceptance case

The user did not identify a specific successful task or goal and should not need to distinguish the two internally. A new local browser case uses ordinary requested work: one linked task is DONE, another is TODO, the goal waits for the remaining check, and no completion evidence exists. The actual goal view now summarizes the marked-done count, explicitly labels absent evidence, and shows a waiting child's reason in the parent view. It omits the generic initial "Start pursuing the objective" placeholder and labels substantive next actions as planned, avoiding an apparent instruction to run while the goal is waiting.

Five managed browser checks and six goal-view unit checks pass; TypeScript and changed-file lint checks pass. The preview is saved at [`uat-unfinished-work.png`](../../bench/runtime/uat-unfinished-work.png). It uses synthetic data and the real goal router/store/UI in a disposable database, with schedulers, providers and business connectors disabled. Task status is not promoted to independent evidence, and completion approval is unavailable for this waiting, unevidenced goal. User acceptance of the preview remains pending.

## Candidate admission and tool boundaries

Adversarial proposals through the pinned Pydantic AI 2.46.0 and Deep Agents 0.7.15 APIs exposed three prototype admission failures: Pydantic could begin model work for a stopped request, and both could begin model work for a different tenant before their gateway rejected the tool. These were experimental adapter failures, not changes to the selected native runtime. The shared synthetic gateway now checks admission before framework execution; Pydantic repeats it at graph boundaries, and Deep Agents repeats it before model and tool calls.

All ten boundary checks now pass, covering initial denial with zero model calls, stopping during provider work before a proposed write, bounded rejection of unrequested values, and proposed framework file writes. The file-write check installs a real temporary filesystem backend through the public Deep Agents factory and confirms no file is created. The tests use public callbacks, tools, graph iteration and middleware, with no framework source modification. A fresh 30-action screening per candidate passes (`uat-candidate-boundary-screen.json`), and five native verified-outcome checks pass after the shared fixture change. The monthly/PR compatibility workflow includes the candidate boundary suite.

`uat-candidate-boundary-contracts.json` retains the initial failure count and final scope. This remains in-memory fixture authority and synthetic provider evidence: it does not prove durable controls, compatible checkpoint recovery, host memory integration, or shared goal-family accounting through either alternative runtime. It does not qualify a replacement.

The adapters now accept host-defined tool schemas, dispatch and independent verification instead of directly inspecting the synthetic `report` key. The host may supply its system prompt. Tool names are closed over separately from model-generated arguments. Deep Agents explicitly binds each allowed call to the host's tool through its public request override, including collisions with built-in names such as `write_file`.

Fifteen candidate checks pass, including non-`record` operations, built-in name collisions with an empty temporary filesystem, one model request and one host write after verified success, and absent usage remaining unknown. SDK-reported token counters are retained where available; cost remains unknown rather than invented as zero. Five native verified-outcome checks and 30 synthetic actions per candidate also pass (`uat-candidate-host-tools-screen.json`). These are prerequisites for full shared-runtime integration, not a completed `RunRequest`/`RuntimeResult`, durable-control, goal-budget or checkpoint binding.

The Pydantic cloud-screening client now explicitly disables SDK retries, matching the existing Deep Agents transport setting. Agent-level `retries=0` did not disable the underlying SDK's retry default. Mock HTTP 429 and 503 responses each produce exactly one HTTP attempt, a surfaced failure and no tool dispatch or write. All 17 candidate boundary checks pass (2.88s); changed-file Ruff passes. These tests use synthetic credentials and an in-process HTTP transport, with no provider calls. The screening client is closed on exit. This closes a hidden retry path, but does not establish complete candidate budget accounting or qualify either replacement.

## Candidate shared request reservations

Both experimental adapter constructors now accept a host-owned request budget. Public Pydantic model wrappers and LangChain chat-model interfaces reserve from the existing `SharedBudget` before invoking the underlying model, including after tool binding. Reported positive token counters settle the reservation; missing usage, provider exceptions and cancellation retain it. An observed overrun records the true charge, closes further admission and prevents the returned tool proposal from executing. Pydantic streaming is explicitly unsupported at this boundary rather than bypassing accounting.

The 27 candidate boundary checks pass (3.11s), including simultaneous runs competing for one allowance, cancellation while a provider is pending, provider failure, overrun and absent usage for both frameworks. Nine native runtime contracts also pass (0.17s); Ruff, formatting and diff checks pass. The initial missing-usage fixture failed because Pydantic's FunctionModel estimates usage when omitted; the revised fixture explicitly supplies an unreported provider response after that synthetic estimator. No production code or acceptance condition was weakened to accommodate it.

These wrappers require a trusted conservative per-request token bound; they do not calculate one. The ledger remains in memory in these tests. Durable goal-family binding, restart reconciliation, complete shared-runtime integration and a matched comparison remain outstanding. The current runtime remains selected and no live provider calls were made for these checks.

## Durable candidate request ledger

Migration 138 adds tenant-scoped request reservations linked to existing goal attempts. `DurableAttemptBudget` commits a unique request identity and conservative charge before dispatch; concurrent ledger objects share the same database allowance. Reconstructed objects retain charges and reject duplicate request identities. Exact usage settles once, unknown usage remains reserved, and overrun charges commit before the response is rejected. Closed attempts require explicit reconciliation rather than allowing a late worker to rewrite already-accounted totals. Existing lease recovery transfers interrupted charges to the goal and its parent.

Candidate request wrappers now await ledger operations in worker threads, preserving event-loop responsiveness during database access. Four private-database adapter tests exercise both pinned frameworks: usage survives reconstruction and finishes into the goal total, and a durable pause denies the model call before any business effect. Four additional storage cases cover competing workers, duplicate IDs, unknown/conflicting usage, overrun persistence, expired-worker recovery, parent limits and cross-tenant denial. Migration application is checked twice for re-entrancy.

All 62 goal tests pass (8.92s). A newly created environment installed only `candidates.lock` and passed all 31 candidate boundary/database tests (6.55s); the lock now includes the OpenAI transport and PostgreSQL driver required by these contracts. It reported one existing pytest configuration warning because pytest-timeout is not installed in this minimal environment. Ruff and diff checks pass. No shared database, provider or production migration was used.

This proves explicit candidate budget/goal-store binding, not admission through the full `AgentRuntime` interface. Runtime result persistence, checkpoints, memory, durable tool controls during a model call, and matched comparison remain required. The native scheduler still serializes goal coordinators per tenant; the concurrency evidence covers delegated requests within a goal attempt, not a newly enabled parallel-goal scheduler.

## Durable candidate tool admission

The experimental `GoalGateway` wraps a host-supplied business gateway and checks its tenant and live goal-attempt authority asynchronously before admission and again before tool invocation. The ledger reuses the same durable lease/status/settings and parent authority checks for provider reservations and business actions. A recovery-required goal cannot execute the candidate's business tools before explicit reconciliation. Authorization remains separate from usage settlement, so a model request already completed when Pause arrived is still charged accurately.

The clean pinned environment passes 41 candidate checks (3.13s). New cases commit pause, cancel, lease expiration or recovery-required state during the provider call: both frameworks deny the returned action with zero host dispatches/writes and retain the reported 13-token charge. Cross-tenant gateway use fails before any model call. Twenty-eight native runtime and durable-store checks also pass (2.37s), along with Ruff, formatting and diff checks.

This is a tested goal-tool boundary around the supplied host dispatcher. It does not yet establish full shared-runtime result/checkpoint/memory integration, candidate Stop latency, or reconciliation of external actions already dispatched before a control command. No provider calls or production changes were made.

## Shared candidate request/result bridge

`CandidateRuntime` now accepts `RunRequest`, emits `ProgressEvent`, returns `RuntimeResult` with the actual pinned framework identity, and exposes host-committed pause/cancel controls. A required trusted host interface owns admission, persistence, authorized gateways and terminal recording. The bridge validates returned run attribution, bounds execution by the inherited deadline and a 60-second cap, and cancels its progress task on every terminal outcome. Candidate loops defer their timeout to the enclosing host deadline rather than installing a second execution owner. Gateway admission also rejects expired deadlines.

Unknown usage is represented explicitly as `None` in the shared `Usage` contract. Native results remain numeric as before. Failed candidate calls cannot invent zero provider calls or zero cost; host implementations must persist the separate usage object rather than interpreting legacy `AgentRun` numeric defaults as evidence. Completion requires both the candidate report and the host gateway's independent verification.

The clean pinned suite passes 56 checks (4.56s), including actual framework completion through the bridge; attribution and runtime identity; failure, timeout and durable-host-control cancellation; unknown usage; rejected checkpoint replay; failed persistence/identity/deadline admission before model work; and progress-delivery failure without duplicate execution. Nineteen native runtime/deadline contracts pass (2.37s); Ruff, formatting and diff checks pass. CI includes the bridge suite.

The bridge's lifecycle tests use an explicit in-memory host. The separate private-database gateway and ledger tests remain evidence for those components, not evidence that a production host has connected them all. A production-store host binding, compatible checkpoints, memory integration, complete control propagation and matched comparison remain unfinished. Checkpoint requests are explicitly refused rather than silently replayed. No candidate is selected for production.

## Candidate run-store host

The experimental `StoreHost` records candidate admission and terminal results in existing `agent_runs` and uses existing durable runtime controls. The required trusted gateway factory owns principal/options/tool authorization; the store host does not invent a business-tool allowlist. Its run gateway rechecks durable request/run/ancestor stops before model and tool admission. Cross-tenant parent references are rejected. Terminal updates require the matching tenant, framework version and checkpoint version and a still-running row, preventing a late finalizer from replacing a terminal outcome.

The private database applies real engine, subagent, verification and runtime-control migrations (011, 016, 100 and 127), plus the agent-run identity additions from 037. This caught the bridge's invalid `unverified` verification status; failed verification now uses the existing `failed_verification` value. Unknown usage is persisted as SQL NULL and in runtime metadata, rather than zero. Text request IDs remain in runtime metadata when the legacy UUID correlation column cannot represent them. Remote durable stop denials now produce a cancelled, unresolved terminal result through a typed stop exception.

All 65 candidate checks pass in the clean pinned environment (5.24s), including real-framework completion and persisted attribution, unknown cost, remote stopping during a provider call, pre-admission request stops, parent stop inheritance, cross-tenant parent denial, failed verification and late-finalizer rejection. Twenty-six native runtime/control/deadline checks pass (5.25s). Ruff, formatting and diff checks pass. Initial schema-test setup errors were confined to the disposable fixture's encoding/connection handling and were repaired without changing production migrations.

This binds the candidate lifecycle to the run/control store. It does not yet wire the ordinary production manifest/tool-dispatch/memory admission path or automatically bind goal-family budgets through the whole lifecycle. Checkpoint compatibility/recovery, in-flight external-effect reconciliation and matched performance comparison still require work. The host remains experimental and is not selected by the running installation.

## Interrupted run admission

The candidate store host now shields and retains the run-insert task when its awaiting request is cancelled. Reconciliation durably stops the request, waits for the insert's outcome, then marks any committed row cancelled. A lost commit acknowledgement is handled by attempting terminal reconciliation rather than assuming no row exists or repeating the insertion. A shutdown/test drain hook exposes retained admission work; reconciliation failures are logged. The original cancellation/deadline result returns without waiting on the delayed database write.

Four private-database cases combine cancellation/deadline expiry with normal/lost insert acknowledgement. Each releases the delayed worker, drains reconciliation, and verifies a cancelled row with zero model calls and zero business effects. Zero usage is justified here because execution never reached the provider; uncertain provider usage remains unknown in the other contracts. All 69 candidate checks pass (6.62s), along with Ruff and diff checks. This checks interruption within a live process; process-crash recovery and the production service's shutdown wiring are still separate incomplete requirements.

## Candidate process-loss recovery

The experimental host can now recover expired candidate runs atomically: row locks prevent competing recovery workers from both taking a run; durable run/request stops and an unresolved timeout result commit together. Recovery does not invoke a model or a business tool. It preserves known usage and marks unreported usage unknown. Native runtime rows, other tenants and unexpired runs are excluded. Late finalizers remain unable to replace the recovered terminal record, and the stopped request cannot be implicitly admitted again.

A subprocess fixture runs each real pinned framework with a synthetic gateway, commits one receipt to the private database, then exits with `os._exit(23)` before framework cleanup or final-result persistence. A fresh host sees the surviving running row. After the fixture's deadline is expired, two concurrent recovery calls recover it exactly once, preserve its sole receipt and deny replay without model calls. A separate case checks native/tenant isolation and preservation of already-reported usage.

All 72 candidate checks pass in the clean pinned environment (10.53s); Ruff and diff checks pass. The subprocess uses only the disposable PostgreSQL cluster and synthetic tools. This demonstrates conservative process-loss recovery, not compatible checkpoint continuation or a completed external-provider reconciliation workflow. The recovery hook is not scheduled by the production service, and ordinary manifest/tool/memory/goal-lifecycle integration and matched comparison remain unfinished.

## Observed comparison configuration

The controlled candidate screening now forwards the existing manifest's temperature to both frameworks. Pydantic previously used a hard-coded 0.5 even when the manifest differed. Actual mock-HTTP capture also revealed different default tool strictness and tool choice; controlled screening explicitly uses strict tools and automatic tool choice for both, via public model settings and middleware. The standalone adapters retain their defaults unless the host supplies those settings.

Two transport-level cases use temperatures 0.2 and 0.9 and assert equality of the entire initial provider JSON payload from the two installed frameworks: model, messages, tool schema/strictness, output limit, temperature, streaming and tool choice. Future candidate screening records per-request observed model/settings and prompt/tool hashes through an HTTP event hook. It does not retain authorization headers or raw prompt text. The isolated dependency lock now includes the existing LangChain OpenAI transport and its tokenizer dependencies needed to reproduce these checks.

All 74 candidate tests pass (7.66s), with Ruff and diff checks passing. No live provider call was made. This establishes controlled configuration equivalence for the candidates' initial synthetic action request; it does not retroactively qualify prior live runs, prove native-runner equivalence, or compare performance. A matched native cohort and representative mixed workload remain required.

## Comparison failures and missing measurements

The comparator accepts explicit nulls for unobserved measurements while still requiring valid elapsed time and independent state checks. Such rows remain in outcome counts and the full latency population. Metric summaries report known and unknown sample counts; unknown values are not imputed as zero. Missing required measurements in either baseline or candidate block replacement qualification, and optimization reports do not claim reductions from absent usage. Optional metrics such as unreported cost remain visibly unknown. Optimization reports also expose whether both cohorts completed every sample.

The candidate screening producer now records observed HTTP attempts even when a framework raises before returning usage. Failed provider requests retain null token/cost fields and observed request fingerprints. Queue delay is recorded separately; total latency includes queueing and adapter construction/execution, with the framework's narrower timing retained separately. A mock-503 test exercises the actual producer for both candidates, confirming one HTTP attempt each, failed outcomes, no effects and unknown usage without credentials in the artifact.

Twenty-four comparator/size checks pass (5.35s), and three transport/producer checks pass (1.19s); Ruff and diff checks pass. These are local mocked checks, not new live-provider samples or a replacement qualification. Historical artifacts remain unchanged.

## Native provider configuration parity

A new native-loop test intercepts actual HTTP transport, supplies a synthetic tool-call response and verifies one completed fixture write with no subsequent request. The observed DeepSeek configuration used 16,384 output tokens and an 8,192-token thinking budget, rather than the earlier candidate screen's 512-token output limit. The native prompt also contains two separate system messages. Those previous profiles must not be compared as matched workloads.

A bounded single-action configuration adapter now passes the captured native prompts and provider-specific output/thinking parameters through each framework's public APIs. Deep Agents retains both system-message boundaries. The native capture is produced in one isolated environment and consumed by the candidate environment. Transport assertions compare every JSON field, normalizing only omitted `stream` versus explicit `false`, the documented [Chat Completions default](https://platform.openai.com/docs/api-reference/chat/create). The first Pydantic check exposed an extra null `max_completion_tokens` field; host-specified omissions now remove adapter defaults rather than serializing a conflicting null limit.

The native HTTP capture passed; all 77 candidate tests, including comparisons against that captured payload, pass (9.38s). Ruff, formatting and diff checks pass. CI performs the same native-capture/candidate-check sequence. `uat-native-provider-configuration.json` retains configuration and hashes without prompt text or credentials. No live provider calls occurred.

This proves initial provider-request parity for the supported synthetic action. It does not prove multi-turn prompt evolution, equal complete runtime services/resources, performance targets or framework qualification. Production model settings remain unchanged; further comparison must follow the observed native configuration.

## Candidate goal-controller lifecycle binding

The candidate store host now requires the existing trusted goal binding and a positive host-supplied per-request token bound for goal execution. It validates the parent and family-budget identities against durable goal state before admission, wraps the host gateway with goal authority checks, and binds a per-execution model wrapper to the same durable ledger used by the controller. The base candidate is copied rather than mutated across requests; configured prompts and model settings are retained. Goal runs attach to the existing controller's run registry and attempt record, preserving goal attribution and the cron trigger in `agent_runs`.

Native provider-budget helpers now support asynchronous ledger operations and uncapped goal accounting while preserving the existing in-memory path. Candidate and delegated native provider calls therefore reserve from one durable attempt allowance. The existing controller finalizer reads those charges: observed usage settles normally, and an uncertain provider failure retains the reservation instead of being overwritten with zero. A verified fixture action does not itself complete the enclosing goal criteria.

The candidate suite passed 99 tests (10.83s). After correcting the uncapped test fixture to actually omit the child budget too, all 22 lifecycle cases passed again (4.20s): both frameworks, success/provider failure, parent/child and capped/uncapped goals, invalid identity/bound rejection, and native/candidate requests competing for one allowance. The native goal/runtime/size suite passed 76 tests (11.28s). Ruff and diff checks pass; CI includes the lifecycle cases. All databases and providers are private/synthetic.

The conservative per-request bound still comes from a trusted host; automatically deriving it for every supported model/modality is not established. Ordinary manifest/tool authorization and memory admission, compatible checkpoint continuation, representative matched performance and user acceptance remain incomplete. No production admission or model configuration changed.

## Prepared native tool gateway and regression follow-up

The broad native engine run after durable goal-budget binding passed 10,888 tests, with 29 skipped and 173 deselected (358.90s). The selection excludes slow, integration, LLM, end-to-end and smoke tests. It emitted 398 warnings, including an unawaited coroutine warning; this is a passing test result, not a warning-free execution claim.

An experimental direct-tool gateway now reuses `RunToolProxy` for native plan-mode admission, hooks, guardrails, dispatch identity and step recording. It requires a host-prepared session, resolved principal role and guardrail service. The store host validates and binds that session to its persisted run before execution, preserving the role, accessible tenants and benchmark marker. A trusted state predicate supplies verification; a successful tool response alone cannot establish completion. Schemas are copied and constrained to prepared authority, and calls remain bounded.

The gateway refuses delegation, nested execution and deferred dispatch schemas. Approval-requiring calls are refused until candidate approval/session lifecycle support is implemented. It does not register a live interactive session or implement native manifest/memory bootstrap. Durable stop checks still wrap the gateway in `StoreHost`; local session interruption is also honored. This is a tested integration seam, not full service equivalence or production admission.

Thirty-eight gateway/proxy checks pass (1.48s), and six size checks pass (2.26s). The new store-binding check initially exposed an eager optional-framework import in goal binding; imports now occur only when wrapping a candidate model. The native environment can prepare and test the gateway without installing either candidate framework. CI includes the new checks. All tool effects in these checks are synthetic.

The full pinned candidate suite also passes after these changes: 99 tests in 14.54s. Its one warning reports the absent optional pytest-timeout plugin; adapter deadlines remain exercised by their own contracts. Ruff, formatting and diff checks pass. Full acceptance, candidate selection and user acceptance remain pending.

## Installed frameworks through native dispatch

The combined test now runs each installed framework through `CandidateRuntime`, `StoreHost`, the prepared native gateway, `RunToolProxy`, the real tool registry/dispatcher and private PostgreSQL run/step/receipt tables. Providers and the business handler are synthetic; permission decisions and the external audit sink are intercepted. This exercises dispatch policy enforcement, not a production RBAC database or external provider.

This exposed missing durable step publication in the first gateway implementation. The gateway now uses the native idempotent step writer before returning a tool result. A lost step acknowledgement fails the run as unresolved and stops subsequent calls instead of repeating a business effect. The native best-effort session flush is deliberately not used here because silently losing the step would hide this failure.

A two-proposal response also exposed a second dispatch after verified success in both frameworks: the fixture database prevented a duplicate row, but dispatch counts were `[1, 0]` rather than `[1]`. Both failing cases are retained in the verification record. Verification is now checked inside the gateway's serialized call boundary; queued calls return without dispatch after success. An interrupted/failed call latches unresolved state, preventing a queued call from treating a committed receipt as an entirely successful execution.

The 16 combined cases cover success, duplicate proposals, plan mode, permission denial, guardrail refusal, durable stop during provider work, lost step acknowledgement and another queued proposal after that loss, for both frameworks. They pass in a fresh environment installed from the native project requirements plus the pinned candidate lock. CI has a separate combined-environment check. The native gateway/proxy/size checks also pass (44 tests, 3.67s). These changes do not establish manifest/memory bootstrap, live approval handling, compatible resumption or general external-write reconciliation.

Final combined verification: 115 tests passed in 17.06s, including the 16 native-dispatch cases and the existing 99 candidate cases. Native wire capture was regenerated locally with intercepted HTTP (1 passed, 2.65s); no provider request left the test transport. Ruff, formatting and diff checks pass.

## Uncertain write responses and normal-chat acceptance

Four more native-dispatch cases simulate a business write followed by a lost provider reply. If the independent host predicate can verify the result, both candidates finish after one dispatch. If the receipt is unavailable, both initially repeated dispatch; those two failures are retained in the verification record. The gateway now stops on an unverified error from an effectful dispatch, preserving its step before reporting uncertainty. A queued proposal is also denied. Native pre-admission refusals retain their structured result, and declared read-only tool failures remain retryable within existing bounds. The host still needs an appropriate independent verification predicate; this does not implement every provider's reconciliation protocol.

The user explicitly requested acceptance review in normal chat instead of the goal-workspace preview. `test_runtime_chat_goal_acceptance.py` now exercises the normal chat route, native runner and real goal handlers against private PostgreSQL. Its two messages ask what remains and then request a pause. The stored goal remains unfinished after the status question and becomes paused after the explicit request; linked tasks remain DONE/TODO. `uat-chat-goal-review.json` contains the delivered transcript. Responses are scripted from asserted tool state, so this proves chat/tool/state wiring and offers a wording/behavior review, not live model understanding. User acceptance remains pending.

An initial test setup patched the runner instead of its separate LLM client, inadvertently sending requests for invalid fixture model names to the configured provider. Both model names were rejected; no business handler ran. The setup is corrected at `LLMClient`, and an explicit failing `litellm.acompletion` stub plus a zero-call assertion now guards the test. No claim of zero provider attempts applies to that initial failed setup, and billing was not measured. Subsequent corrected runs use only the scripted model.

Final checks: 119 combined candidate tests pass (19.13s); 46 native chat/gateway/proxy/size tests pass (6.80s), with one upstream Pydantic warning. Ruff, formatting and diff checks pass. CI includes the normal-chat check. No production deployment, goal resumption or historical business changes occurred.

## Accepted chat behavior and a live-discovered deadline defect

The user accepted the normal-chat unfinished-work explanation and explicit pause preserving the open task: “Yes, that matches what I expect.” This acceptance is recorded on `uat-chat-goal-review.json`; broader modernization acceptance and deployment remain unapproved.

An explicitly invoked two-turn diagnostic used the configured primary `openrouter/deepseek/deepseek-v4.1-flash` through the native chat route, with an isolated minimal manifest/workspace and only private goal tools. It read the remaining task and paused the correct goal, but the first response took 64.014s despite an intended 60-second host deadline. The initial test asserted state rather than elapsed time and passed; this audit rejects that as deadline acceptance. `uat-chat-goal-live-primary.json` preserves the original transcript, timings and seven provider attempts.

`runtime.chat_control.start` created a new request context without preserving the host deadline. A focused regression test reproduced the missing deadline before the fix. Chat admission now preserves a same-tenant/same-principal host deadline while keeping its own request identity; it does not inherit a different principal's deadline or goal authority. The real deadline wrapper cancels blocked work in the new test. This does not introduce a blanket 60-second limit for all ordinary chat requests.

The initial live diagnostic also reported a discrepancy because goal tools read the private task store while the warmup's task source was an empty fixture. Both fixture surfaces now read the same tenant's private task state. The corrected diagnostic, `uat-chat-goal-live-primary-after-deadline.json`, completed its status and pause turns in 15.888s and 22.619s respectively, again with seven provider attempts and unchanged DONE/TODO tasks. This is a new diagnostic after concrete changes, not a replacement for the failed observation or a p95 comparison. Its estimated native-accounting cost is $0.0025; no billing reconciliation is claimed.

Twenty-one deadline/control/chat checks pass (6.29s); 43 native HTTP/chat/runtime/size regression checks pass (30.23s), including the existing 30 acknowledgement/stop repetitions. The scripted default still forbids external model calls. Live mode is explicit, reads the selected chain from the supplied manifest, caps provider attempts at twelve, retains each completed turn before assertions, and restricts mutation to pausing the one isolated goal after the explicit pause request.

The live language review is not exhaustive: the model's scheduling explanation does not establish that every event-based wake or disabled-coordinator condition is represented correctly. At that revision, the goal read tools omitted the tenant's execution-enabled setting; the follow-up below addresses this omission.


## Execution settings and explicit wake conditions

Goal reads now report the tenant's execution-enabled setting. A waiting goal also exposes its scheduled review, linked-task changes and optional matching event as explicit alternatives; paused goals expose no active wake conditions. The descriptions distinguish waking from permission to execute. Reads do not enable pursuit or resume work.

Private-database tests cover tenant isolation, absent settings, changed settings on subsequent reads, unchanged paused state, and a real linked-task event that cannot be claimed with execution disabled but can be claimed after enabling the private fixture. The goal/chat/size suite passed 73 tests (14.95s); tool registry/admission/plan checks passed 144 tests (5.07s).

Two further configured-primary diagnostics are preserved. `uat-chat-goal-live-execution-setting.json` completed in 47.779s and 41.582s, correctly identifying disabled execution but incorrectly implying only a scheduled review could wake the goal. After explicit wake metadata, `uat-chat-goal-live-wake-conditions.json` completed in 45.112s and 33.642s and described both scheduled and linked-task wakes. Both paused only the private goal and retained DONE/TODO tasks. Each used seven provider attempts. These observations exceed the 30-second simple-action reference and do not establish a p95 or performance qualification.

The latter transcript still calls a future wake condition “satisfied” and speculates about engine health based on private test run rows. It is not accepted as fully correct conversational behavior. The fixture now explicitly supplies unavailable engine-health context instead of presenting synthetic run history as installation health. No additional live sample was taken after that fixture correction. State assertions alone do not establish prose accuracy; the earlier failed explanations remain in the artifacts. The user's accepted scripted status/pause case remains accepted, while broader acceptance stays incomplete.


## Wake metadata checked against scheduler behavior

The follow-up now states explicitly that registered triggers are not evidence of a fired trigger and enabling pursuit does not make a future review due. Goal reads expose the event registration cutoff and the explicit task-event matcher as well as linked-task changes and named event matching. This reflects existing scheduler behavior; scheduling and authorization were not changed.

Three private-database contract cases exercise named events, task-addressed events, and scheduled reviews through actual scheduler claims. Each starts with enabled execution and a future review, proves a matching event from before registration cannot wake the goal, then proves a fresh matching event or a due review permits exactly one active claim. Wrong event payloads are rejected. The goal/chat/size suite passed 76 tests (17.12s, three dependency warnings). Ruff and diff checks pass. No live provider calls were used for this follow-up; whether the model consistently follows the improved description remains unverified.


## Checkpoint reload failure cannot restart native work

Recovery inspection found a gap in the current engine, independent of the candidate resumption work: the runtime adapter checks a checkpoint at admission, then the native lifecycle loads it again. A missing or malformed conversation on the second load previously returned normally, allowing execution to continue from the newly prepared conversation. Three new cases reproduced that fallthrough before the fix (three failed, one passed).

The lifecycle reload now supplies the session tenant and raises a recovery error if loading/restoration fails or the saved conversation is empty or not a list. The normal runner failure path records this error instead of entering the action loop. A compatible conversation with no optional scratchpad still restores. This is conservative failure handling, not checkpoint migration or candidate continuation.

Verification: 41 runtime/checkpoint/restart/size checks passed (6.75s); all 80 existing runner checks passed (6.17s, one upstream warning). A subsequent five-case recovery suite (3.15s) includes the actual native runner and verifies FAILED status, explicit recovery error, zero provider calls and zero registry dispatches when the second load disappears. The initial positive fixture exposed its missing originating-message setup after errors stopped being swallowed; the fixture now supplies the same setup as the runner. CI includes the new recovery suite. No external provider or business operation was invoked.


## Approved-plan terminal outcomes in chat

Normal chat already used the shared terminal-result formatter, but approved-plan execution did not. Both ordinary and deep-plan branches preferred partial output over FAILED/TIMEOUT/CANCELLED status, saved that output as the assistant response, and omitted status from the final event. A deep-plan failure with partial text also emitted a successful deep-result event. Eight initial route cases failed: the six unsuccessful cases exposed misleading text, while the successful cases lacked explicit terminal status.

Both approval branches now use the existing result formatter for final chat text, in-memory history and persisted exchanges. The final event includes the actual run status. Deep-result events are restricted to completed runs; unsuccessful deep runs send an error followed by the terminal explanation. Existing successful text remains unchanged, and empty successful output does not create a persisted response. Repeated deep terminal-event construction was consolidated to satisfy the existing function-size limits; those limits were not raised.

Sixteen route cases cover normal/deep execution, completed/failed/timeout/cancelled outcomes, and empty/nonempty partial output. They use synthetic terminal runs and mocked persistence, not live providers or business actions. Together with chat, per-user isolation, plan-mode/integrity and size checks, 144 tests passed (9.87s). Ruff, formatting and diff checks pass. CI includes the new route suite. Browser rendering and user acceptance of these failure explanations are not established by these route tests.


## Browser verification and interrupted plan streams

The rebuilt standalone dashboard passed 16 browser cases, including six new ordinary/deep approved-plan FAILED/TIMEOUT/CANCELLED responses replacing earlier partial success text. These use browser-intercepted synthetic SSE streams and isolated server URLs; they verify rendering, not a live business provider.

Inspection found another incomplete-outcome path: a stream ending before `done` previously committed its partial text as the final response, and fetch/HTTP failures could disappear silently. Two new browser cases reproduced the former in both plan modes. The client now requires a terminal event before treating the stream as final and shows “I couldn’t confirm the outcome. Some actions may have finished; check their status before trying again.” on premature stream completion or request/transport failure. It removes the approval card without resubmitting the action. Explicit local abort handling remains on the existing Stop path.

After the fix and a fresh production build, all 20 deep/plan browser cases passed (23.7s), including premature EOF and transport failure in both modes. The tests assert the terminal explanation, absence of partial success, cleared approval card, usable chat input and exactly one approval request. Twenty-three existing plan/approval-card component checks also passed (1.64s). Build, ESLint and diff checks passed. The isolated browser runner reports expected failed backend requests because non-intercepted calls point to deliberately unavailable test ports; no production backend is used. CI now builds and runs this browser suite. This is automated browser evidence, not additional manual user acceptance.


## Ordinary-chat interrupted outcomes

The same interruption audit now covers `/chat/send`. Four new component cases reproduced partial-success retention or a blind retry suggestion on premature EOF, transport failure, server abort, and an empty failed terminal response. A shared terminal-outcome interpreter now handles ordinary and approved-plan chat. Successful responses retain their text; an explicit completed empty response does not reuse partial text; legacy empty terminal events after error text remain compatible. Missing terminal acknowledgement reports the outcome as unconfirmed and suppresses the ordinary chat's follow-up dashboard update. Transport failures no longer suggest blindly retrying a possibly dispatched action.

The focused component suite passed 31 tests (1.35s); all five chat-panel component files passed 50 tests (1.40s). The initial reproduction retained four failures and four passing cases. ESLint and the production build pass. The new browser scenarios exercise the actual rebuilt dashboard with intercepted SSE responses, preserving isolation from real providers and business services. Manual acceptance of this uncertainty wording remains pending.

Final browser verification passed all 25 cases (28.8s), including the five new ordinary-chat outcomes and the existing plan/deep cases. Each new case checks final text, removal of partial success, usable chat input and one send request. A normal-chat acceptance question for the uncertainty response has been sent to the user; no answer is assumed.


## Broad regression rerun and watchdog test evidence

At revision `04e9a05d10d`, the full frontend suite passed 1,622 tests across 146 files (10.36s). The jsdom navigation notice remains; this is not a warning-free test claim.

The previously recorded unawaited-coroutine warning was traced to `test_no_watchdog_bound_is_harmless`: a synchronous mock side effect returned another coroutine, while the test only checked that the result was non-null. Strengthening the assertion reproduced an AttributeError on that coroutine. The mock now returns the actual response, and the test checks response content and exactly one awaited provider call. All 14 watchdog wiring/semantics checks passed (12.47s), with one upstream Pydantic warning and no unawaited coroutine warning. This fixes misleading test evidence, not production model-call behavior. The broad engine invocation began before this test-only correction, so its result is recorded separately from the focused corrected checks.

The broad engine run at `04e9a05d10d` completed with 10,933 passed, 29 skipped and 173 deselected in 355.77s. Its 398 warnings include the original watchdog mock warning; the corrected focused run above is separate. The selection excludes slow, integration, LLM, end-to-end and smoke markers. These broad results precede the following audit-recovery feature.


## User correction: recover the audit record automatically

The user rejected the proposed fallback that asked them to check action status: Robothor should consult its audit log itself. The previous pending manual acceptance is therefore not accepted. The local behavior now reads the original request's durable run record instead of ending with that message.

A new authenticated GET `/chat/outcome` binds the client request UUID using the same tenant/principal/session key as admission. It selects the matching root run, excludes child runs and other principals/tenants, returns current run state and terminal text, and never dispatches work. Runtime-context request identity supports runs without a legacy correlation value. Missing and multiple matching root records are not treated as success. Responses bypass caching. The Next.js route reuses the existing agent authorization and credential forwarding.

Ordinary and approved-plan chat now mount a background read-only recovery view after lost delivery, empty failed/aborted terminal messages or transport errors. It looks up the same request, polls with capped backoff while running/unavailable, and replaces the message with the recorded terminal response. New chat input remains available and the action is not resubmitted. This recovery view is tied to the current mounted conversation; preservation across a full browser reload and general provider-side receipt reconciliation are not established here. Reading a run response is not a new independent verification of every external business effect.

Verification: 80 backend recovery/control/plan/session/size checks passed (7.62s), including real private PostgreSQL reads, principal/tenant/session isolation, partial-output rejection and HTTP recovery after clearing the in-memory chat cache. The HTTP setup initially tried to initialize an asynchronous pytest fixture inside an active loop; that fixture wiring was corrected before the passing run. Two BFF refusal checks initially expected nine routes and were updated to include the tenth route, with all refusals still enforced. Seventy-seven client/route/component tests passed (1.44s), covering authenticated GET forwarding and no execution call. The rebuilt dashboard passed 25 browser cases (43.7s); recovery scenarios read a running record then its completed result, assert the same original request ID and exactly one original send/approval. Browser streams and outcome responses are synthetic; backend persistence is exercised separately. Build, ESLint, Ruff, formatting and diff checks pass. No production deployment, real business action or model request occurred.


## Audit recovery outage and conversation-lifecycle checks

Three additional component contracts exercise temporary audit-service failure followed by missing/running/completed records, prolonged outage backoff, and a late response after the conversation unmounts. The first restores the original recorded answer after four GETs with the same request and agent, then makes no further calls. The outage case verifies delays of 1/2/4/8/10/10 seconds using a fake clock and proves unmount stops subsequent polls. The cancellation-resistant transport case resolves after unmount and cannot deliver an old conversation's result. These checks add evidence for automatic read-only recovery without changing production code.

All six chat component files passed 53 tests (1.55s). ESLint and diff checks pass; the runtime browser CI job now also runs the recovery, chat-panel, route-authorization and engine-client component contracts. This is mocked transport/lifecycle verification, not live outage latency certification or proof of browser-reload persistence.


## Failed calendar readback remains pending

Following the user's audit-recovery correction, inspection of the supported calendar operation found that an interrupted write's GET response was used without checking for provider errors or the requested event identity. An error or unrelated/malformed event could be recorded as “reconciled” with no attendees present, move the operation to blocked, and prevent another read through the existing recovery path. Three private-cluster cases reproduced that behavior.

Interrupted-write reconciliation now validates the read response and event identity before reporting attendee facts. An unavailable or invalid read records `reconciliation_pending` in the operation result and retains the executing uncertainty barrier. It does not create a human-repair task merely because this read failed. A subsequent valid read of that same operation can establish attendee presence without dispatching another write. Notification delivery remains explicitly unverified; observing attendees does not prove notification delivery. This fixes the existing reconciliation path; it does not establish automatic scheduling for every provider or bind every business receipt into the new chat outcome endpoint.

The initial three cases failed; after the fix, 21 readback/attendee/size checks passed (6.38s). All 10 existing calendar-operation checks passed (0.68s) using private schemas in the guarded test database and fake Google transport. New cases use a disposable PostgreSQL cluster and synthetic event data, assert persisted pending state, and perform only GETs during recovery. CI includes the private-cluster cases. No historical meeting or production provider was touched.


## Chat recovery separates run status from recorded action evidence

The recovered chat result now reads calendar-operation references from the original run's direct `gws_calendar_add_attendees` tool audit and joins the durable operation store under the same tenant, principal and agent. It reports those recorded action facts alongside the run status. An interrupted run can therefore show a completed, verified calendar change without claiming the whole run completed. Requested notifications remain distinct from verified delivery. This reads existing evidence only; it does not issue a new provider call or write.

A completed run's prose cannot override blocked, executing or unmatched operation evidence. The first receipt implementation omitted unmatched references and still allowed the original success prose; three strengthened cases reproduced that false evidence acceptance. Missing/foreign/conflicting references now produce unverified evidence instead. UUID references are normalized, and invalid reference text is not reflected into the chat explanation. Only explicit direct calendar-tool references are covered; deferred-tool wrappers, other effect types, and independent fresh provider reconciliation remain incomplete.

Final verification passed 42 recovery/calendar/approved-plan/size checks (5.21s). The tests use private PostgreSQL, include actual calendar-operation result creation through a fake provider, and check interrupted-run success receipts, contrary evidence overriding prose, principal/agent isolation, conflicting references and equivalent UUID encodings. Existing operation IDs and state are not changed by recovery reads. Ruff, formatting and diff checks pass. No production effects or deployment occurred.


## Deferred calendar audit recovery

Receipt recovery now recognizes calendar actions invoked through `tool_call`, using the dispatcher's explicit target and nested arguments with its directly returned result. Other deferred tools and invalid argument shapes cannot supply calendar receipts. Existing principal/tenant/agent scoping and contrary-evidence handling remain in the shared reader.

Two new private-database checks initially failed because deferred receipts were omitted. After the fix, 32 recovery/calendar/size checks passed (4.44s), including six new deferred cases. Ruff passed. This extends recorded calendar evidence coverage; it does not establish general external-service reconciliation, fresh provider verification, or full acceptance. No model requests, production effects or deployment occurred.


## Continue receipt recovery after a terminal run

Chat recovery previously stopped polling as soon as a run was terminal, even when a referenced calendar operation remained executing. Four new backend assertions and a component scenario reproduced the missing distinction and premature finalization. The outcome endpoint now exposes `reconciliation_pending` separately from run status. Chat displays the available recorded explanation and continues its read-only backoff until that pending state clears. Completed, draft and blocked receipts do not cause indefinite pending polling; a cancelled run stays cancelled even when its action evidence later becomes verified.

Private PostgreSQL/backend/calendar/size verification passed 36 tests (5.05s). The full frontend suite passed 1,629 tests in 147 files (10.80s), with the existing jsdom navigation notice. The production build, Ruff, ESLint and diff checks passed. The first browser invocation correctly refused an occupied port; the existing service was left untouched and a separate port selected. This change observes durable evidence updates; it does not itself schedule provider readback. Automatic provider reconciliation, browser-reload recovery, and the broader runtime comparison remain unfinished.

The rebuilt isolated browser suite passed all 26 cases (47.3s), including a cancelled run whose calendar evidence changes from pending to verified. It verifies the same request identity, one original send, updated chat text and enabled input. Browser responses are synthetic; backend persistence is verified separately.


## Automatic scoped calendar readback during chat recovery

The recovery endpoint now schedules background provider readback when the original authenticated run has ended and its recorded calendar operation remains executing. The background job re-resolves the scoped original request, then rechecks operation tenant/principal/agent ownership and executing state. It shares the writer's nonblocking resource advisory lock, rechecks under that lock, and durably records a ten-second read cooldown. It invokes only the fixed calendar GET reconciliation path, never the action dispatcher, confirmation path, agent runner or repair-task creator. Transient failure leaves the executing uncertainty barrier intact; another recovery poll can schedule a later read.

A successful read records observed requested attendees and leaves notification outcome unverified. Chat now includes those observed attendees in its recovered explanation instead of hiding them behind generic unverified status. The operation remains blocked when notification outcome cannot be established; the run's cancelled/failed status is preserved. Readback must not assert that attendee presence proves notification delivery.

Verification passed 102 backend recovery/calendar/plan/session/size checks (7.92s), followed by 13 readback checks (1.37s) including two added background state-revalidation cases. Ruff/format/diff checks pass. Private PostgreSQL plus fake Google tests cover read-only recovery after failure, durable cooldown, tenant/principal/agent refusal, draft/completed refusal and writer-lock contention. An HTTP/private-database test follows pending status through an automatically executed background readback to a subsequent evidence-bearing answer, asserting one readback and zero agent execution calls. That HTTP test substitutes the provider readback; the separate worker tests exercise the actual readback against fake Google. No production provider, historical meeting or deployment was touched.

This is automatic recovery while chat polls the original request. A durable unattended reconciliation scheduler, recovery after browser reload, general provider coverage and full runtime acceptance remain incomplete. Production deployment is not authorized or performed.


## Calendar recovery without an open chat

The daemon now owns a tenant-scoped recovery worker that reads durable executing calendar operations without a chat session or model call. Each sweep selects at most five oldest eligible records, leaves fresh attempts alone for 30 seconds, and uses the same scoped readback and nonblocking resource lock as chat recovery. Failed reads update the attempt time, allowing the next batch to reach other pending records. Drafts and completed operations are not executed or replayed.

Each batch runs in a separate Python process, with only the tenant identifier passed as an argument. This avoids adding provider IO threads that can hold interpreter shutdown open. The daemon supervises one batch at a time, terminates it on cancellation, and escalates to kill after one second if necessary. Database write-ahead state survives process interruption; later startup resumes reads from that state. The worker joins the existing daemon task lifecycle. No shared daemon was started or deployed during verification.

Verification: 70 recovery/chat/daemon-shutdown/exit/size checks passed (12.11s), followed by 18 focused recovery cases (1.54s) after adding batch fairness coverage. Private PostgreSQL and fake Google demonstrate unattended state recovery, tenant refusal, failed-read cooldown, a five-then-two batch sequence for seven pending operations, and no PATCH calls. A real isolated sleeping child is terminated and reaped within a two-second test deadline; spawn-failure and cancellation behavior are also exercised. Module CLI bootstrap, Ruff, formatting and diff checks pass. The new cases are included by the existing CI recovery test file. Full service startup, production operating limits, broader provider reconciliation, browser reload behavior and overall acceptance remain unproven.


## Returned readback failures enter automatic recovery

Two new cases reproduced a gap beyond process crashes: the attendee tool could return normally after an unavailable post-write GET, and the durable operation became blocked rather than eligible for automatic readback. The tool now returns `reconciliation_pending` for this unavailable-read case, keeping the executing barrier and avoiding a premature human repair task. Both acknowledged writes and lost write acknowledgements are covered. Readback remains GET-only and does not replay the calendar change.

Reconciliation now retains an earlier acknowledged notification request through repeated read failures and successful attendee readback. It does not turn a lost acknowledgement into a known notification request or claim delivery. Recovered chat text distinguishes observed attendees, acknowledged notification requests and unverified delivery.

The initial two new cases failed. Final verification passed 67 calendar/readback/chat/size checks (7.18s) plus all 10 existing durable calendar-operation checks (0.59s). The private-database scenarios simulate the provider applying exactly one PATCH, returning or losing its acknowledgement, then failing two readbacks before recovery. Each finishes with observed requested attendees and exactly one PATCH. Ruff, formatting and diff checks pass. Tests used fake Google and isolated test schemas/databases; no real meeting, production provider or deployment was touched. Broader provider coverage and full acceptance remain incomplete.


## Broad regression verification after calendar recovery

At `b68204804a5`, the broad native engine selection passed 10,982 tests, with 29 skipped, 173 deselected and 396 warnings in 354.79s. Selection: `not slow and not integration and not llm and not e2e and not smoke`. The separate complete goal test directory passed 69 tests with two warnings in 9.27s. No introduced regression was observed in those scopes. The latest frontend run remains 1,629 passed across 147 files; subsequent changes are backend-only.

The engine warning summary still includes unawaited alert coroutines from loop-guard tests and an unawaited `connect_tcp` coroutine reported during a timeout-endpoint test. These need investigation; this passing run is not a warning-free claim. Logs are `/tmp/runtime-engine-regression-b682048.log` and `/tmp/runtime-goals-regression-b682048.log`. The accepted baseline remains at `eea3252b157`; its existing untracked benchmark fixtures were left in place. Neither the shared working checkout nor production was changed.

The machine-readable acceptance record now preserves its initial remaining-work snapshot separately and describes the current gaps using later evidence. Earlier MiMo and prompt-diagnostic failures are retained; the later configured-model cohort (primary 30/30, MiMo 29/30 with one unresolved deadline) does not qualify full runtime replacement or representative provider latency. The user's accepted normal-chat unfinished-work/pause case remains accepted. New recovery behavior, broader goal controls, browser reload, general provider reconciliation, candidate equivalence and rollout verification remain incomplete.


## Rejected background tasks release their coroutines

The broad-run alert warnings were traced to synchronous loop-guard tests calling alert dispatch without a running event loop. TaskRegistry.spawn propagated admission failure but left its unowned coroutine open. Two new lifecycle assertions reproduced this for no event loop and a rejected task creation. The registry now closes the coroutine when task creation fails, without executing it, and re-raises the exception. Successfully admitted tasks retain their existing tracking and drain behavior.

The three loop-guard alert tests now run in an event loop with a private registry and mocked alert sender. They assert actual awaited delivery once, including the soft-alert suppression on a second iteration, instead of only checking budget flags while dispatch silently failed. No operator message is sent by these tests.

Verification passed 37 registry/guard/timeout/size tests with RuntimeWarning promoted to error (4.43s; six dependency deprecation warnings). A broader focused set including runaway alerts, token guards and daemon shutdown passed 62 tests with the same warning policy (11.75s). Ruff, formatting and diff checks pass. The connect_tcp coroutine warning from the broad run did not recur in the isolated timeout-endpoint checks; its cause remains unproven and is not marked fixed. The earlier broad 10,982-pass run precedes this cleanup. Full acceptance and production deployment remain open/not authorized respectively.


## Scoped browser reload recovery foundation

History now returns an opaque recovery namespace derived from the authenticated tenant, principal and effective session, using structured identity encoding to avoid delimiter collisions. This namespace is a browser storage key, not an authorization credential. Both engine and frontend history responses disable caching. The frontend forwards only the engine-provided namespace, ignoring a browser-supplied recoveryScope parameter.

A sessionStorage journal stores only pending UUID request identifiers under that namespace. It deduplicates entries, removes resolved identifiers, ignores malformed data and tolerates unavailable browser storage. It stores no prompts, answers or credentials. It is not yet wired into ChatPanel admission or history restoration, so browser-reload recovery remains incomplete. The next step is to persist before dispatch and restore scoped pending entries into the existing read-only recovery view without sending the original action again.

Verification passed 79 backend history/recovery/session/size checks (4.57s) and 31 frontend journal/route/client checks (three files, 0.596s). The first frontend command selected only one existing test file because the new test was outside Vitest's __tests__ include pattern and one route-test path was incorrect; it was moved into the included directory, the paths corrected, and all three files explicitly ran. Ruff, ESLint, formatting and diff checks pass. No production action or deployment occurred.


## Chat reload recovery wired into request admission and restoration

Ordinary executions and approved plans now journal their opaque request ID before dispatch under the authenticated conversation namespace. If initial history is still loading, admission can resolve that namespace through a bounded two-second history read. A failed history read is not cached as permanent lack of support. Old servers without namespace support and browsers without storage keep ordinary chat usable, but cannot provide this reload guarantee.

History restoration appends pending identifiers for that exact scope to the existing audit-recovery view. It never sends or approves the action again. Known terminal delivery and recovered terminal evidence remove the journal entry. Interrupted delivery retains it. An abort before dispatch removes the entry without starting work. Initial plan drafting still relies on the existing pending-plan restoration path; the journal covers execution. Session storage survives reloads in the same tab; cross-device restoration, clearing site data and closing a tab are outside this browser mechanism. Unattended calendar readback remains server-side.

Two focused agent-routing checks initially caught an unnecessary extra history read when history had already resolved without a recovery scope. That redundant lookup was removed. A separate streaming test intermittently finished its fixture stream before asserting the tool indicator because it used a fixed 50ms delay. The test now holds the stream open until the actual tool name is observed, then releases it and verifies completion.

Final verification: the full frontend suite passed 1,633 tests in 148 files (9.13s), followed by five focused journal cases (0.821s) after adding two early-scope lookup assertions. The rebuilt isolated browser suite passed all 30 cases (52.2s). Reload cases cover ordinary requests and approved plans, exactly one original execution, original request identity, journal removal after recovery, no lookup from another authenticated scope, and no recovery lookup after an already delivered result. The delivered-result case inspects session storage at the intercepted execution request to verify journaling happened before dispatch. Browser transport is synthetic; authenticated backend scoping and durable outcome reads were tested separately. Build, ESLint and diff checks pass, and CI includes the journal unit tests. No production deployment or real business action occurred; manual acceptance of this behavior remains pending.


## Explicit pre-execution refusals do not leave phantom recovery entries

The browser previously mounted audit recovery for every HTTP error, including authorization and validation refusals that never reached the engine. With the reload journal this also retained a pending identifier for work that had not started. The send and plan-approval frontend routes now mark only their explicit pre-dispatch validation/authorization refusals with `request_admitted: false`. Their backend-transport failure responses do not carry that marker.

Chat preserves and displays the refusal explanation, clears the corresponding journal entry and does not start recovery when that marker is present on a failed response. Generic 400/502 errors, malformed bodies and non-boolean marker values remain unresolved and eligible for audit recovery; a status code alone is insufficient evidence that execution did not start. This does not classify every possible engine-side rejection.

The first focused/broad runs found one existing exact-response assertion that needed the intentional new field while retaining the original explanation. The assertion was updated; the focused four-file suite then passed all 41 cases (1.34s). Route tests prove the explicit refusals never invoke engine execution and transport failures never get the refusal marker. No production effect or deployment occurred.

Final verification passed all 1,648 frontend tests across 149 files (9.74s) and all 34 rebuilt browser cases (1.0m as reported). Four new browser cases cover 400/403 refusals for ordinary execution and plan approval, visible explanations, cleared storage, one submission and zero audit lookups after reload. Existing interruption/recovery cases remain passing. Build, ESLint and diff checks pass; CI includes the admission-response contract tests. Browser responses are synthetic, and the frontend route tests independently establish that explicit refusal paths do not call the engine. Full acceptance and manual review remain open.


## Bound unattended recovery batches even when database access stalls

The recovery subprocess already had cancellation cleanup, but normal batch execution waited indefinitely for process exit. A blocked database connection or query could therefore prevent all later sweeps. A new real-process case reproduced that stall. Each batch now has a five-minute process deadline, allowing the existing five sequential bounded provider reads while also covering database stalls. Timeout uses the same terminate/reap path as shutdown, with kill escalation after one second. A process-exit race during escalation is tolerated.

Final verification passed 31 recovery/readback/daemon-shutdown/size checks (7.03s). The new case lowers the batch deadline to 50ms around a real isolated sleeping child, verifies it has been reaped before the next 30-second sweep delay, and fails if the loop instead reaches the test's two-second outer timeout. The test initially failed on that outer timeout. Ruff, formatting and diff checks pass. No production process, provider or meeting was touched. Full daemon startup validation and broader runtime acceptance remain incomplete.


## Normal-chat parent/child pause acceptance evidence

The existing accepted unfinished-work/pause harness now also runs with a long-term parent and an unfinished child. It exercises the native chat route and runner with the real goal handlers and private PostgreSQL. After the pause mutation, the scripted provider performs another goal read and can report cascading pause only after observing the parent and child both paused, the completed task still DONE, the remaining task still TODO and no child completion evidence.

The recorded chat answer is: “Paused the goal and its unfinished child goal. The remaining task is still open; neither goal is complete.” The transcript is in `bench/runtime/uat-chat-goal-hierarchy.json`. The original user-accepted single-goal transcript is unchanged. The new artifact remains manually unaccepted; it is scripted wiring/state evidence, not a claim about live language understanding. Live diagnostics explicitly skip the additional hierarchy parameter so this test extension does not silently double provider requests.

Both normal-chat cases passed (4.00s, one dependency warning). The broader goal/chat/size suite passed 77 tests (12.87s, three dependency warnings). Ruff, formatting and diff checks pass. The native dispatcher allowed only goal read/control tools, and the synthetic cases asserted zero external model calls. No production goal was paused or resumed, and no deployment or external business action occurred.


## Explicit child pauses survive parent resume

Private-store tests found two control gaps. If a parent paused a child and an operator then explicitly paused that child, its inherited `paused_by_parent` marker remained. Resuming the parent consequently resumed the independently paused child. Also, a direct child resume was accepted while the parent remained paused, which could reopen its linked task. The first two-case test passed for a child paused before its parent but failed for one explicitly paused afterward; the second test failed because the forbidden child resume did not raise.

Direct pause/cancel/resume transitions now clear inherited pause ownership. Parent-driven propagation still records ownership in the store, so resuming a parent restores only children paused with it. Direct child resume checks the parent's active state under the existing goal transaction/advisory lock and refuses while the parent is inactive. Tests verify the linked task stays TODO and non-runnable until the relevant explicit resume, and evidence remains unchanged.

The normal-chat hierarchy scenario now adds “Keep the child paused separately” followed by “Resume the parent, keeping that child paused.” Each control is constrained to the requested goal/action and followed by a fresh goal read. The result is a queued parent, paused child and unchanged DONE/TODO tasks. Because execution is disabled in this fixture, the scripted response explicitly reports that no background work has started. `bench/runtime/uat-chat-goal-selective-resume.json` records this new manually unaccepted transcript; earlier accepted/synthetic transcripts are preserved.

Final verification passed 80 goal/native-chat/size checks (17.74s, three dependency warnings). The expanded normal-chat cases separately passed two checks (4.12s), using scripted model responses and asserting zero external model requests. Ruff, formatting and diff checks pass. No production goal, task, setting or deployment changed. Live language understanding and full acceptance remain unproven.


## Linked tasks honor inactive goal ancestors

A private-database test reproduced a dispatch mismatch: after three blocker reports moved a parent to blocked, goal admission excluded the queued child, but `pursuit_task_runnable` still allowed its linked task. The SQL guard inspected only directly linked goals. Migration `139_goal_task_family_controls.sql` now follows the same-tenant parent chain and denies task dispatch when any ancestor is paused, blocked, in review, complete or canceled. Unresolved/foreign parent references fail closed. A recursive UNION bounds traversal even for a historical cycle; it does not claim to repair malformed hierarchy data.

The disposable fixture applies the new migration twice to verify re-entrancy. Tests cover the real three-blocker transition and resume, all inactive parent states with a stale queued child, active queued/waiting parents, missing/foreign references and ordinary unlinked tasks while goal execution is disabled. This preserves everyday task behavior outside pursuit goals. The production CRM DAL and thread-pool queries already call the same SQL function; no caller-specific bypass was added.

The new behavior initially failed in the blocked-parent test. Final goal/native-chat/size verification passed 91 tests (16.36s, three dependency warnings), and existing thread-pool/CRM-task caller tests passed 95 tests (1.39s). Ruff, formatting and diff checks pass. Caller tests use mocked databases; guard semantics use private PostgreSQL. Migration 139 has only been applied to disposable test clusters, not the shared or production database. Deployment requires applying that migration; full acceptance remains incomplete.


## Populated-database upgrade check for migration 139

A new disposable PostgreSQL test installs the original task guard extracted from migration 126 after creating a blocked parent, queued child, linked unfinished task and ordinary task. It verifies the original guard allows the child task, snapshots settings/goals/task links/history/attempts/tasks, then installs migration 139 twice. Each installation denies the child task, keeps the ordinary task runnable and preserves every snapshotted row exactly. The new function is restored in a finally block so a failed assertion cannot leave the test cluster using the legacy guard.

The upgrade check passed (one test, 0.94s); Ruff, formatting and diff checks pass. Read-only inspection of accepted revision `eea3252b15` confirms both its CRM DAL and thread-pool call the same `pursuit_task_runnable(task UUID, tenant TEXT)` interface. This is source-level caller compatibility plus a populated SQL upgrade check, not a complete application rollback drill. An application rollback should retain the compatible migration 139 guard and operation records; reinstalling the original guard would reintroduce the dispatch defect demonstrated by the test. No shared or production database was modified, and full rollout acceptance remains incomplete.


## Audit recovery clarification and candidate text history

The user's objection to asking them to check an uncertain outcome is valid: recovery must consult the durable audit record automatically. A browser disconnect can recover the saved outcome; a provider disconnect between an external write and its acknowledgement needs external readback. The latter cannot establish notification delivery merely from attendee presence. Fresh local verification passed 62 chat recovery, calendar readback and operation tests (3.07s), including automatic read-only recovery and no duplicate writes. These use synthetic providers and isolated records, not production actions. The earlier generic fallback is not manually accepted.

The bounded candidate comparison now carries complete prior user/assistant text pairs through both public SDK interfaces, preserving leading host system messages and provider parameters. Exact HTTP payload comparisons passed for initial requests and two prior conversational turns against the captured native fixture; malformed or tool-bearing text history is refused. Eight profile tests passed, and 61 candidate boundary, durable-budget and native-dispatch tests passed (9.26s). No live provider calls were made. This is text-history wire parity only: native memory bootstrap, tool-call history and compatible checkpoint continuation remain unverified or unsupported. No replacement is selected and full acceptance remains incomplete.


## Delegated calendar actions participate in chat recovery

The audit reader previously consulted only the root run's calendar tool steps. New private PostgreSQL tests failed for both a delegated child and grandchild: an executing calendar operation was omitted, leaving the parent's successful prose and verified flag intact. Recovery now traverses the run family within the authenticated tenant and principal, includes each member's receipts, and passes the recorded executor identity to readback. Unrelated runs and descendants beneath foreign tenant/principal boundaries are excluded. Duplicate operation references are collapsed per executor; conflicting references remain unmatched regardless of traversal order, preventing a valid sibling receipt from erasing contradictory evidence.

Final verification passed 129 recovery, calendar, per-user chat, plan-outcome and module-size tests (4.13s), with Ruff and diff checks passing. The readback routing test mocks the provider-facing reconciliation function; existing private-store tests independently exercise its scope/locking and read-only behavior. An earlier combined run also emitted the known intermittent unawaited connect_tcp warning; the final run emitted none, which does not establish that warning's resolution. Test fixture UUID collisions discovered during development were fixed with unique ordered IDs. No live provider, production data or deployment was used. This closes the delegated calendar audit gap, not general external-tool coverage or full acceptance.


## Chat fixture embedding isolation

Investigation of the intermittent unawaited connect_tcp warning found an unintended model-service dependency in the per-user chat fixture. A diagnostic HTTP TCP interceptor blocked and recorded requests from the plan-iteration test to local Ollama through chat_store._embed_turns and get_embeddings_batch_async. Mocked persistence can return truthy synthetic row IDs, which previously launched real background embedding requests. The chat_app fixture now mocks batch embeddings with an empty synthetic result; product memory and embedding code are unchanged.

The same diagnostic test passed with zero HTTP TCP attempts after isolation. All 127 recovery/calendar/per-user/plan-outcome checks passed with RuntimeWarning promoted to error (4.01s). Earlier allocation-tracing runs did not reproduce the warning itself, so this is proof of removing one hidden external dependency, not proof that all sources of the warning are resolved. The recorded diagnostic is bench/runtime/uat-chat-network-isolation.json.


## Broad local regression refresh at da202e576fb

The engine selection (not slow/integration/llm/e2e/smoke) passed 10,995 tests, with 29 skipped and 173 deselected, in 348.10s. The separate goal suite passed 84 tests in 10.28s with two dependency warnings. All 149 frontend files passed, totaling 1,648 tests in 9.09s. Logs are /tmp/runtime-engine-regression-da202e.log, /tmp/runtime-goals-regression-da202e.log and /tmp/runtime-frontend-regression-da202e.log. Earlier engine/frontend records are retained in uat-verification.json.

The engine run emitted 394 warnings, including two unawaited connect_tcp warnings. It loaded the chat fixture before the embedding-isolation correction; the later 127-test warning-as-error run validates that fixture change. No claim is made that every coroutine warning source is resolved. These results refresh local regression evidence for the current implementation, but excluded tests, representative live runtime equivalence/performance, complete daemon/rollback drills and remaining normal-chat acceptance are still open. The accepted baseline and shared deployment were unchanged.


## Trusted deadlines include runtime admission

The current adapter previously armed the host deadline only immediately before native execution. Checkpoint/control reads and acceptance-status delivery preceded that boundary. A stalled status callback consequently outlived a 20ms trusted deadline until the test's separate 300ms timeout fired. The adapter now owns one deadline around admission and execution, and rechecks remaining time immediately before native dispatch. The latter guard prevents a callback that suppresses cancellation from admitting execution after expiry. Existing nested deadline ownership and native finalization remain in place.

Tests cover a stalled checkpoint read, stalled acceptance delivery, cancellation-suppressing delivery, inherited deadlines, cancellation cleanup and native cancellation persistence. The checkpoint read is dispatched to a thread; cancellation stops admission, but cannot forcibly interrupt the already-started read, which the fixture explicitly releases. Runtime/runner/checkpoint/size verification passed 114 checks (6.16s), followed by 60 native-gateway, normal-chat goal and audit-recovery checks (3.95s). Each run had one dependency warning. Ruff, formatting and diff checks passed.

This enforces an explicitly supplied deadline across runtime admission. It does not finish classification or automatic assignment of the 60-second policy to every simple conversational action; that requirement remains open. No real provider, calendar write, shared goal or deployment was involved. The preceding broad regression results predate this adapter change.


## Assign confirmed-action deadlines before setup

Ordinary calendar confirmations previously received a 60-second execution cap only after native setup identified the stored draft. The current runtime now recognizes the existing unambiguous confirmation form and assigns a 60-second trusted deadline before runtime admission/setup begins. This only restricts time: scope, stored arguments, permissions and actual confirmation remain enforced by native tool admission. Earlier trusted deadlines are never extended. Feature-disabled, unrelated, plan-only/deep-plan, resumed, delegated, goal-bound and benchmark requests retain their prior policy.

Runner/deadline/policy verification passed 110 tests (7.58s), then the benchmark/policy/contracts/size suite passed 29 tests (4.48s); each emitted one dependency warning. The native runner benchmark completed 30 measured iterations per path after warmup. All 30 confirmed operations used zero model calls and one synthetic write, with requested attendees, RSVP state and meeting times preserved. The tool-entry assertion verifies a live deadline already exists with no more than 60 seconds remaining. Feature-off comparison runs each used two scripted model calls. Raw measurements and bootstrap p95 intervals are in uat-confirmed-admission.jsonl and uat-confirmed-admission-summary.json. These are synthetic same-checkout paths, not a live provider or runtime-selection comparison.

Ruff, formatting and diff checks pass. General conversational simple-action classification and total ingress-to-admission timing are still open; this closes early deadline assignment only for the supported ordinary confirmation path. No production changes were made, and full acceptance remains incomplete.


## Recoverable pre-execution admission timeouts

The native runtime entrypoint now enables admission-timeout auditing. If a trusted deadline expires before native execution is entered, the host writes a terminal timeout run under the original tenant, principal, agent and request identity. The message states that this request expired before execution began and that earlier attempts retain their own outcomes. No model or business tool is invoked. Once execution has entered, its existing native audit remains authoritative; no extra admission run is created.

Audit persistence uses the existing create/update DAL with a one-second wait cap and preserves the original timeout if storage fails. An already-dispatched database write can finish later. This is best-effort audit availability, not a guarantee during database failure. Ordinary operator cancellation is not relabeled as admission expiry, and native setup failures after execution entry remain outside this new pre-entry record path.

Private PostgreSQL tests verify scoped recovery of expired and stalled admission, native-entrypoint activation, absence of duplicate admission records after execution starts, and preservation of expiry on audit failure. An ASGI chat test verifies that one failed send can be recovered through GET /chat/outcome with the original request ID, without repeating execution. The fixture substitutes persistence DAL functions with inserts/updates into an isolated minimal schema; the existing tracking tests are exercised separately. Final admission/deadline/policy/contracts/size checks passed 49 tests (5.25s). Wider runner/tracking/chat-recovery/goal checks passed 155 tests (8.53s, one dependency warning). Ruff, formatting and diff checks pass. No production deployment or real provider action was used; full acceptance remains incomplete.


## Admission audit tested through the real tracking DAL

Replacing the admission test's substitute create/update functions with the real tracking DAL exposed a defect: record_timeout passed tenant_id to update_run, whose public signature has no such argument. Both expired and stalled-admission tests failed, recovering only the generic timeout text instead of the pre-execution explanation. The unsupported argument has been removed. The update targets the newly generated run UUID returned by this admission path; create_run still records tenant and principal, and recovery still applies both scope filters.

The fixture now redirects only tracking.get_connection to private PostgreSQL and adds the existing run columns needed by the DAL to its minimal schema. Actual create_run and update_run SQL execute unchanged. Tests verify the completed timestamp and durable runtime tenant/principal/request/deadline metadata, plus the ASGI send/outcome path and absence of execution replay. This is real-DAL coverage against a compatible isolated schema, not a full migration-stack or deployment test.

Tracking/deadline/recovery/admission/size checks passed 92 tests (4.91s). After adding durable metadata assertions, all six admission tests passed (1.45s). Ruff, formatting and diff checks pass. The prior substitute-DAL result is retained as historical evidence and superseded by this check for persistence compatibility. No production record was touched; broader acceptance remains incomplete.


## Admission timeout records become visible atomically

A private PostgreSQL concurrency test paused admission persistence immediately after create_run committed, before its follow-up update could run. Recovery saw a terminal timeout but only generic text. Since chat treats terminal recovery as finished, the explanation could remain missing permanently from that delivery even if the later update succeeded.

create_run now includes the existing completed_at and error_message columns in its initial INSERT. Admission timeout persistence uses that single insert, removing the follow-up update. Ordinary pending runs still insert NULL for both terminal fields. The concurrency test now sees the full explanation and completion timestamp at the first visible commit. No migration is required; both columns already exist in migration 011.

Focused tracking/admission/size checks passed 39 tests (1.29s). Wider native controls, runner, chat recovery, deadline/contracts, goal chat and gateway checks passed 173 tests (11.55s, one dependency warning). A further integration selection produced six failures and 55 passes because the configured test database lacks the pre-existing runtime_context column; this is not reported as a passing integration result and that shared schema was not modified. Dedicated private-database checks now cover ordinary owner, federation and absent-principal creation, person linkage fields and NULL terminal fields through the real DAL. Those checks plus recording-failure/sub-agent checks passed 58 tests (3.43s). They do not replace the full timeline integration coverage that failed to run against a compatible schema. Ruff, formatting and diff checks pass. No production deployment changed, and full acceptance remains incomplete.


## Canonical migration path and tracking integration

Preparing a fresh isolated schema found that migrations 127_runtime_contract, 138_goal_provider_reservations and 139_goal_task_family_controls existed on disk but were missing from the canonical manifest. Normal migration discovery therefore omitted runtime context/control tables and the later goal budget/task-family changes. They are now registered, and a migration-contract test requires their presence and goal dependency ordering.

The reproducible command `.venv/bin/python -m bench.runtime.migrated_integration` initializes a disposable PostgreSQL 16 cluster with pgvector, UTF-8 and Unix-socket-only access, applies the full canonical chain, verifies a repeated apply is a no-op, then runs the actual identity and person/timeline integration modules. All 13 tests passed (0.55s), resolving the six prior schema-related failures on a compatible isolated database. The shared test schema was not changed. The first harness attempt used initdb's SQL_ASCII default and failed to encode migration SQL; explicitly selecting UTF-8 corrected that setup issue.

All 52 canonical migration/upgrade routing/document-count checks also pass. Ruff, formatting and diff checks pass. `bench/runtime/uat-migrated-integration.json` records the drill. This proves fresh-schema migration and the selected real-DAL integration behavior; it does not prove populated-database upgrades, full daemon startup or application rollback. No production deployment was initiated and full acceptance remains incomplete.


## Populated canonical schema upgrade

The disposable migration command now creates a second private database using the canonical manifest with the three newly registered migrations omitted, reproducing the pre-registration schema. It seeds synthetic tasks, a blocked parent and queued child, task linkage/history/attempts, an interrupted run and tool audit, a checkpoint, and an executing calendar operation. The prior guard admits the child's task, reproducing the old behavior.

Applying the current canonical manifest applies exactly migrations 127, 138 and 139. Snapshots across ten populated tables remain identical, except for the intentionally added runtime_context column, whose current/v1/checkpoint-v1 default is checked separately. The linked task is now denied by its blocked ancestor; the ordinary task remains runnable. Existing calendar uncertainty and checkpoint contents are preserved. The provider reservation table starts empty. A second apply is a no-op and preserves the same snapshots.

The combined fresh/populated command passed and its 13 real tracking/person-timeline integration tests passed again (0.48s). All 52 migration contracts passed (0.20s), with Ruff, formatting and diff checks passing. `bench/runtime/uat-populated-upgrade.json` records the evidence. This closes the populated canonical schema upgrade check for these migrations; it does not establish full daemon startup, application rollback, active checkpoint resumption across candidate runtimes or production rollout acceptance. Both databases were disposable Unix-socket-only clusters/resources and no provider action or shared database migration occurred.


## Real daemon startup and shutdown with isolated resources

The command `.venv/bin/python -m bench.runtime.migrated_integration --daemon` now boots the actual daemon entrypoint after full canonical schema setup. It uses an empty temporary workspace/fleet, a private Redis Unix socket, a fresh PostgreSQL database and a minimal environment without provider credentials. A drill-only sitecustomize guard refuses Python network connections outside its private Unix sockets and external subprocesses other than the calendar recovery worker. The parent process probes only the newly allocated loopback health port. No daemon lifecycle function is mocked.

The daemon reported all subsystems started, reached HTTP health, and launched the real calendar recovery subprocess. SIGTERM produced exit code 0 in approximately 1.27 seconds; the owned process group was absent after shutdown, before fallback cleanup. One worker spawn was recorded and there was no recovery-sweep startup error. The initial guard had compared tuple process arguments to a list and incorrectly refused the worker; normalization corrected this harness error before the passing rerun. The same command's fresh/populated migration checks and 13 tracking/person-timeline integration tests passed (0.27s for the tests).

Ruff, formatting and diff checks pass. `bench/runtime/uat-daemon-startup.json` records the outcome. This establishes isolated empty-fleet startup/shutdown and recovery-worker launch. It does not prove active-goal or loaded-fleet restart, external-provider reconciliation within the daemon, production ingress behavior or application rollback. No shared daemon, deployment or real business operation was changed; full acceptance remains incomplete.


## Stopped work survives real daemon restart without resume charges

The isolated daemon drill now supports `--daemon --restart`. It seeds a durably canceled run with a current-version checkpoint and a restart-like terminal reason, a paused goal with goal execution enabled, and a blocked uncertain calendar operation. It then starts and cleanly stops the real daemon twice with in-flight resumption enabled, verifying the records after each cycle. An initial fixture used checkpoint schema version 0 and merely exercised incompatibility rejection; it was corrected to current version 1 before assessing resume behavior.

The corrected drill failed because startup charged a resume attempt for the stopped run before the later runtime guard rejected execution. The daemon scan now checks durable runtime controls before checkpoint loading, batch selection, attempt charging or resume-task scheduling. Checkpoint loads for remaining candidates use the recorded tenant. A unit control confirms an unstopped candidate remains selectable, rather than making all startup resumes inert.

Both final restart cycles passed: no resume attempt charged, no new steps on the stopped run, checkpoint and cancel preserved, paused goal unchanged, and blocked uncertain operation unchanged. Health and worker launch passed on both boots; shutdowns were approximately 1.32s and 1.17s with no surviving owned process group. All 31 resume/control/size checks passed (4.86s), and the full drill's 13 tracking integrations passed (0.28s). Ruff, formatting and diff checks pass. `bench/runtime/uat-stopped-restart.json` records the result. This is stopped-work restart verification, not active-goal continuation, loaded-fleet performance or full application rollback certification. No real stopped goal was resumed and no production deployment changed.


## Startup resume selection retains tenant ownership

Extending the real-daemon restart fixture with another tenant's eligible cancelled run reproduced an isolation gap: the startup scan selected every tenant and charged the foreign run before later checkpoint/runner checks rejected it. Resume selection now requires the configured runner tenant, candidates retain the recorded tenant, attempt updates include that tenant and must affect a row, and resumed execution receives the tenant explicitly. Missing runner tenant configuration fails closed. Durable-stop checks remain in place.

The corrected private-database drill passes two real startup/shutdown cycles with resume enabled, leaving both the stopped local run and foreign tenant run at zero charged attempts. Existing paused goal, checkpoint, cancel and blocked uncertain-operation checks still pass. The database role is a private superuser, so SQL tenant selection is tested independently of RLS masking the defect. Unit controls verify a same-tenant eligible candidate is handed to execution with its tenant, and a charge affecting zero rows cannot admit work.

All 33 resume/control/size checks passed (5.21s), and the drill's 13 tracking/person-timeline integration tests passed (0.48s). Shutdowns were approximately 1.17s and 0.87s, with health, worker launch and owned-process cleanup checks passing. Ruff, formatting and diff checks pass. `bench/runtime/uat-resume-tenant-isolation.json` records the evidence. This covers startup resume selection and charging, not every startup maintenance/reaper path, loaded-fleet continuation or full rollback acceptance. No real tenant work was resumed and no production deployment changed.


## Stale cleanup retains tenant ownership

The restart drill now seeds stale running agent and workflow records for both the daemon tenant and a foreign tenant. The initial run failed because cleanup timed out foreign work. Agent selection and updates, and workflow updates, now include the configured tenant; both startup and the periodic watchdog pass that tenant explicitly.

Two real daemon restart cycles now clean local stale records while preserving foreign running records. Earlier stopped-run, foreign-resume, paused-goal, checkpoint and uncertain-operation assertions still pass. All 53 focused reaper/workflow/resume/size checks passed (3.64s); the canonical migration drill and 13 tracking integrations passed (0.30s). Ruff and diff checks pass. Evidence is in `bench/runtime/uat-cleanup-tenant-isolation.json`. This verifies these cleanup paths, not all startup maintenance or active-goal continuation. Full acceptance and production rollout remain incomplete.


## Cleanup preserves locks for skipped work

Two regression tests reproduced cleanup releasing an agent lock despite skipping its healthy run, and releasing/counting a run whose conditional update affected zero rows. Cleanup now collects only successfully updated rows and releases their locks after commit; counts and logs reflect those rows. All 56 focused reaper/workflow/resume/size tests pass (4.93s), and the isolated canonical migration/two-restart drill passes with 13 integration tests (0.28s). The initial two failing tests are recorded in `bench/runtime/uat-reaper-lock-preservation.json`. Ruff and diff checks pass. This does not establish replacement-run lock ownership or full HA concurrency correctness; full acceptance remains incomplete.


## Broad regression verification after admission and restart fixes

At `5f5b6e62179`, `.venv/bin/python -m pytest -q robothor/engine/tests -m "not slow and not integration and not llm and not e2e and not smoke"` passed 11,031 tests, skipped 29 and deselected 173 in 356.20s. The run emitted 393 warnings, including one unawaited `connect_tcp.<locals>.try_connect` coroutine attributed at collection to `test_tool_admission.py::TestGuardrailGate::test_a_block_writes_an_audit_row`; the allocation origin is not established. The prior broad run also had this warning class. It remains unresolved, not silently excluded. Log: `/tmp/runtime-engine-regression-5f5b6e62179.log`.

The complete goal suite passed 84 tests in 9.94s with two dependency deprecation warnings (`/tmp/runtime-goals-regression-5f5b6e62179.log`). This updates current-code evidence after the focused fixes; no failures were observed in these scopes. Excluded integration/live/slow scenarios, matched candidate qualification, active-goal continuation, rollback and remaining manual acceptance are not established by this run. Earlier broad results remain preserved in `uat-verification.json`. No deployment occurred.


## Goal recovery retains coordinator ownership

The real-daemon restart drill reproduced a paused goal run being charged and launched through generic checkpoint recovery without its trusted goal binding. Startup scan now excludes goal-owned runs identified by trigger, runtime metadata or attempt linkage. GoalController retains recovery ownership. The final drill independently checks all three attribution forms across two real daemon restarts, alongside earlier stop/tenant/cleanup assertions; the canonical migration and tracking integration checks pass.

A positive private-store controller test expires an active goal lease, reconstructs a controller and verifies a fresh attempt with 70 tokens remaining after 30 recorded tokens. Writes are rejected until reconciliation is recorded, then admitted. Five new tokens are charged once; a stale finish cannot double-charge; a second waiting tick does no execution. All 111 goal/resume/size tests pass (14.70s, two dependency warnings). Ruff and diff checks pass. Evidence: `bench/runtime/uat-goal-recovery-ownership.json`. The positive path uses a scripted executor, not full native active-goal checkpoint continuation. Legacy descendants lacking recorded goal attribution and application rollback remain unverified; full acceptance is still incomplete.


## Legacy delegated goal recovery

Extending the actual daemon drill with legacy child/grandchild runs lacking runtime goal metadata reproduced generic recovery charging these descendants. The scan now computes the tenant-scoped goal family recursively, with UNION over IDs to terminate cycles, before selecting ordinary interrupted work. Two restart cycles preserve all three goal attribution roots and their six descendants.

A canonical-database integration test verifies ordinary work remains selectable, cross-tenant parent links do not inherit goal ownership, and a goal-family cycle terminates while excluding descendants. All 26 resume/size tests pass (5.28s); the full private migration/restart command passes 14 integration tests (3.65s). Ruff and diff checks pass. Evidence: `bench/runtime/uat-goal-descendant-recovery.json`. This closes the recorded legacy-parent-link selection gap, not full native active-goal continuation, general orphan attribution or rollback. Nothing was deployed.


## Native goal execution after lease recovery

A new canonical-database test reconstructs goal execution after an expired lease, using the real AgentRunner, runtime adapter, registry/dispatch and goal controller. Structured scripted model responses first request a wait before reconciliation (denied), record reconciliation, then wait successfully. All three ordered outcomes are asserted in durable tool-step rows, including the exact reconciliation-required refusal; the completed run retains the new goal/attempt identity. A later waiting tick makes no model call. Thirty prior tokens plus 450 new tokens produce 480, and an old attempt finishing late cannot double-charge.

The full canonical fresh/populated migration command passes 15 integration tests in 4.29s with one Pydantic warning. Ruff and diff checks pass. Initial fixture mistakes are preserved in `bench/runtime/uat-native-goal-recovery.json`: frozen config assignment, MagicMock usage, an automatic planning response mistaken for a tool attempt, and ambiguous diagnostic SQL. Pinning simple routing and checking every persisted tool outcome prevents the earlier weak state-only assertion from passing incorrectly.

This is native execution after lease expiry with scripted responses and seeded test permission rules. The reconciliation marker is synthetic; no external provider readback is claimed. A real process-crash active-goal drill, full checkpoint continuation, candidate comparison and rollback remain unfinished. No product code changed in this verification increment, and nothing was deployed.


## Native goal worker process crash

The native recovery integration test now has a subprocess case. A real runner records a synthetic progress update through normal goal dispatch, verifies that its tool audit and goal checkpoint text are durable, then kills its own test process with SIGKILL during the next scripted provider request. The parent requires both the marker and the actual signal exit status. It accelerates lease expiry in the private database and recovers through a new controller/native execution.

The interrupted request retains its conservative budget charge. Recovery requires reconciliation before updates, then reaches waiting; the old attempt remains interrupted with its charged tokens. The initial progress effect/audit is present exactly once after recovery, stale finish cannot charge twice, all three recovery tool outcomes are saved, and another waiting tick makes no calls. Full canonical migration/integration command: 16 passed, one intentional standalone-worker skip, one Pydantic warning, 7.39s. Ruff and diff checks pass. Evidence: `bench/runtime/uat-native-goal-crash.json`.

This proves native worker process-crash recovery through durable goal state with scripted responses. It does not prove killing/restarting the complete daemon, saved-message checkpoint continuation, actual external provider readback or application rollback. Production state and configured model selection remain unchanged.


## Compatible native admission preserves existing work

The rollback policy routes new admissions to a compatible current runtime while preserving prior controls, usage and operations. The accepted baseline predates durable-stop enforcement, so a binary downgrade onto stopped work is not the approved fallback. No such downgrade was performed.

A new canonical-database test executes an ordinary request through the actual native runner/runtime and records its completed current-runtime identity. Exactly one scripted provider call and zero business tool calls occur. Full row snapshots of an older canceled run, its durable stop/checkpoint, a paused goal with usage, and an executing uncertain calendar operation remain identical. This strengthens the earlier control-only insertion test with actual execution, but is not a runtime routing switch or full application rollback drill.

The canonical migration/integration command passes 17 tests, with one intentional worker-only skip and one Pydantic warning, in 5.90s. The initial fixture asserted runtime instead of runtime_id and was corrected. Ruff/diff checks pass. Evidence: `bench/runtime/uat-native-admission-preservation.json`. Full rollback, candidate qualification and remaining acceptance work remain open. Nothing was deployed.


## Full daemon goal crash and restart

The private canonical migration drill now accepts `--goal-crash`. A temporary main manifest and drill-only scripted provider drive the actual daemon scheduler, goal controller, native runner and normal dispatch. The first boot saves progress, then waits in its second provider request. The harness requires health and the readiness marker, kills the owned daemon process group with SIGKILL, and verifies that no live member survives (host-reaped zombies are distinguished from executable processes). It accelerates the goal lease expiry, then boots a fresh real daemon with generic resume enabled.

Recovery proceeds through the goal controller: no generic resume attempts are charged, the tool audit is exactly progress / denied wait / reconciled / wait, and the saved progress effect occurs once. The interrupted attempt retains 42,562 reserved tokens; the finished recovery attempt uses 450, totaling 43,012. The goal is waiting with reconciliation cleared. Recovery-worker launch and health succeed on both boots; recovered-daemon graceful shutdown takes about 1.17s.

The combined command also passes the prior two stopped-work restart cycles and all 17 canonical database integrations (one intentional standalone crash-worker skip, one Pydantic warning, 7.39s for integrations). Ruff and diff checks pass. The initial drill assertion expected completed instead of the store's finished attempt status; that fixture correction is recorded in `bench/runtime/uat-full-daemon-goal-crash.json`.

This closes the synthetic full-daemon active-goal crash/restart check. Model replies are scripted, external network connections are refused, and the lease clock is accelerated. Saved-message checkpoint continuation, live external provider readback, runtime comparison, application rollback and remaining user acceptance are separate requirements still open. No deployment occurred.


## Checkpoint setup tenant isolation

A canonical-database test found that runtime setup called load_latest without the trusted tenant and could apply another tenant's saved plan-only mode. The setup read now passes tenant_id. The same-tenant positive control still restores plan-only execution; the foreign checkpoint leaves the supplied mode and identity unchanged. The first foreign-tenant test failure is preserved in `bench/runtime/uat-checkpoint-setup-isolation.json`.

All 41 focused resume/runtime/size tests pass (3.94s). The canonical migration/integration command passes the expanded checks; its exact summary is recorded in the artifact. Ruff and diff checks pass. This is the setup read boundary, not full native message-checkpoint continuation or a claim that every identity revalidation path is certified. Nothing was deployed.


## Successful native saved-message continuation

The new canonical-database checkpoint test confirms that the actual native runner receives the saved task context and assistant/tool-result pair, answers with one scripted model call and zero repeated tools, and preserves the source checkpoint. It initially failed because the in-memory task had been restored but the audit row still contained Resume from checkpoint. Tracking now accepts task_text updates, and the native runner persists the restored, already-redacted task before continuing. A failed update stops execution before any provider or business call.

The completed run now retains the restored objective and tenant/runtime identity, and the test checks the audit objective from inside the provider boundary. All 38 focused checkpoint/tracking/size tests pass (2.97s); 104 runner/resume tests pass (5.84s, one Pydantic warning); the canonical migration/integration command passes 20 tests (7.68s, one worker-only skip and one Pydantic warning). Ruff and diff checks pass. Evidence: `bench/runtime/uat-native-checkpoint-continuation.json`.

This establishes native saved-message continuation for the synthetic case. Candidate checkpoint compatibility, live-provider behavior, full application rollback and remaining acceptance gates stay open. Nothing was deployed.


## Durable checkpoint lineage

The native continuation test exposed missing audit linkage: the resumed run did not persist which previous run supplied its checkpoint. AgentRun now carries resume_from_run_id from the explicit native resume argument, and runtime_context records it separately from the delegated parent_id. The canonical native test asserts the actual original run ID in the completed continuation row. Existing historical rows are not rewritten.

All 122 focused runtime/tracking/runner/size tests pass (4.85s, one warning), and 20 canonical integrations pass (6.30s, one worker-only skip and one warning). Ruff and diff checks pass. Evidence: `bench/runtime/uat-resume-origin.json`. This establishes durable lineage for new resumes; chat recovery traversal of that lineage remains unfinished. Nothing was deployed.


## Chat recovers the continued run

Three failing private-store tests reproduced returning a canceled original despite a completed continuation, and failing to mark forked/cyclic continuation records ambiguous. Chat recovery now follows the explicit resume origin through one unambiguous chain, scoped to the same tenant and principal. It returns the latest run while retaining calendar receipts across the original, continued and delegated work. A completed continuation cannot hide an executing older operation: verification remains false and reconciliation remains pending. Foreign tenant/user links do not affect the result.

The canonical native checkpoint test now reads the actual resumed outcome using the original request key, proving the audit writer and recovery reader work together. All 45 focused tests pass; the broader chat/calendar/goal/size selection passes 110 tests (5.55s, one warning), and 20 canonical integrations pass (7.67s, one intentional worker-only skip and one warning). Ruff and diff checks pass. Evidence: `bench/runtime/uat-chat-continuation.json`. Historical missing links are not invented and multiple initial admissions remain ambiguous. No business action was replayed or production deployment changed.


## Stop follows native continuations

Three failing private-store tests found that durable run/request stops did not reach checkpoint continuations. Stop ancestry now follows both delegated parent links and explicit resume origins, constrained to the tenant at every step. UNION terminates cycles; a validated UUID cast keeps the run ID lookup directly usable and safely ignores malformed references. Tests include mixed continuation/delegation chains, unrelated and foreign work, cycles, malformed origins, and actual runtime tool-admission denial after either kind of original stop.

All 65 focused control/runtime/chat/size tests pass (3.53s), and 20 canonical integrations pass (6.32s, one intentional worker-only skip and one warning). The combined real-daemon restart/crash drill also passed before the final equivalent UUID lookup refinement. Ruff and diff checks pass. Evidence: `bench/runtime/uat-continuation-stop.json`. This increment does not certify a stop-latency cohort or prevent an already dispatched external request from finishing. Nothing was deployed.


## Do not repeat superseded checkpoints

The successful native checkpoint integration exposed the original canceled run remaining eligible for another startup resume. Startup scan now excludes records with a same-tenant recorded continuation, and charging repeats that check so a stale selection cannot restart an already superseded origin. The latest canceled/running continuation remains selectable with its own checkpoint; completed latest work does not. A denied old charge leaves its attempt count unchanged.

All 26 focused resume/size tests pass (3.79s). The combined canonical migration, real-daemon restart and full goal-crash command passes, including 23 integrations (7.76s, one worker-only skip and one warning). Ruff and diff checks pass. Evidence: `bench/runtime/uat-resume-supersession.json`. Concurrent first admissions before either continuation row exists remain a separate atomic-claim concern; this test does not certify that race. Nothing was deployed.


## Exclusive startup checkpoint admission

An overlapping-startup integration test reproduced two launches and two charges for the same checkpoint before either worker wrote a continuation row. Startup now first acquires a nonblocking tenant/source PostgreSQL session lock using a dedicated connection. The connection remains owned through execution; failure to charge or schedule releases it. Worker finally and a task completion callback cover normal exit and cancellation, including cancellation before coroutine entry. The source-continuation recheck remains in place.

The private database test proves one launch/charge, then successful reacquisition after either completion or cancellation. A focused early-cancel check proves no runner call and released ownership. All 27 focused resume/size checks pass (4.41s), and the combined real-daemon restart/goal-crash command passes with 25 integrations (6.22s, one worker-only skip, one warning). Ruff and diff checks pass. Evidence: `bench/runtime/uat-resume-claim.json`.

Each active startup resume holds a dedicated DB connection. Session loss while a worker survives and cross-process failure injection for this claim remain unverified; this is not general HA fencing certification. Nothing was deployed.


## Refuse further admission after resume claim loss

A private PostgreSQL failure-injection test terminated the advisory-lock session while its startup worker remained alive. Provider admission initially still succeeded, reproducing the gap. The worker now carries its dedicated claim in task context, and resumed provider/tool admission verifies that connection before proceeding. Connection failure refuses admission with an explicit reconcile-before-continuing error. Worker exit resets the context and closes ownership; a subsequent unrelated provider admission remains usable.

All 37 focused resume/control/size checks pass (3.86s), 105 tool/admission/request-budget checks pass (2.71s), and 26 canonical integrations pass (7.92s, one intentional worker-only skip and one warning). Ruff and diff checks pass. Evidence: `bench/runtime/uat-resume-claim-loss.json`. This covers detected PostgreSQL session loss before admission. In-flight effects, network-blackhole timing, database failover and general production fencing are not certified. Nothing was deployed.


## Broad regression audit after continuation and claim changes

At `7c5f5c64b21`, the broad engine selection passed 11,040 tests and failed one: runner.execute grew to 990 lines against its pinned 986-line cap. It skipped 29, deselected 178, emitted 393 warnings and took 357.83s. One unawaited connect_tcp coroutine warning remains. The complete goal suite passed 85 tests in 10.83s with two dependency warnings. Logs are `/tmp/runtime-engine-regression-7c5f5c64b21.log` and `/tmp/runtime-goals-regression-7c5f5c64b21.log`.

The checkpoint restore/audit block now lives in an async lifecycle helper, retaining the same persistence seam and failure behavior. The cap was not increased. The first extraction missed a local asyncio import; that correction preceded the passing reruns. All 92 function/module-size, runner and recovery checks pass (10.53s), and 26 canonical integrations pass (4.98s, one worker-only skip and one warning). Ruff and diff checks pass. The original broad failure is preserved, and no post-extraction broad pass is claimed. The acceptance summary now distinguishes completed synthetic daemon/checkpoint recovery from remaining live/provider, candidate and rollback work. Nothing was deployed.


## Post-extraction broad engine pass

At `aa0312e8cf3`, the full non-slow/non-integration/non-live engine selection passes 11,041 tests with zero failures, 29 skips and 178 deselections in 355.39s. This includes the unchanged function-size cap and the recent checkpoint, chat continuation, Stop and claim-loss changes. The prior failure remains preserved in uat-verification.json.

The diagnostic command sets `sys.set_coroutine_origin_tracking_depth(8)` before pytest. Of 393 warnings, one remains an unawaited connect_tcp try_connect coroutine. Its creation stack traverses HTTPCore connection handling and AnyIO connect_tcp, task-group start_soon and call_for_coroutine. The trace does not reach the initiating application caller, so root cause and remediation are still unresolved; the collecting test is not blamed. This run is correctness evidence, not a comparative performance cohort. Log: `/tmp/runtime-engine-regression-aa0312e8cf3.log`.

Current broad engine checks now pass; the latest full goal run remains 85 passed and the frontend remains 1,648 passed at its recorded revision. Live-provider operating limits, candidate qualification, application rollback and remaining manual acceptance stay open. No deployment occurred.


## Audit recovery after delivered execution errors

Following the user's expectation that Robothor consult its audit trail automatically, three new chat component cases reproduced a gap: failed, timed-out and cancelled runs with nonempty terminal error text bypassed outcome recovery. The shared terminal handler now enters the existing original-request audit recovery path for those statuses regardless of error text. Durable stop wording now describes checking recorded results instead of handing that check to the user.

The tests require the original request identity, exactly one action submission, and recovered failure text alongside calendar evidence. Completed responses retain their direct delivery. The initial run had 3 failures and 8 passes; after the fix, 18 focused streaming/control/recovery tests passed (1.68s). The full frontend suite passes 1,651 tests across 149 files (9.41s), with the existing jsdom navigation notice. Changed-file ESLint, TypeScript and diff checks pass. Evidence: `bench/runtime/uat-terminal-error-recovery.json`.

These are synthetic component responses; the backend chat/calendar recovery tests separately passed 64 checks (3.25s). General external-service reconciliation, plan drafting interruption recovery and remaining runtime acceptance stay open. No production deployment or manual acceptance is claimed.


## Failed exploration cannot publish an approvable plan

Inspection of draft interruption recovery reproduced a false-readiness issue: failed, timed-out and cancelled exploration runs could return partial text containing PLAN_READY and publish it as a pending plan. Three new backend cases failed before the fix. Only a completed exploration may now produce a plan; terminal response and chat history use the failure-aware result text, and the stream carries the run status.

The 93 backend chat/session/plan/size checks pass (3.92s), as do all 11 plan-interface component tests (1.05s). The new cases assert no plan event, no active plan, no plan persistence, failure text in response and history, and exactly one exploration execution. Ruff lint/format and diff checks pass. Evidence: `bench/runtime/uat-plan-exploration-outcomes.json`. These use synthetic runner outcomes. Automatic completed-draft recovery after a lost connection, other external providers, and the remaining runtime acceptance gates remain open. Nothing was deployed.


## Recover a saved plan without repeating preparation

Planning now journals its request and uses original-request outcome recovery after transport errors, HTTP failures and premature EOF. The endpoint attaches only the matching pending, unexpired plan. It keeps delivery pending while that request saves its draft and can restore a scoped saved plan after session-cache loss. Plan publication now requires strict persistence, and recovery strips internal markers. Recovery does not approve or execute the draft.

The initial matching-plan check failed (1 failed, 20 passed). The expanded backend regression passes 142 tests (5.94s); final focused checks including rejected/expired/foreign-run/failed-run refusal pass 78 tests (6.28s). The full frontend suite passes 1,654 tests in 149 files (9.40s), with the existing jsdom navigation notice. Three new component cases prove one preparation, matching original request and zero approvals. Canonical migration/native integrations pass 26 tests (6.33s, one worker-only skip and one dependency warning). Ruff, ESLint, TypeScript and diff checks pass. Evidence: `bench/runtime/uat-saved-plan-recovery.json`.

Backend HTTP tests mock runner/persistence and component tests mock outcome transport; this is not yet one browser-to-engine disconnection drill. Simultaneous plan revision and a crash between completed exploration and saved draft remain unqualified. No deployment or additional manual acceptance is claimed.


## Browser-to-native-engine saved-plan recovery

The new optional `--chat-browser` canonical integration drill uses a freshly built standalone app, real Chromium, actual Next chat proxies, loopback FastAPI chat, native AgentRunner and private canonical PostgreSQL. It forwards the plan request and consumes the saved-plan response, then drops that response before the browser receives it. Unrelated dashboard APIs and provider transport are synthetic; the native planning and alignment logic remain active.

The browser restores the approval card after one outcome read. Database checks require one completed native run and one matching pending plan. There is exactly one planning submission, no approval and no business-tool execution. Provider calls are the existing two draft passes and one alignment check, with no recovery generation. The initial attempts exposed a test timeout and a scripted provider missing the alignment response; both fixture issues and failed logs are preserved.

The complete canonical command passes 27 integrations (12.36s), with one intentional worker-only skip and one dependency warning. Ruff, Node syntax, formatting and diff checks pass. Evidence: `bench/runtime/uat-native-plan-browser.json`. This establishes the combined synthetic browser-to-engine disconnect case previously missing; it does not establish a live latency cohort, production authentication qualification, or manual acceptance. Nothing was deployed.


## Broad verification after saved-plan recovery

At revision `9f83e49d0e6`, the broad engine selection passes 11,054 tests, with 29 skipped, 179 deselected and 393 warnings (356.15s). The separate goal suite passes all 85 tests, with two dependency warnings (10.79s). No test failures were introduced in these selections. The previously observed unawaited connect_tcp warning remains; its collecting test does not establish the application path that initiated it. These suite durations are not runtime/provider performance measurements. Logs and the earlier engine result are preserved in `bench/runtime/uat-verification.json`.

The monthly/PR browser compatibility job now invokes the canonical browser/native saved-plan recovery command after its existing app build and Chromium setup. The job uses Ubuntu 24.04 and installs its [PostgreSQL 16 pgvector package](https://packages.ubuntu.com/noble/postgresql-16-pgvector), which the canonical schema needs. YAML parsing and prerequisite order checks pass, and the matching combined command already passed locally. Hosted CI has not been run; this change configures ongoing coverage without claiming a hosted result.

The acceptance matrix now includes delivered execution-error recovery and saved-plan readiness explicitly. Full acceptance remains false: matched candidate qualification, broader external-service reconciliation, application rollback and remaining normal-chat acceptance are not established by these passing checks. No deployment occurred.


## Consume a pending plan approval once

A failing endpoint test reproduced two executions for overlapping approvals of the same pending plan. Approval now atomically claims the saved plan before admitting native or deep execution. The database update matches tenant/session and the exact execution-bearing plan fields, records the winning approval request ID and refuses subsequent stale claims. Validation/admission were extracted into `chat_plan_claim.py` without increasing module-size caps. Unit apps that only seed in-memory plans now explicitly mock persistence admission; separate private-database and native-browser tests exercise the real claim.

The real browser test then exposed the Next proxy converting a durable 409 refusal into a generic 502. The client/proxy now preserve that response and its explicit admission refusal. After recovery of a saved draft, two distinct approval requests yield statuses 409 and 200, with one native execution run. The original recovery-only case still leaves its plan pending with zero approvals.

Private claims pass 8 checks (0.86s); the final plan/deep/session/store/size selection passes 150 tests (12.74s). All 1,655 frontend tests pass (9.63s), and 35 focused proxy/client tests pass after the final wording change. A fresh standalone build and the canonical browser/native command pass 28 integrations (13.12s, one worker-only skip and one dependency warning). Ruff, ESLint, TypeScript, Node syntax and diff checks pass. Evidence and intermediate failures: `bench/runtime/uat-plan-approval-claim.json`.

These duplicate browser requests have distinct request IDs. Reattaching a retry that reuses the winning request ID, concurrent revisions, and interruption after claim but before run admission remain open. The earlier broad engine result predates this change; no full-acceptance or deployment claim is made.


## Retried approval retains the original admission

The combined browser test reproduced reporting an overlapping retry of the same approval request ID as not admitted. Approval now checks the authenticated request's durable run record or saved claim before validating the current plan, and repeats that check after a racing claim loses. An existing admission returns 409 with request_admitted=true, so the existing frontend recovery path follows its original result. This works after the pending plan has been cleared as well as before the first run row exists. A distinct request remains an explicit refusal.

Private SQL checks cover tenant/principal/session isolation, claim-before-run and run-after-plan-clear stages. All 152 focused plan/deep/session/store/size checks pass (12.98s); 35 frontend admission/recovery/proxy checks pass (1.00s). The canonical browser/native command passes 29 integrations (16.07s, one worker-only skip and one dependency warning). In the same-request case, two concurrent approvals produce 200/409, a third post-completion retry retains admitted=true, and outcome lookup returns the completed execution. Exactly one execution run exists. Ruff, Node syntax, formatting, changed-test ESLint and diff checks pass. Evidence and failed attempts: `bench/runtime/uat-plan-original-request-recovery.json`.

The earlier same-request reattachment gap is now covered. Process death between claim and run admission, concurrent plan revisions, matched runtime comparison and remaining acceptance gates remain open. No production action, deployment or manual acceptance is claimed.


## Older completion preserves newer plans

Two failing HTTP cases reproduced normal and deep execution clearing a newer pending plan from the session cache. Their cleanup also used an unconditional session-wide database clear. Completion now carries the durable approval request identity through PlanState serialization/restoration and retires only that matching approved plan. Both cache and SQL checks preserve a newer plan, a pending revision, or a newer approval of the same plan ID. Database retirement is awaited; failure retains the durable consumed claim for duplicate protection and logs only the exception type.

The private SQL/cache suite passes 16 checks (1.12s). The plan/deep/session/store/size selection passes 161 tests (10.96s); 42 persistence/Telegram/deep compatibility checks pass (3.97s). Canonical browser/native integrations pass 29 tests (14.37s), with one worker-only skip and one dependency warning. Helper extraction brought run_approved below the oversized-function threshold, so its old exception was removed; no cap was increased. Ruff, formatting and diff checks pass. Evidence: `bench/runtime/uat-plan-late-completion.json`.

The newer-plan race is tested through mocked-runner HTTP and private SQL layers separately; the browser drill verifies ordinary retirement and original-request recovery. Revision/rejection write races and claim-before-run process death remain open. No production action, deployment, or full acceptance is claimed.


## Pending draft revisions and rejections

Three failing HTTP tests reproduced revision/rejection of an already approved plan and publication of partial output from a failed revision. Pending changes now use an awaited conditional database update matching tenant, session and the exact draft. A late revision cannot restore a rejected draft or overwrite an approval/new revision. Successful revisions receive a fresh approval ID and updated text hash; failed revisions leave the original text and revision history unchanged. Recovery recognizes revision runs as plan exploration.

The focused HTTP, private PostgreSQL, plan/deep/session and size-ratchet selection passes 177 tests (8.30s). Private tests include concurrent revision winners, stale approval refusal, scope isolation and publication only after a durable successful change. Canonical browser/native integrations pass 29 tests (18.72s), with one worker-only skip and one dependency warning. Those browser cases cover saved-plan recovery and duplicate approvals; revision/rejection races are tested at the HTTP/helper/SQL layers. Evidence: `bench/runtime/uat-plan-pending-changes.json`.

The user rejected asking them to investigate uncertain outcomes: Robothor should consult its audit records and automatically reconcile external results. A sent request is not itself proof of provider completion. This is an acceptance requirement, not acceptance of all recovery paths. Claim-before-run process death, new plan-start ordering, broader reconciliation and remaining runtime acceptance gates stay open. No deployment or full acceptance is claimed.


## Saved-plan recovery after an early status request

A failing HTTP case reproduced a reconnect ordering bug: requesting plan status creates an empty cached session, and recovery previously consulted the saved plan only when the session was absent. Recovery now accepts an idle empty session and rechecks for competing work after reading persistence. Tests preserve newer plans/tasks started during that read.

The final focused selection passes 195 tests (14.25s). The canonical browser/native suite adds a status-first reconnect case and passes 30 tests (20.10s), with one worker-only skip and one dependency warning. The added case clears only process-local sessions after a real draft has been persisted, calls the actual status proxy, and recovers through the browser, Next proxy, native engine and private canonical database. It produces one preparation run, no approval/execution and no business-tool calls. Two test-fixture mistakes and their logs are preserved in `bench/runtime/uat-plan-status-recovery.json`; both were corrected. Ruff, formatting, Node syntax and diff checks pass.

This is evidence for reconnect ordering, not general restart or all-provider reconciliation. Full acceptance remains open and no deployment occurred.


## Broad engine regression after plan recovery and admission changes

The broad non-slow/non-integration/non-LLM/non-e2e/non-smoke engine selection completed with 11,092 passed, two failed, 29 skipped, 181 deselected and 393 warnings in 362.45s. Its two failures were new race-test fixtures collected before the required original_message field was added; both failed before reaching the behavior being tested. Those exact cases pass against committed revision `c9a2ccc14ac` (2 passed, 0.70s), as does the corrected 195-test focused selection. Product logic did not change during the broad run. The integration-only browser case was added during that run and separately passes in the canonical suite.

Every selected case has passing evidence across the broad run and corrected-case rerun. This is not described as one clean broad invocation. The existing unawaited connect_tcp warning remains, and its collection location does not identify the initiating path. Original failures and rerun logs are preserved under `engine_plan_recovery_regression` in `bench/runtime/uat-verification.json`. Full acceptance and deployment remain unclaimed.


## Expose recorded approval before execution exists

A private-database test reproduced outcome lookup returning not_found even though the scoped approval claim was already committed. With no matching run, lookup now checks the saved approved claim and returns accepted, nonterminal and unverified, sourced from the approval record. The browser says the approval is recorded and checks whether execution has started. Once the original run appears its result takes precedence; lookup never starts execution.

The combined private database/HTTP/recovery/admission/size selection passes 151 checks (8.01s). The 27 focused frontend checks pass (1.22s), and the canonical migrated native suite passes 26 integrations (7.49s), with one worker-only skip and one dependency warning. SQL checks enforce tenant/principal/session/request scope. The HTTP test loses all session cache, observes the approved claim, then reads the original completed run with zero runner calls. The frontend branch is unit-tested, not separately browser-tested this turn. Ruff, formatting, ESLint and diff checks pass. Evidence: `bench/runtime/uat-approval-audit.json`.

This closes audit visibility in the claim-before-run gap, not recovery of execution after process death there. Recorded approval is not evidence of execution or completion. Claim lifetime and safe crash recovery still require work, alongside the remaining acceptance gates. No deployment or full acceptance is claimed.


## Durable stop remains visible before a run is available

Four failing private-database cases showed that recovery returned accepted/not_found even after a durable request stop was committed. Pending outcome lookup now prioritizes that scoped stop and returns stopping with stop_requested=true, nonterminal and unverified. Chat keeps the stop visible while reading the original request for late results. Missing run evidence is not proof that no effects occurred; a later cancelled or completed execution record retains its actual recorded outcome.

The combined recovery, admission, durable controls and size selection passes 165 tests (11.93s). Focused frontend checks pass 28 tests (1.98s). Canonical migrated native integrations pass 26 tests (7.66s), with one worker-only skip and one dependency warning. Tests cover stops with/without approval, tenant/principal/session/request scope and late execution outcomes; frontend recovery uses GETs only. Ruff, formatting, ESLint and diff checks pass. Evidence: `bench/runtime/uat-stop-before-run-audit.json`.

This closes visibility of a pre-run stop, not safe recovery of an orphaned admission or a general deadline for unresolved recovery. The frontend branch is unit-tested, not separately browser-tested this turn. No production deployment or full acceptance is claimed.


## Candidate screening preserves negative evidence

Review of the offline screening harness found unconditional completed status for any returned adapter result and discarded warm-up failures. The harness now requires adapter verification and independent fixture verification, exactly one write and one dispatch. It retains failed/timeout samples with observed effect counts and wall duration; unknown usage/cost remain null. Warm-ups are separate from measured samples but count toward correctness. The CLI refuses to overwrite existing evidence and exits unsuccessfully after preserving any failed report.

The pinned candidate contract selection passes 111 tests (14.55s), including injected unverified, missing-write, extra-dispatch and timeout cases, successful sample accounting, failed-report exit and overwrite protection. The actual offline screen completes 30 measured repetitions per candidate plus two retained warm-ups: all 62 samples pass. This uses Pydantic AI 2.46.0 and Deep Agents 0.7.15 with synthetic models/tools and no provider network calls. Raw results: `bench/runtime/uat-candidate-screen-preserved.json`; verification: `bench/runtime/uat-screening-evidence.json`.

CI now includes these failure-contract checks and an always-run artifact upload for the screening report. YAML and step wiring were checked locally; hosted CI was not run. This is correctness evidence only, not matched native/candidate performance evidence. Process death before final report persistence is still outside this writer's coverage. Candidate qualification, the unresolved admission recovery work and all other acceptance gates remain open. No runtime selection or deployment occurred.


## Screening evidence survives process termination

A failing owned-subprocess test demonstrated that killing the screen before final report creation lost its sample evidence. The CLI now creates an exclusive JSONL journal, flushing and fsyncing metadata, each sample start and each observed outcome. Overall completion is recorded only after screening finishes. Either an existing journal or report prevents accidental overwrite. CI's always-run upload now includes both files.

The pinned candidate suite passes 112 tests (11.80s), including SIGKILL after a successful warm-up and the start of repetition 1. That drill preserves the finished sample and unfinished sample identity, produces no final report, and verifies a fresh invocation refuses to overwrite the partial evidence. The 30-repetition-per-candidate offline run passes all 60 measured samples plus two warm-ups; all 62 start/finish pairs match the final report exactly (126 journal events). Raw report/journal and verification are linked in `bench/runtime/uat-screening-journal-verification.json`. Ruff, formatting, diff and workflow YAML checks pass; hosted CI was not run.

This closes process-termination evidence loss in the offline screening writer. It is not a power-loss drill, production admission recovery, matched provider comparison or runtime qualification. Other acceptance gates remain open and no deployment occurred.


## Early approval stop through browser and native engine

The new browser-originated early-stop drill first exposed an inherited unit fixture disabling controls.stopped. All plan browser cases now explicitly restore the real durable-control function. With that corrected, the native provider gate denied dispatch but the runner incorrectly recorded failed rather than cancelled. A dedicated DurableStopError subtype now reaches shared failure finalization, producing cancellation with “Stopped as requested.” Ordinary budget/provider errors remain failures with their tracebacks; error-message text cannot trigger cancellation.

The browser scenario goes through actual Next proxies, native runner and private canonical PostgreSQL. It holds admission after the approval claim, observes accepted, records Stop, observes stopping and releases admission. The original execution then records cancelled with zero execution model calls and zero business-tool calls. The draft's two model passes and alignment check remain unchanged. This uses browser fetch/API calls; it is not a dedicated Stop-button rendering test or a process-death drill.

The focused runner/session/budget/control/outcome/size selection passes 188 tests (10.83s, one dependency warning). Canonical browser/native integrations pass 31 tests (23.89s), with one worker-only skip and one dependency warning. Ruff, formatting, Node syntax and diff checks pass; no size cap increased. Initial harness and product failures are retained in `bench/runtime/uat-early-stop-native-browser.json`. Deep-engine classification, orphaned pre-execution approval recovery and the other acceptance gates remain open. No deployment or full acceptance is claimed.


## Deep execution checks durable admission

The added deep-plan browser scenario reproduced worker launch despite an already committed request stop; its worker was synthetic and no external action occurred. Deep execution now checks tenant/run-scoped durable controls on its worker thread immediately before calling the deep worker. Typed stop uses the shared cancelled outcome and “Stopped as requested.” A separate failing test showed deep execution continuing after its initial audit write failed; that path now refuses worker launch with an accurate pre-execution failure.

The focused deep mode/plan, controls, outcome and size selection passes 52 tests (8.94s). The canonical browser/native suite passes 32 integrations (22.49s), with one worker-only skip and one dependency warning. The new case uses actual browser-originated requests, Next proxies, native engine and private canonical PostgreSQL, holds approval after the durable claim, records Stop and releases admission. It verifies cancelled execution and zero deep-worker, execution-model and business-tool calls. The audit-write failure is injected separately in a unit test. Existing successful deep context/progress/error behavior still passes.

The initial function-size check found execute_deep one line over its cap; moving configuration construction into the admission helper fixed this without increasing any cap. Ruff, formatting, Node syntax and diff checks pass. All failures and evidence are recorded in `bench/runtime/uat-deep-admission-stop.json`. This verifies admission, not enforcement inside an already-running deep worker or recovery after process death. Those and the remaining acceptance gates stay open. No deployment or full acceptance is claimed.


## Real deep-worker progress boundary

Inspection before testing running-worker cancellation found that the runner passed on_event to a real worker function that did not accept it. A new test calling AgentRunner through checked admission into the actual execute_deep_reason reproduced TypeError; earlier runner tests replaced the whole worker. The worker now accepts the optional synchronous callback and reports running/completed/failed milestones. Callback delivery exceptions are logged without changing the reasoning result or repeating execution.

The focused real-worker/deep/admission/size selection passes 79 tests (5.28s). Only the external RLM framework is simulated in the new runner-to-worker test; it verifies a single completion call, output and cost. Direct worker tests verify both connected and disconnected progress callbacks. A missing pytest import in the first expanded test run was corrected and its log retained. Ruff, formatting and diff checks pass. Evidence: `bench/runtime/uat-deep-worker-progress-interface.json`.

This fixes the internal call boundary, not live framework qualification or cancellation of an already-running deep worker. That original cancellation check and other acceptance gates remain open. No deployment or full acceptance is claimed.


## Retain late deep-worker results after cancellation

A failing thread-cancellation test reproduced lost finalization: cancelling deep delivery discarded the future's returned result and left the progress task running. Deep worker calls now shield the worker future, clean up progress in a finally block, and retain a tracked waiter plus a one-time result callback after caller cancellation. Returned worker output/cost is recorded as cancelled, never successful completion of the interrupted request. Cancelling the registry waiter still leaves the worker future/result callback intact while the loop is alive.

A new canonical native integration additionally reproduced recovery showing running despite a durable Stop. Nonterminal runs now consult their scoped durable control authority and expose stopping while awaiting final evidence. The integration starts a synthetic worker, records Stop, cancels delivery, verifies nonterminal stopping, releases the worker and reads its cancelled audit row, response evidence and cost. Only one worker call occurs.

The focused deep/worker/recovery/control/size selection passes 140 tests (9.77s). The canonical browser/native command passes 33 integrations (25.75s), with one worker-only skip and one dependency warning. Ruff, formatting and diff checks pass; no size cap increased. Failures and evidence are retained in `bench/runtime/uat-deep-worker-cancellation.json`.

This preserves late results within a live process; it does not forcibly stop an OS thread or prove provider/tool enforcement inside the external deep framework. Process-death recovery and those internal controls remain open, as do other acceptance gates. Loop shutdown leaves unknown evidence open rather than falsely claiming completion. No deployment or full acceptance is claimed.


## Deep callbacks retain owner controls; recovery races fixed

Two failing private-database tests reproduced deep callbacks losing context on framework threads and allowing tool dispatch after durable Stop. Runner-owned deep execution now binds its trusted tenant/run identity while constructing the six built-in callbacks. Each callback copies the captured context, checks durable controls before invocation and denies dispatch if the control store is unavailable. Failed worker exit restores the previous binding. This preserves owner context under concurrent callback execution.

The canonical worker test starts before Stop, then attempts an exec_shell callback after Stop. The underlying effect is synthetic and never called. The cancelled audit retains its late response, denied-call evidence and cost. The full browser run exposed two additional races: a same-request approval retry could return an admission refusal after another request changed the cache; and draining background work could miss persistence spawned by a finishing worker. Approval now rechecks durable admission before that refusal. Registry drain includes newly spawned work within its original deadline and cancels remaining children when the deadline expires. Both have deterministic failing-then-passing regression tests.

Final evidence at `45ec6dd16d7`: 95 focused deep checks (9.73s), 140 approval checks (12.07s), 66 combined race/size checks (6.85s), and 33 canonical native/browser integrations (24.51s, one worker-only skip and one dependency warning). The broad engine selection passes 11,119 tests, with 29 skipped, 185 deselected and 393 warnings (352.57s). All 85 goal tests pass (10.68s, two warnings). The broad run used unchanged code/tests and is a single clean invocation. Existing unawaited connect_tcp warnings remain; collection location does not establish the initiating path. Ruff, formatting, Node syntax and diff checks pass. Failures and logs are preserved in `bench/runtime/uat-deep-tool-controls.json` and `uat-verification.json`.

This binds runner-owned deep custom callbacks only. Standalone/unbound entry paths, arbitrary REPL code, framework-owned provider requests, full deep authorization parity and process-death recovery remain unqualified. These passing suites do not establish all acceptance gates, select a replacement runtime or authorize deployment. No production action occurred.


## Stop remains available during original-request recovery

At `103e2dc7371`, recovering chat messages retain a Stop control after connection loss and reload. The control uses the message's original request and agent, requires a durable acknowledgement, prevents duplicate submissions while waiting, and reports an unconfirmed stop honestly. Audit recovery continues without resubmitting the original work. Terminal records remove the control; recorded stops remain visible while execution evidence is pending.

The fresh standalone build passes. All 1,660 frontend tests pass across 150 files (9.49s), including eight focused recovery/stop tests. All 33 canonical native/browser integrations pass against that build (28.00s, one worker-only skip and one dependency warning). All 36 rendered deep/ordinary chat scenarios pass (reported 1.3m), including Stop after connection loss and reload. The initial rendered run had six failures: old fixtures expected stream-only failure reporting even though chat now automatically reads the original audit. Those fixtures now retain failed/timeout/cancelled outcomes in their audit responses and require original-request lookups with one approval. The initial failure log is preserved. ESLint and diff checks pass.

Evidence: `bench/runtime/uat-recovery-message-stop.json`. The new rendered Stop-button cases mock backend APIs; separate canonical integrations cover actual Next/native controls and private audit persistence. These checks do not establish full acceptance, live-provider performance or production readiness. No production action or deployment occurred.


## Approval receipts survive replacement plans and session deletion

At `7168c64a184`, migration 140 stores approval receipts independently of the current chat plan. A failing private-database test showed that replacing a plan before its worker created an audit row erased original-request admission evidence. The approval now commits its receipt in the same transaction as the pending-to-approved change. Recovery and duplicate checks consult the scoped receipt after session replacement/deletion. Reusing a request or plan identity rolls back the attempted claim; receipt insertion failure also rolls it back. Existing approvals are backfilled without changing session data. Approval evidence remains distinct from execution or completion evidence.

Verification: 201 focused plan/recovery/packaging/size checks (13.82s), all 264 selected chat/plan-integrity/deep-plan checks (8.88s), and 33 canonical native/browser integrations (24.72s, one worker-only skip and one dependency warning) pass. The canonical drill now applies migration 140 on fresh databases and during populated upgrades, preserves snapshots including chat_sessions, checks the backfilled receipt, and verifies repeat apply is a no-op. All 52 migration/upgrade-routing/documentation checks pass. Ruff, formatting and diff checks pass; no size cap increased. `bench/runtime/uat-approval-receipts.json` retains initial failures, fixture-isolation corrections, the packaging failure before tracking the new SQL file, and the stale documentation-count correction.

Migration 140 must precede new admissions on this version; only disposable databases were migrated. Durable approval evidence does not establish recovery of an orphaned worker or authorize repeating uncertain work. Full acceptance, provider measurements and application rollout/rollback remain open. No production deployment or business action occurred.


## Normal-chat repeated confirmation acceptance case

At `41faa8f1fa6`, a new ordinary-chat test starts from a real draft in a private calendar-operation store. “Go ahead” runs through the native chat route, runner admission and real calendar handler, making one fake Calendar PATCH and verifying the attendee. A subsequent “Yes” runs through the same chat path and reads the stored operation result. It makes no additional Calendar requests. Both turns use zero model calls; direct and streaming model entry points fail the test if invoked. The actual returned transcript and private receipt status are recorded in `bench/runtime/uat-chat-confirmation.json`.

The single case passes (3.49s, one dependency warning). Combined confirmation, goal-chat, performance, benchmark and size checks pass all 15 tests (6.70s, one dependency warning). Ruff lint/format and diff checks pass. The case is included in the runtime-contract CI job, with no remote CI execution claimed. No product code changed in this step.

The user was asked to review the actual response wording in normal chat; manual acceptance remains pending. Calendar transport is fake and run/history persistence is mocked; operation persistence and handler behavior are real. This is not a live-provider latency or full durable-chat integration claim. No production notification, calendar change or deployment occurred. Full acceptance remains open.


## Explain delayed approval evidence and retain it after process exit

At `2d5b4c2985d`, outcome recovery distinguishes an approval receipt at least 60 seconds old with no execution record. It reports that missing evidence explicitly and continues checking the original request. Chat displays this recorded explanation and retains Stop, including after reload. A late execution result supersedes the notice. The notice remains nonterminal and unverified; it is not proof that no effects occurred or authorization to repeat the action. Legacy approvals without a receipt timestamp retain the ordinary admission explanation.

A failing private-database test reproduced the missing delayed state. A separate process test now commits the actual plan claim and exits abruptly with code 23 before invoking any runner. After session deletion, parent-process recovery still finds the receipt; no run row exists. This proves approval evidence survives that process boundary, not recovery of an executing or orphaned worker.

Verification passes 90 backend recovery/claim/size checks (8.45s), nine component checks (0.863s), all 1,661 frontend tests across 150 files (9.55s), all 36 rendered deep/ordinary chat cases (reported 1.3m), and 33 canonical native/browser integrations (25.40s, one worker-only skip and one dependency warning). The rendered delayed-approval tests use mocked backend responses; canonical backend persistence tests are separate. Fresh build, Ruff lint/format, ESLint and diff checks pass. Evidence is in `bench/runtime/uat-delayed-approval-evidence.json`.

The notice appears on the next outcome read after the receipt reaches the age threshold. This does not establish the general 60-second simple-action deadline or automatic orphaned-worker resumption. Manual review of the repeated-confirmation chat case remains pending. Full acceptance, production/provider qualification and rollout remain open. No production change occurred.


## Candidate qualification decision and new-admission preservation

At `ad65df0b14b`, both installed candidate adapters were tested with their own runtime/checkpoint envelope. Pydantic AI 2.46.0 and Deep Agents 0.7.15 reject resumption before host preparation or model work. All 43 adapter/boundary tests pass (2.05s); these tests prove safe refusal, not the required resumption capability. `bench/runtime/qualification-decision.json` records both integrations as not qualified for general replacement in this revision. OpenCode's existing transport-only evidence does not establish the controls needed to advance. No candidate is a finalist; retain the improved current engine under the approved fallback rule. This does not claim upstream frameworks cannot support recovery. Any future qualifying candidate still needs unchanged matched configurations, finalist sample counts, uncertainty and latency gates. No latency superiority is claimed.

The native new-admission preservation drill now includes the independent approval receipt and early request Stop. A completed synthetic new request makes one model call and zero business calls while leaving those records, the earlier cancelled run/control/checkpoint, paused goal/budget and uncertain calendar operation unchanged. All 33 canonical native/browser integrations pass (24.14s, one worker-only skip and one dependency warning). Evidence is in `bench/runtime/uat-approval-admission-preservation.json`.

This is record preservation under new native admission, not an application routing switch or binary downgrade. An application rollback drill remains a future promotion gate; no candidate is eligible for that promotion now. Product acceptance work continues independently, and the repeated-confirmation normal-chat review remains pending. No production deployment or action occurred.


## Broad engine regression after approval recovery

The single broad engine invocation at `90617b97d52` passes 11,126 tests with zero failures, 29 skipped, 185 deselected and 393 warnings (359.83s). Selection excludes slow, integration, LLM, e2e and smoke markers. Product code and selected tests were unchanged during the run. Later edits were documentation, bench adapter tests and the separately verified integration-marked admission-preservation test, which this selection excludes. The existing unawaited connect_tcp warning remains; its collection site is not evidence of its initiating path.

`bench/runtime/uat-engine-approval-recovery-regression.json` and `uat-verification.json` preserve the exact scope and log. This refreshes broad regression evidence after the receipt/recovery changes without claiming live-provider or full product acceptance. The current engine remains selected; manual repeated-confirmation review and remaining product acceptance gates are still open. No production deployment occurred.


## Bound supplied, explicitly simple interactive profiles

At `1714e70dd84`, the shared runtime also assigns a maximum 60-second deadline to root webchat/telegram admissions whose supplied agent configuration explicitly declares `difficulty_class=simple`. This does not depend on calendar operations being enabled and never extends a shorter deadline. Goals, parented/delegated work, resumes, planning/deep modes and benchmarks retain their existing policy. Existing deterministic calendar confirmation behavior is preserved. The policy changes time limits only; native authorization and confirmation remain in force.

The two new profile tests initially failed because neither request received a deadline. Final verification passes 69 deadline/admission/contracts/size checks (9.77s), 87 runner/chat/performance checks (6.98s, one dependency warning), and 33 canonical native/browser integrations (21.83s, one worker-only skip and one dependency warning). A private-database test stalls acceptance under a shortened automatically assigned test deadline: execution is never called, and the real tracking DAL records a recoverable timeout with its assigned deadline and original request identity. Ruff lint/format and diff checks pass; no size cap increased. Evidence is in `bench/runtime/uat-simple-profile-deadline.json`.

This is an incremental admission policy for supplied, explicitly simple configurations. Ordinary chat that resolves its configuration later and general natural-language classification remain open. The existing router's message-length/tool-count heuristic is not treated as proof that a request is simple. These results do not establish the full simple-action deadline target or full acceptance. The preceding broad engine result is retained with its original revision. No production deployment occurred.


## Resolve explicit simple profiles before ordinary native admission

At `bad57a53d59`, root webchat/telegram manifest resolution moves to the native admission boundary. Successful configurations are reused for execution; failed resolutions are carried into the existing native refusal path without another lookup. The original payment-text protection, failure explanation and execution authorization stay in that native path. Explicit simple profiles receive their deadline measured from boundary entry before lookup. A completed slow lookup therefore reduces remaining execution time instead of starting a fresh minute. Excluded goal/delegated/resumed/planning/benchmark modes retain their existing policy.

Verification passes 141 profile/deadline/admission/runner/chat/size checks (12.91s, one dependency warning), 33 canonical native/browser integrations (28.49s, one worker-only skip and one dependency warning), and the loopback HTTP acceptance test (23.87s) with 30 acknowledgement/Stop samples and active progress. The native-entrypoint/private-DAL test resolves a simple manifest with no caller-supplied configuration, stalls acceptance under a shortened deadline, and recovers a timeout before execution starts. Actual native execution verifies one lookup and a deadline before the synthetic model call. Missing and schema-invalid manifests retain their native refusal text with one lookup. Initial export/timestamp test diagnostics are retained in `bench/runtime/uat-resolved-profile-admission.json`; raw HTTP results are in `uat-resolved-profile-http.json`. Ruff lint/format and diff checks pass; size caps unchanged. Relevant tests are added to runtime-contract CI, with no hosted run claimed.

This closes late manifest resolution for explicitly simple interactive profiles. Arbitrary conversational classification remains open. Manifest lookup is still synchronous: returned lookup time is charged, but a blocked filesystem read is not interrupted by this change. Local HTTP measurements use synthetic provider/lookup fixtures and do not certify deployment ingress or live-provider latency. Full acceptance and the repeated-confirmation manual review remain open. No production deployment occurred.


## Interruptible profile lookup with durable admission outcomes

At `2ffe534bbc0`, interactive profile lookup runs off the event loop. Its wait is capped at 60 seconds from boundary entry and constrained by any earlier inherited deadline. Timeout or cancellation before native execution records a scoped original-request timeout or interrupted outcome. Earlier attempts keep their own outcomes; no completion or blanket absence of prior effects is claimed. A loader-originated TimeoutError is not mislabeled as expiration of the runtime deadline.

Two private-database tests block a real lookup thread while the event loop continues, then exercise timeout and explicit task cancellation. Each recovers a terminal, unverified admission outcome before releasing the thread. Releasing it later never starts native execution. The fixture releases and joins its own threads. Final verification passes 160 lookup/profile/deadline/admission/runner/chat/size checks (14.94s, one dependency warning), 33 canonical native/browser integrations (26.08s, one worker-only skip and one dependency warning), and the loopback HTTP acceptance test (24.55s) with 30 acknowledgement/Stop samples and progress. Ruff lint/format and diff checks pass; size caps unchanged. Lookup checks are included in runtime-contract CI with no hosted run claimed. Evidence: `bench/runtime/uat-profile-lookup-interruption.json` and `uat-profile-lookup-http.json`.

This removes the event-loop block and bounds the caller's wait; it does not forcibly terminate a filesystem thread or establish resilience under permanent executor saturation. Prompt acknowledgement during every slow lookup, general conversational classification, remaining manual review and full acceptance stay open. Synthetic providers/private test stores were used. No production action or deployment occurred.


## Acknowledge before interactive profile lookup

At `1635530c6c0`, root interactive requests attempt acknowledgement before profile lookup. Delivery retains the existing one-second bound and respects an earlier inherited deadline. The attempt is carried into CurrentRuntime so it does not produce another accepted event after lookup. Cancellation during pre-lookup delivery records an interrupted or expired admission outcome without loading a profile. Failed delivery does not resubmit execution.

Two blocked-lookup tests initially failed because no accepted event arrived before the read. They now assert that the event precedes lookup-thread entry for both timeout and cancellation. Native execution verifies one acknowledgement, one lookup and one synthetic model call, including an acknowledgement callback that raises. The new private-DAL cancellation check recovers the original request's cancelled admission while lookup stays uncalled. Final verification passes 162 focused checks (17.86s, one dependency warning), 33 canonical native/browser integrations (25.46s, one worker-only skip and one dependency warning), and the loopback HTTP acceptance test (23.88s) with 30 acknowledgement/Stop samples and progress. Ruff lint/format and diff checks pass; size caps unchanged. Evidence is in `bench/runtime/uat-profile-acknowledgement.json` and `uat-profile-ack-http.json`.

The HTTP cohort uses a synthetic provider; blocked-read ordering is verified separately. This does not certify deployment ingress or guarantee delivery through a failed connection. Progress during prolonged pre-execution lookup, remaining manual review and full acceptance remain open. No production action or deployment occurred.


## Preparation progress and Stop during blocked lookup over real HTTP

At `9c20b3b4eee`, pending profile reads emit preparation progress every five seconds, including elapsed time and the original request identity. Callback delivery is bounded to one second. The same lookup remains owned throughout; delivery failure does not restart it. Completion/cancellation ends progress ownership, and deadline or cancellation still interrupts stalled progress delivery. Two tests first reproduced the absent preparation updates, then passed after wiring this behavior.

Final verification passes 164 profile/progress/deadline/admission/runner/chat/size checks (16.39s, one dependency warning) and 33 canonical native/browser integrations (22.32s, one worker-only skip and one dependency warning). A new real loopback HTTP test passes (15.10s): across 30 requests, acknowledge before the blocked read, observe preparation progress, durably Stop, and read the cancelled original-request outcome before releasing the read. It asserts zero model/business calls and one lookup per request, and cleans up its threads/server. Measured acknowledgement p95 is 42.72ms and Stop acknowledgement p95 is 7.08ms; the two preparation updates arrive at approximately 5.01s and 10.01s. Raw samples and bootstrap intervals are retained in `bench/runtime/uat-profile-preparation-http.json`; scopes and failures are in `uat-profile-preparation-progress.json`. Ruff lint/format and diff checks pass; size caps unchanged. These tests are added to runtime-contract CI with no hosted run claimed.

The slow reads are synthetic and the services/database are isolated. This proves the local pre-execution delivery/control/audit path, not deployment ingress, permanent executor saturation, a matched live-model cohort or general conversational classification. Full acceptance and the pending repeated-confirmation manual review remain open. No production deployment occurred.


## Isolate blocked profile reads from control and audit workers

At `3581b1a9db5`, configuration reads use a dedicated four-worker pool. Capacity is acquired before submission and held until the actual worker future finishes, including when its async caller is cancelled. Waiting callers remain cancellable without occupying default-executor threads; tenant ContextVars are copied into each read. Pool ownership resets after fork.

The initial reproduction failed: two blocked reads exhausted a two-worker default executor and a control stand-in timed out. The fix passes 86 profile/admission/deadline/contracts/size checks (10.62s, one dependency warning), including component tests with 1, 2, 5 and 20 tenant contexts and cancellation of running/queued readers. Canonical native/browser integration passes 33 checks (21.30s, one worker-only skip and one dependency warning). The real loopback HTTP test passes (16.58s): 30 sequential requests acknowledge, durably Stop and recover a cancelled original-request audit outcome before each blocked read is released, with zero model/business calls. Acknowledgement p95 is 44.19ms and Stop p95 is 7.15ms; preparation updates arrive at approximately 5.02s and 10.02s. Raw samples and intervals are retained in `bench/runtime/uat-profile-pool-http.json`; failure and scope are recorded in `uat-profile-pool.json`. Ruff lint/format and diff checks pass; pool tests are included in runtime-contract CI with no hosted run claimed.

This protects default-executor capacity from configuration reads. It cannot kill a permanently blocked filesystem thread: four such reads exhaust this dedicated pool, later callers remain subject to admission deadlines, and process shutdown may still wait for the threads. Concurrent HTTP operating limits, permanent-failure shutdown, live-model latency, remaining manual UAT and full acceptance are not established. The previous broad engine result retains its original revision pending the new regression run. No production deployment occurred.


## Concurrent HTTP preparation, Stop and original-request audit recovery

At `3014f6006e3` (product code `3581b1a9db5`), the local HTTP load check runs 30 concurrent rounds at each of 1, 5 and 20 synthetic tenants: 780 requests total. Each round waits until all requests have been acknowledged and the available profile pool is occupied, then issues Stop concurrently. Every request reads its own terminal cancelled admission record before the fixture releases any reader. The queued lookups never execute; the cohort makes zero model and business calls. The final run passes all three parameter cases in 16.40s. Confidence intervals resample whole concurrent rounds, preserving within-round contention. Individual samples and intervals are retained in `bench/runtime/uat-profile-load-{1,5,20}.json`; exact scope and fixture diagnostics are in `uat-profile-load.json`.

The initial three setup errors were missing imported engine fixtures, corrected before requests ran. An earlier passing run used individual-request bootstrap intervals; the final report uses round-level resampling. Ruff lint/format and diff checks pass. The test is included in runtime-contract CI; no hosted run is claimed. This establishes this synthetic preparation workload up to 20 simultaneous HTTP tenants, not general fleet capacity, live-model latency, competing goals or permanent-failure process shutdown. No production deployment occurred; full acceptance remains open.


## Broad engine regression after bounded profile admission

The broad engine invocation at product revision `3581b1a9db5` passes **11,156 tests with zero failures**, with 29 skips, 185 deselections and 393 warnings in 365.12s. Selection: `not slow and not integration and not llm and not e2e and not smoke`. Product source and collected engine tests remained unchanged during the invocation; subsequent concurrent HTTP benchmark tests and documentation are outside its collected scope. Local load checks ran concurrently, so this elapsed time is not a matched baseline performance comparison. The existing unawaited `connect_tcp` coroutine warning remains; where garbage collection reported it does not identify its initiating path. The prior broad result is preserved separately. Evidence: `bench/runtime/uat-engine-profile-pool-regression.json`.

This refreshes broad regression evidence after profile lookup, acknowledgement, progress and worker-capacity changes. The separately recorded canonical native/browser checks cover selected integrations; excluded categories, live-provider performance and remaining user acceptance are not certified by this run. No production deployment occurred; full acceptance remains open.


## Configured cloud fallback and truthful host-deadline finalization

At `49627a99058`, the owning asyncio deadline window is available to native cancellation finalization. When that window has actually expired, the run retains the host-deadline explanation before asyncio converts its cancellation into `RuntimeDeadlineError`. Ordinary cancellation is not relabeled, and window context resets on exit. Existing enclosing host/workflow cancellation still uses cancelled status; this change corrects the reason, not the status convention.

The new wire tests use the selected main manifest's cloud chain and temperature with the isolated native action fixture and mocked HTTP transport. Synthetic 503 responses on the first one or two models each receive one native retry before advancing. Every attempt retains the original deadline. Successful fallback writes the synthetic value once, independently verifies it, and stops model work. A stalled fallback expires with zero writes. Two assertions first reproduced the lost deadline reason in the run submitted to persistence. Final verification passes 167 fallback/deadline/admission/runner/size checks (12.05s, one dependency warning) and 33 canonical native/browser checks (23.94s, one worker-only skip and one dependency warning). Ownership tests cover ordinary cancellation and context reset. Ruff lint/format and diff checks pass; CI includes the wire test, with no hosted run claimed.

Evidence and initial fixture diagnostics are retained in `bench/runtime/uat-fallback-deadline-reason.json`. The wire cases mock persistence and verify its arguments; they do not establish real-database storage of this specific reason, full manifest parity, live-provider latency, local GPU fallback or concurrent fallback load. The broad 11,156-pass result remains attached to its preceding product revision. Full acceptance and remaining normal-chat review stay open. No production deployment occurred.


## Persisted interruption causes recovered through the chat API

At test revision `fd6c67f2b6c` (product `49627a99058`), two new canonical integration cases execute the native runner against disposable PostgreSQL. Synthetic primary failure advances to a stalled fallback. One case expires its host deadline; the other explicitly cancels before expiry. The actual tracking finalizer writes one terminal cancelled run with the correct cause, completion timestamp, original request identity and deadline. Two read-only chat outcome API calls recover that exact row and explanation without another model or business call. A foreign tenant cannot retrieve it. Each execution makes two synthetic provider calls and zero business calls. The API supplies a trusted fixture identity; it does not test JWT issuance or deployed ingress.

The canonical migration/native/browser command passes 35 checks, with one worker-only skip and one dependency warning, in 26.63s. Fresh/populated upgrade assertions and existing native/browser scenarios remain in that command. Initial fixture failures occurred after the database checks: the standalone chat router returned 503 because its engine/config fixture had not been initialized. Correcting the fixture produced the final pass; no additional product change was needed. Ruff lint/format and diff checks pass. Evidence: `bench/runtime/uat-native-deadline-recovery.json`. The canonical harness includes these checks in its existing CI path; no hosted run claimed.

This closes real-database and chat recovery coverage for the deadline reason fixed in the preceding revision. It does not establish live-provider outage latency or remaining manual acceptance. A separate broad engine refresh is running; no result is assumed. No production deployment occurred, and full acceptance remains open.


## Preserve interrupted native live benchmark samples

At `45a41d171d5`, each native live sample durably records its model/repetition and start before execution. Terminal outcomes are flushed and fsynced before the corresponding finish event. Cancellation records an interrupted row before propagating; runtime deadline exceptions retain timeout classification and unknown usage/cost stays null. Exclusive journal creation prevents mixing a fresh run with earlier interrupted evidence. A killed process leaves an identifiable unresolved sample rather than silently reducing the measured population.

Four offline tests pass in 3.12s. Subprocess cases drive the actual opt-in live harness with a nonexecuting fixture runner and no credentials: SIGKILL preserves the started sample, while task cancellation leaves a matching interrupted row and finish event. Additional checks cover timeout classification and the existing candidate journal. Ruff lint/format and diff checks pass. CI includes the offline journal tests; no hosted run claimed. Evidence: `bench/runtime/uat-native-live-journal.json`.

This is process-interruption accounting, not automatic replay or recovery of provider usage. A separate 30-sample selected-primary live cohort is running with synthetic business tools; its result is not assumed. The broad engine refresh excludes the opt-in slow live harness. Full acceptance remains open and no production deployment occurred.


## Selected-primary live action cohort and broad deadline regression

The selected primary `openrouter/deepseek/deepseek-v4.1-flash` passed 30/30 live native action samples at benchmark revision `45a41d171d5` (product `49627a99058`). Each sample performed exactly one independently verified synthetic write and made no provider or tool calls after success. Measured p95 was 5.496s, with bootstrap 95% interval 3.563–7.298s. All samples remained within their 60-second host deadline. The journal contains 30 starts and 30 matching terminal outcomes, with no unresolved samples. The test passed in 81.69s with one dependency warning. Engine-estimated total cost is $0.01362441; billing reconciliation is not claimed. Raw rows, durable events and exact scope are in `uat-live-primary-current.jsonl`, `uat-live-primary-current.events.jsonl` and `uat-live-primary-current-summary.json`.

This meets the 30-second reference for this synthetic single-action cohort using the selected primary. It is not a matched baseline/candidate comparison or a representative chat/goal workload; it does not resolve the earlier MiMo deadline failure. Some samples overlapped the separate broad engine suite. Previous observations and failures remain preserved. No real business tools or production changes were used.

The broad engine refresh at `fd6c67f2b6c` passes **11,162 tests with zero failures**, 29 skips, 187 deselections and 393 warnings in 370.91s. Product source and collected engine tests remained unchanged during that invocation; the later journal changes affect bench helpers and the excluded opt-in slow live test. The new canonical deadline recovery tests are integration-marked and verified separately in the 35-pass native/browser run. The existing unawaited `connect_tcp` warning remains. Elapsed suite time is not a performance comparison. Evidence: `bench/runtime/uat-engine-deadline-reason-regression.json`. Full acceptance, broader provider performance and remaining normal-chat review stay open; nothing was deployed.


## Repeat the accepted normal-chat workflow with live configured models

At `cd9c7ce68a3`, the status/pause acceptance fixture records elapsed request time for each chat turn. A cohort driver repeats its live option in 30 isolated test processes with fresh private goal/task data, preserving transcripts, logs and durable sample starts/finishes. A failed conversation remains in the cohort and does not suppress later samples. An existing output directory is refused. The configured model chain is read from the existing main manifest; the fixture retains its 12-provider-attempt bound per conversation and 60-second host deadline per turn.

Both scripted chat cases pass (4.76s, one dependency warning). Four harness/journal checks pass (4.34s), including preservation of an injected partial timeout conversation while the remaining 29 samples execute, refusal to overwrite its evidence, and native journal interruption checks. Ruff lint/format and diff checks pass; offline cohort checks are included in CI with no hosted run claimed. Evidence: `bench/runtime/uat-chat-cohort-harness.json`.

The 30-conversation live run has started in `/tmp/runtime-chat-cohort-current`; no result is assumed. It uses synthetic business dispatch and private goal/task stores, not production goals or a scheduler. It measures this isolated native-chat workflow rather than complete production configuration. Machine state assertions do not replace human review of the resulting language. Full acceptance remains open and no production deployment occurred.


## Keep incomplete chat populations visible

The offline cohort reporter accounts for every planned conversation as passed, failed, unresolved or not started. Timing from failed turns remains in the measured distribution; missing status/pause timings are counted explicitly and cannot qualify a complete cohort. It never infers manual acceptance from machine state. Two report/driver tests pass (0.21s): a 60.005-second timeout stays in latency, its missing pause stays missing, a started-but-unfinished conversation remains unresolved, and unstarted conversations remain in the population. Ruff lint/format and diff checks pass; the report test is included in CI with no hosted run claimed. Evidence: `bench/runtime/uat-chat-cohort-report.json`.

At this observation, the unchanged live process had completed six conversations successfully, with one in flight and 23 not started. The first three transcripts retained the open task and correctly described the paused goal, but were verbose and included fixture IDs/versions; they are not claimed as user-approved language or full production-profile behavior. This is partial evidence only. The run remains active; no production code, goal state or deployment was changed in this reporting increment. Full acceptance remains open.


## Completed live normal-chat status/pause cohort

The unchanged cohort at `cd9c7ce68a3` (product `49627a99058`) completed all 30 conversations: 60 live chat turns, no failed state checks, no unresolved starts and no missing turns. Every conversation left the goal paused with one DONE task and one TODO task. Status elapsed-time p95 is **19.563s** (bootstrap 95% interval **15.845–26.299s**); pause p95 is **27.825s** (interval **20.290–34.581s**). One pause took **34.581s**, retained in the distribution. No turn exceeded its 60-second deadline. The run made 198 provider attempts; all 60 returned responses identify the selected primary, DeepSeek v4.1 Flash. Estimated engine-accounting cost totals $0.0738; billing reconciliation is not claimed.

Raw transcripts, logs and the complete 30-start/30-finish journal are preserved in `bench/runtime/uat-chat-cohort-current/`. Summary and explicit scope are in `uat-chat-cohort-current-summary.json`. The observed per-turn p95 meets the 30-second reference for this isolated workflow, while the pause interval extends above it. This is not full production-profile equivalence, a forced fallback-chain outage test, or a matched replacement comparison.

Language acceptance remains open despite the passing state checks. In inspected examples 1, 19 and 20, the reply acknowledges disabled automatic pursuit but offers leaving the item to a scheduled review, potentially suggesting unavailable automatic progress. Replies also expose fixture IDs/versions and are verbose. This is an implementing-assistant review finding, not independent scoring or user acceptance. The cohort is preserved before any wording change. Full acceptance remains open; no production goal or deployment was changed.


## Clarify unavailable scheduled-review options

At `57d0eb02978`, goal-read tool guidance explicitly says that a scheduled review cannot advance the goal while automatic pursuit is disabled. It tells the model not to offer leaving work to that review as an available next step before authorized enablement. It also asks for plain-language finished/open work rather than internal IDs or versions unless requested. Data, permissions, scheduler behavior and execution enablement are unchanged.

Twenty-four goal-tool, scripted chat/control and size checks pass (6.12s, one dependency warning), with Ruff lint/format and diff checks passing. This verifies structured behavior, not live compliance with the wording guidance. The prior 30-conversation cohort remains preserved. A new 30-conversation run has started in `/tmp/runtime-chat-cohort-wording` using the same isolated workflow and configured model chain; its outcome is not assumed. Evidence: `bench/runtime/uat-goal-review-wording.json`.

Prompt guidance is not deterministic enforcement. The repeated live results must be reviewed before claiming the observed wording defect fixed, and human acceptance remains pending. Full acceptance stays open. No production deployment occurred.


## Reject and revert the wording-only experiment

The 30-conversation repeat at `57d0eb02978` completed with all 60 turns and all machine state checks passing, but **the experiment did not pass acceptance**. Status elapsed-time p95 was **35.617s** (bootstrap 95% interval **27.007–35.808s**), exceeding the 30-second reference and the preceding cohort's observed 19.563s. Pause p95 was **21.811s** (interval **16.763–23.007s**). Three status turns exceeded 30 seconds; none exceeded 60 seconds. The cohort made 180 provider attempts, and estimated engine-accounting cost was $0.07, without billing reconciliation.

Inspection of all 30 status replies found that examples 1 and 3 still acknowledge disabled pursuit and then offer leaving the item to the review. The additional guidance therefore did not reliably remove the target defect. Some replies also differ on who can enable pursuit in this limited fixture. Machine state success is not treated as language acceptance. The two sequential cohorts were not interleaved or randomized, and provider-versus-host timing was not retained; the elapsed-time increase does not establish its cause.

The added guidance was reverted at `91e7e63e7b6`; `robothor/goals/tools.py` again matches product revision `49627a99058`. Raw transcripts/logs/journal and the explicit rejection are preserved in `bench/runtime/uat-chat-cohort-wording/` and `uat-chat-cohort-wording-summary.json`. The prior wording defect remains open. The next work is timing attribution and grounding of available next actions, not treating the prompt edit as successful. No production deployment occurred; full acceptance remains open.


## Attribute chat time before another behavior change

At `2651401d7ba`, the diagnostic chat fixture retains native run measurements and per-step model/tool timing, token counts and failure flags. It checks successful-turn identity and compares measured model/tool counts with actual calls. Six scripted-chat/performance checks pass (3.98s, one dependency warning). Two separate live diagnostics pass (35.04s and 26.83s including fixture setup), with their full synthetic transcripts preserved. The first captured aggregate timings; the final revision also captures individual steps.

Across these four turns, recorded model-call time accounts for nearly all elapsed time: tool work is 14–20ms, with less than 160ms of other elapsed time. In the detailed status turn, the list/get decisions take 2.198s and 1.548s, and the final response takes 10.681s. The detailed pause final response takes 5.284s. This points the next investigation toward model round trips and response generation rather than local tool latency. Evidence: `bench/runtime/uat-chat-timing-attribution.json` and `uat-chat-timing-diagnostic-{0,1}.json`.

These are diagnostics, not a new latency cohort or a causal explanation of the earlier p95 increase. Native LLM_CALL timing includes the call/streaming path; it is not a separate measurement inside the provider. The earlier cohort cannot be retroactively given these measurements. Product behavior remains at `49627a99058`; the ineffective wording experiment remains reverted. Wording and full acceptance stay open; no deployment occurred.


## Bounded task facts in goal discovery

The goal-list tool now returns exact linked-task counts and at most three task titles per status, with explicit truncation flags. A single tenant-scoped window query covers the returned goals; default store/API listing remains unchanged. Goal evidence, version and status are not changed by reading task facts, including when all linked tasks become DONE. Individual detail reads retain their explicit goal identity requirement. Versioned controls accept the current version from either goal read.

All 92 goal/size checks pass (13.00s, two dependency warnings); the initial focused tool/scripted-chat selection passes 19 checks (5.74s, one warning). Canonical native/browser integrations pass 35 checks (25.72s, one intentional worker-only skip and one warning). The live fixture now accepts either authorized goal-read tool rather than requiring the extra detail read whose removal is under evaluation; completion, exact task states, provider accounting and one requested pause remain checked.

Two live diagnostics are retained in `bench/runtime/uat-chat-task-summary-diagnostic-{0,1}.json`. The first still made three status model calls and took 49.245s. After documenting which status facts the list contains, the second used two status model calls and only the list tool, taking 11.843s. Its pause used four model calls and took 18.260s. These isolated observations establish that the shorter status path is possible, not a p95 improvement or no-regression finding. Replies remain verbose and expose internal details; manual acceptance and broader correctness/performance remain open. Evidence and limits: `bench/runtime/uat-chat-task-summary.json`. No deployment.


## Honest screening exit status and Unicode fixture repair

The task-summary cohort at product `2488b5322db` exposed a test-database defect: sample 3's pause note contained an em dash that JSONB could not store in the private SQL_ASCII cluster. The transaction failed and the goal remained waiting. A deterministic Unicode-note test reproduced the same error. The goal fixture now explicitly initializes UTF8, as the canonical migration harness already does; no production database was changed.

The cohort was deliberately interrupted after identifying this defect. Retained evidence contains four passed conversations, one failed conversation, one unresolved started conversation and 24 not started. The wrapper exited 130; its owned child exited and its orphaned private database was stopped. All transcripts/logs/events and the selected-model snapshot are retained under `bench/runtime/uat-chat-cohort-task-summary-ascii/`. This incomplete population does not qualify as screening evidence and must not be merged with a corrected run.

Separately, four failing command tests showed that collection returned success without evaluating outcomes. The command now writes `summary.json` and exits nonzero for failed/missing conversations, p95 above 30s, or a single turn above 60s even when p95 passes. The report keeps confidence intervals and manual acceptance false; passing this screen is not full acceptance. Seven focused reporting tests pass (0.41s). After the UTF8 repair, all 96 goal, scripted-chat and reporting tests pass (13.11s, three dependency warnings). Ruff and formatting pass. Evidence: `bench/runtime/uat-chat-screening-exit-unicode.json`. No deployment.


## Task-summary live screen finished: acceptance failed

The corrected UTF8 cohort finished all 30 conversations at harness `fc5b658fdec`, product `2488b5322db`: 60 completed turns, 30/30 correct final goal/task states, no missing or unresolved samples. The collector exited **1**, correctly failing the latency screen. Status p95 is **32.657s** (bootstrap 95% interval **24.287–33.329s**); Pause p95 is **35.423s** (interval **21.296–49.293s**). Both exceed the 30s target. This does not support a no-regression finding against the preceding observed 19.563s/27.825s cohort; sequential provider observations are not a causal attribution to the change.

All 30 status replies were inspected. Examples 3, 8, 10 and 24 still offer leaving work to a scheduled review while pursuit is disabled. Other replies imply a task change can overcome that disabled setting or offer enablement outside the fixture's tools. These examples are not an exhaustive semantic failure count. The original user-accepted scripted status/pause behavior remains the only manual acceptance.

All raw transcripts, logs, the durable journal, machine screen and frozen selected-model settings are retained in `bench/runtime/uat-chat-cohort-task-summary-utf8/`; augmented measurements and limits are in `uat-chat-cohort-task-summary-utf8-summary.json`. The factual renderer and report-channel prototypes were not active in these runs. No deployment, model selection change or full acceptance is claimed. Next work is native integration of the factual report without dropping mixed requests, bypassing permissions or continuing model work after a verified final report.

## Factual goal reports and audit recovery

The native chat path now supports a final single-goal report built from an authorized, fresh goal/task/child snapshot. The model selects the goal but cannot supply report facts. The run-scoped channel denies mixed tool batches, background/delegated execution and mismatched identities; pending steering prevents publication of a stale report. Existing final outcome validators still run. The report and underlying tool snapshot are persisted in the run audit.

The canonical private-database recovery test retrieved the exact original report twice after the synthetic goal changed, without another model call or repeated action. A foreign tenant could not retrieve it. Rendered chat tests cover reports delivered only in the final event and replacing an earlier streamed preamble. These tests do not prove automatic reconciliation for every external provider.

At `e949d6bedf8`, the selected broad engine suite passed 11,175 tests (29 skipped, 188 deselected), and canonical integration passed 36 (one skipped). Subsequent tool-description guidance and focused native/control/handler/collector checks passed 27. The full frontend suite was not repeated; the affected component file passed 13 tests. Logs are linked from `bench/runtime/uat-verification.json`.

The first live diagnostic failed factual-report adoption: both states were correct but the model wrote freeform replies and offered a review despite disabled automatic pursuit. That failure is preserved as `uat-factual-report-live-diagnostic-0.json` and its log. Goal read/list descriptions now direct final single-goal replies to the report tool; ordinary reads remain available for additional work and multiple-goal questions. The existing observation ledger already classifies the report as a read, so no classification change was needed.

The next live diagnostic passed both turns using the existing selected model chain, synthetic tools and a private database. Status used two model calls, pause three, and neither made a model call after publishing the report. It accurately said scheduled reviews would not run while pursuit was disabled. This is one diagnostic, not a qualifying performance cohort or user acceptance. The collector now supports an explicit factual-report scenario and preserves that setting alongside its frozen model configuration. Earlier failed cohorts remain recorded. Overall acceptance and production deployment remain false.

## Repeated factual-report screening and uncertain write responses

At `37d510a4174`, the 30-conversation factual-report cohort passed all 60 native chat turns with the selected model chain. Every turn used the host report; tasks remained DONE/TODO and each goal ended paused. Status p95 was 12.323 seconds (bootstrap interval 9.009–43.529); pause p95 was 22.483 seconds (7.580–57.887). Two replies exceeded 30 seconds, none exceeded 60. Screening passed; broad intervals and sequential collection prevent a causal speed claim. The cohort used 160 provider calls, with estimated cost $0.0498, not billing-reconciled. Full samples, failures journal and frozen configuration are preserved in `bench/runtime/uat-chat-cohort-factual-report/`; the augmented summary is `uat-chat-cohort-factual-report-summary.json`.

A separate synthetic write-response contract reproduced three misleading retry classifications: the write committed before a timeout, HTTP 500, or unreadable HTTP 200 response, and the shared client nevertheless marked each retryable. Potentially mutating calls now report `outcome_unknown: true` and `retryable: false`; ordinary reads retain retry behavior. The handler-exception path uses the existing read-only classification. A connection-pool timeout at the direct HTTP boundary remains retryable because no request was dispatched. Focused service/dispatcher/size checks pass 33 tests after extracting the status mapper to preserve size limits. Red and intermediate failure logs are retained.

This classification does not by itself enforce a durable duplicate barrier, implement every provider's readback, or recover a committed goal control after a later model failure. Those remain open acceptance work. No production deployment or overall manual acceptance is claimed.

## Recovery planner honors unknown effects

At `30453348e10`, the uncertain-write classification change passed the broad engine selection: 11,182 passed, 29 skipped, 188 deselected, 393 existing warnings, 387.63 seconds. Native canonical integration passed 36 with one skipped. Source and collected tests remained unchanged during the broad run; this is correctness evidence, not a latency comparison.

Four further regression cases showed that the recovery planner ignored structured uncertainty and chose behavior from timeout/error text. An additive `uncertain_outcome` category now takes precedence when a failed tool result explicitly reports an unknown outcome. Its recovery action directs the model to the original audit and read-only state checks, and says not to repeat, replace or delegate the unresolved action. The existing error-type storage is text; no migration is needed.

The new native scripted-provider case commits a synthetic write, loses its response, checks that the next model turn receives the correct recovery guidance, and reads the effect back. An independent host predicate then completes the run. Exactly one write, one read and two model calls occur; there is no model work after verified readback. The combined latest native runner/recovery/telemetry/completion/size selection passed 177 tests. See `bench/runtime/uat-uncertain-recovery-guidance.json`, including the retained four-case red result.

This verifies guidance and wiring with synthetic effects; it does not mechanically prevent a model from proposing another write, establish durable duplicate prevention after restart, or add every provider's readback. Those remain incomplete. The broad 11,182-test run predates this planner change and is not presented as a rerun of the latest revision.


## Durable effect journal: local recovery contracts

Migration 141 adds tenant/principal-scoped action admission and outcome records. Native runtime dispatch records an intent before invoking a mutating tool. An unresolved identical intent cannot be dispatched again by a replacement worker. Uncertainty also fences related request/goal/budget writes; reads remain available. Trusted settled readback can confirm the recorded result or prove nonapplication. Confirmed recovery returns the recorded result without another write. Cancellation and late-worker tests preserve uncertainty after dispatch; cancellation before dispatch withdraws admission. Existing calendar reconciliation retains its separate ledger.

Local tests include concurrent identical admissions, tenant isolation/RLS, a real subprocess dying after a synthetic provider write, a replacement native runner attempting the same write, and trusted synthetic readback. The broad engine selection passed 11,209 tests, 29 skipped and 189 deselected (393 warnings, 383.23 seconds). Canonical native integration passed 37 with one skipped. Both runs preceded a further enforced-verification fix: failed readback now says to inspect the audit and reconcile, never to blindly retry or claim nonapplication. The final focused selection including that fix passed 67 tests in 7.55 seconds. Logs and scope are recorded in `bench/runtime/uat-verification.json`.

Intermediate failures are retained: prepared-state abandonment SQL, omitted migration registration, and an incorrect scripted-provider phase in the new native fixture. These were corrected before the successful results above.

This is not complete automatic recovery. The abandoned-run operation is not yet wired into startup/reaper recovery, generic trusted readback callbacks are supplied by tests, and the new ledger is not yet projected into the goal workspace or chat outcome endpoint. Uncertainty can currently block internal goal wait/progress bookkeeping, which needs a narrowly authorized path without clearing the business-write fence. Runtime-bound dispatch is required. No production migration, deployment, framework selection or overall manual acceptance is claimed.

Final-source canonical integration was rerun without the optional browser cases: 31 passed, one skipped, in 8.27 seconds. See `uat-runtime-effects-canonical-latest.log`.


## Terminal-owner effect recovery

Following `21b085ed031`, daemon startup and periodic stale-run cleanup now invoke a bounded tenant-scoped sweep even when no new stale runs were found. The sweep joins the persisted owning run and processes only completed, failed, timed-out, or cancelled owners. Prepared admissions become `not_applied`, which denies a late worker permission to dispatch. Dispatched effects become `uncertain`, retaining the duplicate fence and requiring provider readback. It neither retries actions nor infers outcomes from elapsed time. Missing or live owners remain untouched. Each sweep locks at most 100 effect rows, skips locked rows, and retries housekeeping on later ticks if storage is unavailable.

The targeted recovery/dispatch/reaper/watchdog/size selection passed 95 tests in 12.57 seconds. Final-source canonical integration passed 32 with one skipped in 10.71 seconds, including a test against the real migrated run schema. An initial command referenced a nonexistent daemon test file and collected no tests; that log is retained alongside the corrected result. Evidence is in `bench/runtime/uat-verification.json`. The earlier broad engine run predates this change.

This supersedes the prior limitation that abandoned-record recovery had no daemon caller. It does not resolve service-specific effects automatically; trusted provider readback, goal bookkeeping during uncertainty, and user-facing recovery reporting remain open. No deployment or overall acceptance is claimed.
