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
