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
