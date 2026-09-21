# Runtime acceptance audit

Audited locally through 2026-09-21, against accepted integration revision `eea3252b15`.
This is a requirements audit, not release approval. The current engine remains selected.
The user's positive experience with existing task/goal behavior is the baseline to preserve.
The user need not identify whether an ordinary request became a task or a goal.

## Product and verification

| Requirement | Current evidence | Finding |
|---|---|---|
| Isolated baseline and business fixtures | Separate integration/work worktrees; `accepted-integration-baseline.json`, private PostgreSQL fixture, synthetic calendar/provider transports | Established for the recorded local scenarios; no production writes |
| Implemented / enabled / verified inventory | `runtime-modernization.md` inventory and `runtime-uat.md` follow-up | Recorded separately; local passing tests do not mean deployed |
| Per-scenario benchmark, failures and uncertainty | `bench/interactive/compare.py`, retained JSON/JSONL cohorts, seeded bootstrap intervals | Implemented; missing measurements remain missing |
| Equivalent configuration before performance qualification | Comparator now checks explicit `model_settings` and `resources`, in addition to model/prompt/tool/machine cohort | Mismatches rejected; legacy measurements remain readable but cannot qualify with unknown settings |
| Replaceable execution boundary | `runtime/contracts.py`, `CurrentRuntime`, and `@runtime_entrypoint` on `AgentRunner.execute` | Native admissions already use the adapter, including ordinary chat; no duplicate chat wrapper needed |
| Trusted identity / compatible checkpoints | Runtime context/envelopes, tenant-scoped setup, actual native saved-message continuation and original-request chat recovery | Native synthetic continuation verified without tool replay; alternate runtimes not yet equivalent |
| One execution loop / retry / compaction / delivery owner | Thin native adapter; native runner retains these responsibilities; deadline/retry tests | Covered natively, including nested deadline ownership |
| Shared family budgets / delegated work | `runtime/budget.py`, goal reservations and stale-worker tests | Covered for explicitly token-capped native goal families; unknown usage stays charged. Candidate provider-boundary wrappers and the durable attempt ledger pass concurrent reservation, reconstruction, cancellation, parent-limit and unknown-usage checks using a host-supplied bound; candidate goal-controller lifecycle binding now passes parent/child, capped/uncapped, uncertain-usage and shared native/candidate request tests; the host must still supply a conservative request bound |
| Draft versus execution / duplicate confirmation / ambiguous writes | Calendar operations and attendee suites; 100-sample native calendar cohorts | Covered for the supported calendar operation; not proof for every external business tool |
| Lost chat delivery / automatic audit recovery | Scoped original-request recovery, calendar readback, native continuation, durable effect journal, CRM note readback and authenticated note reconnect artifacts | Calendar and CRM note/task recovery are verified locally; pending effects remain fenced. Browser checks cover saved-plan recovery, approval/stop behavior, and original-request recovery after a real private task creation response is lost. Other business tools still need provider-specific readback |
| Failed draft / saved-plan readiness | `uat-plan-exploration-outcomes.json`, strict draft persistence and matching-plan recovery tests | Failed, timed-out and cancelled exploration cannot publish a partial plan; only a matching pending, unexpired saved draft is restored |
| Deterministic confirmation | `uat-deadline-calendar.json` | Zero model calls, verified single write and no later tools in all recorded confirmation samples |
| Completion without further model work | Native workflow-completion/output-validation tests and live host-contract cohorts | Covered when the host supplies an independent supported outcome predicate; arbitrary conversation is not independently verified |
| Warm simple-request local harness overhead | `uat-task-chat-screening.jsonl`: 30 warm ASGI chat-to-task requests, p95 48.759 ms (conditional interval 43.453–58.200 ms), scripted provider | Below 2 seconds for this minimal-profile case. Includes native admission, execution, receipt readback and response; excludes TCP/browser/live-provider latency, external audit/event delivery and embeddings. Not full representative-workload qualification |
| Bounded simple action | Explicit runtime-deadline contracts, native simple-profile admission and deterministic-operation cap | Eligible root interactive simple profiles and deterministic calendar confirmations receive a 60-second cap measured before profile lookup. Interrupted admission records a scoped outcome; blanket classification of all conversational requests remains unproven |
| Acknowledgement / progress / effective Stop | `uat-native-http.json`, `uat-profile-pool-http.json`, real local TCP + native runner + durable private store | Provider-wait and blocked-configuration cohorts each exercise acknowledgement, progress and durable Stop. Configuration reads use bounded separate capacity; component isolation covers up to 20 callers. `uat-profile-load.json` additionally verifies 30 rounds each at 1/5/20 concurrent HTTP tenants during blocked preparation; deployment-ingress p95 remains unqualified |
| Descendant controls / restart / stale workers | Real daemon goal crash/restart, scoped cleanup, continuation Stop inheritance, exclusive startup claims and database-session-loss admission checks | Synthetic native cases pass; network partitions and production failover remain unqualified. Already dispatched effects require reconciliation |
| Goal workspace: criteria, milestones, children, tasks, budgets, evidence, wake/review | Actual `GoalsView`, goal router/store, managed browser checks | Implemented and locally exercised; user acceptance still pending |
| Reviewable decomposition / steering / revisions | Goal API/store/model and browser tests | Explicit authorization retained; revisions invalidate prior evidence |
| Waiting without model polling / duplicate wakes / multi-day continuation | `test_native_multiday_goal.py`: actual native coordinator/runner and CRM with planning/task protocol, three ordinary requests, restored day-old waiting hierarchy, duplicate event, child then parent completion. Existing controller and full-daemon crash/idle tests remain | Combined native scenario passes with seven completed runs, exact family accounting and no idle/post-completion calls. Simulated day and fresh in-process instances; OS restart verified separately, not a multi-day live deployment |
| Fresh recurring evidence / independent versus judgment-based outcomes | Goal evidence tests and browser approval/revision scenarios | Supported calendar receipts checked independently; broader judgment requires operator review |
| Tenant isolation / delegated controls / memory | Native engine and goal suites, including RLS and tenant/control tests | Native behavior tested; no equivalent candidate evidence |
| Provider fallback | Native verified-outcome checks and `uat-fallback-deadline-reason.json` | Selected cloud chain passes synthetic HTTP 503 retry/fallback checks, one verified write, no post-success calls and shared host-deadline cancellation with accurate finalization reason. Live configured-chain outage/load behavior and local GPU fallback remain unqualified |
| Everyday requests competing with goals | `uat-mixed-runtime-final.jsonl` | 1,560 verified synthetic actions across 1/5/20 tenants; local resource limits published, not production capacity |
| No introduced regressions | At `e69866dfe64`: broad engine 11,298 passed (29 skipped) and canonical/native/browser 60 passed (two skipped). Earlier CRM/goals: 238; full frontend: 1,666; latest focused frontend recovery/streaming: 20. Logs and failures remain in `uat-verification.json` | Source stayed fixed for both latest invocations. Broad engine excludes slow/integration/LLM/e2e/smoke; existing warnings remain. No detected regression in these scopes, not full acceptance |

The factual single-goal chat path passed 30 live screening conversations at `37d510a4174`: all 60 turns used trusted reports, with p95 12.323 seconds for status and 22.483 seconds for Pause. Intervals remain wide (9.009–43.529 and 7.580–57.887); prior sequential cohorts do not establish a causal performance improvement. All 97 installation configuration fields parse identically under accepted-baseline/current code for manual, Telegram and cron triggers. Full native prompt/tool/workspace execution parity, production environment overrides and remaining user acceptance are still open. See `bench/runtime/uat-chat-cohort-factual-report-summary.json`.

Shared HTTP and tool-handler errors preserve unknown write outcomes instead of advertising them as retryable. Subsequent changes add durable native effect admission, terminal-run cleanup, automatic CRM note/task readback, and scoped chat receipts. Goal reads/listing and the workspace expose pending evidence, and goal decisions cannot clear unresolved family actions. These supported paths are tested; automatic reconciliation for other business tools remains incomplete. Historical entries below retain the earlier state and failures.

## Alternative runtimes and rollout

| Requirement | Current evidence | Finding |
|---|---|---|
| Bounded Pydantic AI / Deep Agents integrations through public APIs | `bench/runtime/candidates.py`, isolated pins and action screening | Action adapters now have a shared request/result/control bridge tested with an explicit host; run/control-store host tested with real migrations; ordinary manifest/tool/memory admission and checkpoint resumption remain incomplete; goal-controller lifecycle accounting is now exercised end to end |
| OpenCode assessment without a permanent fork | `bench/runtime/opencode/result.json` | Public SDK serialization/abort transport tested; server enforcement, budget admission and recovery remain unverified |
| Candidate tools through native admission and dispatch | `test_native_dispatch.py`, prepared `NativeGateway`, private run/step/receipt tables | Both frameworks exercise native gates and durable step recording; duplicate proposals stop after verified success; uncertain step acknowledgement stops queued work. Native manifest/memory bootstrap, interactive approval lifecycle and general external-write reconciliation remain incomplete |
| Pinned dependencies and one upgrade per finalist | Candidate lock, `upgrade-before.json`, `upgrade-after.json` | Public action/API upgrade drill completed; checkpoint upgrade parity is not proven |
| Identical model/prompt/tool/resource comparison | Comparator refuses mismatched/unknown configuration | Existing live native/candidate prompts differ. Those cohorts cannot establish comparative superiority |
| Correctness, ≥20% overall p95 improvement, no critical scenario >10% slower | Executable comparator gates; `qualification-decision.json` | No behavior-qualified finalists in this revision. A future finalist must still pass 100 matched repetitions and the unchanged latency gates before selection |
| Lowest qualifying p95, uncertainty/integration burden tie-break | Bootstrap measurement support | No qualifying finalists to rank; retain current runtime |
| New-session rollout, active checkpoint compatibility, rollback without replay | Native compatibility/stop-preservation contracts and documented admission rollback | Private canonical fresh/populated schema upgrades and real daemon restart drills preserve stopped work, paused goals, checkpoints and uncertain operations. Synthetic full-daemon active-goal recovery and native saved-message continuation pass. Local current/prior/current application-code rollback preserves stopped/paused/unresolved work and passes new native admissions with identical dependencies. HTTP admission in the booted rollback daemon and container/dependency rollback remain unverified. No production promotion or rollback was performed or authorized |
| Monthly compatibility / prompt security updates | `.github/workflows/runtime-contracts.yml`, including canonical browser/native saved-plan recovery after app build | Monthly/PR synthetic checks configured; the matching combined command passes locally, but hosted CI and operational update review remain unverified |

## Next acceptance work

The user requested normal-chat acceptance review instead of the goal workspace preview.
`uat-chat-goal-review.json` records the tested two-turn status/pause interaction through
the native chat route and real goal store with a scripted model. The user accepted this
behavior on 2026-09-20: “Yes, that matches what I expect.” This is acceptance of the
unfinished-work explanation and pause behavior, not the entire modernization or deployment.
Two separately recorded configured-primary live diagnostics now exercise that interaction;
the first exceeded its intended deadline and exposed a dropped host deadline in chat
admission. The fixed diagnostic completed in 15.888s and 22.619s; two turns are not a
latency cohort or evidence of complete production-profile parity.

The user rejected a generic reconnect response that asks them to check whether Robothor acted. Acceptance must instead exercise Robothor checking its own durable records and the destination, reporting confirmed outcomes, and avoiding duplicate actions. An attempted dispatch alone does not establish an external result, but uncertainty is a fallback after reconciliation, not the default response to a dropped connection. The verified note/task recovery checks implement this direction; they are not manual acceptance of the entire change.

The user-facing case is ordinary task work: show what remains unfinished,
why it is waiting or blocked, what evidence supports completion, and what Pause/Stop did.
It must not depend on the user recognizing synthetic customer names or knowing the
internal task-versus-goal distinction. Their feedback on the running installation does
not by itself accept the isolated modernization build.

For this tested revision, both bounded candidate integrations fail the required
checkpoint-resumption capability and are not finalists. `qualification-decision.json`
records the refusal checks with each adapter's own checkpoint envelope. OpenCode
has not established the server-side controls needed to advance. Retain the improved
current engine under the approved fallback rule. This is not a conclusion about
upstream frameworks' ultimate capabilities. Any future candidate iteration must
pass the same behavior contracts before matched finalist performance testing;
unmatched smoke repetitions cannot qualify a replacement. The local MiMo deadline failure is preserved as an incomplete
action, with zero writes and no false completion; it is not edited out of the cohort.

Deployment review remains a separate, explicitly excluded action. Lack of deployment
does not authorize deploying as a way to obtain acceptance evidence.

Goal read tools now expose tenant execution enablement and explicit waiting-goal wake alternatives. Private tests cover disabled execution and linked-task wakes. Further live diagnostics preserve correct pause/task state, but exceed the 30-second reference and include imprecise wake language and a fixture-health inference. These are unresolved conversational/performance acceptance limits, not proof of full acceptance. The chat fixture now marks installation health unavailable rather than treating private run rows as production health.

A follow-up native recovery audit reproduced checkpoint reload fallthrough after successful admission. The lifecycle now fails closed on unavailable or malformed saved conversations and scopes the reload to the session tenant. An actual runner check verifies failed recovery without model or business calls. This improves native recovery safety; candidate checkpoint continuation remains incomplete.

The user rejected the proposed “check their status before trying again” response after lost chat delivery. It is superseded by automatic recovery of the original request's durable run record, with authenticated tenant/principal/session scoping and no action replay. Private-database, API/client and rebuilt-browser checks pass. This addresses lost result delivery; full-page reload persistence and generic external-provider receipt reconciliation remain separate gaps. The original status/pause chat acceptance remains accepted.

Chat audit recovery now also reports durable calendar-operation receipts referenced by direct calendar tool steps, keeping run failure separate from verified recorded action success. Contrary or unmatched operation evidence prevents successful run prose from being treated as verified completion. Deferred tool references, other providers, and fresh provider reconciliation are not established by this receipt projection.


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


Stale agent/workflow cleanup tenant isolation: reproduced a foreign-tenant mutation in the private real-daemon restart drill, then scoped selection and updates to the daemon tenant at startup and in the watchdog. Two restart cycles preserve foreign records while cleaning local orphans; 53 focused checks and 13 real-DAL integration tests pass. See `bench/runtime/uat-cleanup-tenant-isolation.json`. Other maintenance paths and active-goal continuation remain unqualified.


## Cleanup preserves locks for skipped work

Two regression tests reproduced cleanup releasing an agent lock despite skipping its healthy run, and releasing/counting a run whose conditional update affected zero rows. Cleanup now collects only successfully updated rows and releases their locks after commit; counts and logs reflect those rows. All 56 focused reaper/workflow/resume/size tests pass (4.93s), and the isolated canonical migration/two-restart drill passes with 13 integration tests (0.28s). The initial two failing tests are recorded in `bench/runtime/uat-reaper-lock-preservation.json`. Ruff and diff checks pass. This does not establish replacement-run lock ownership or full HA concurrency correctness; full acceptance remains incomplete.


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


## Freeze model settings within each chat cohort

A deterministic collector test changed the source manifest after the first child. Before the fix, all 29 subsequent children read the changed primary despite the journal retaining the original model label. The collector now writes primary/fallback/temperature settings into the new output directory before any child runs and points every child at that snapshot. Eight cohort/reporting checks pass (0.42s), with Ruff and formatting passing. Evidence: `bench/runtime/uat-chat-model-snapshot.json`.

The current UTF8 task-summary cohort was started at `fc5b658fdec` and already uses a manually frozen selected-model file. Its runtime remains `2488b5322db`; this collector-only edit does not modify those running conversations. Full acceptance remains open.


## Prototype a factual goal-progress report

The live task-summary experiment still permits contradictory prose about disabled reviews. `robothor/goals/presentation.py` now prototypes rendering a concise report directly from a complete trusted goal-store snapshot. It distinguishes task status from goal completion, preserves unfinished child work and pending verification, bounds previews without hiding omissions, and never presents a disabled or paused review as available. Missing task/child reads fail rather than being treated as empty.

Six private-store behavior tests plus six size checks pass (3.14s); Ruff, formatting and diff checks pass. Evidence: `bench/runtime/uat-goal-presentation-prototype.json`. The renderer does not choose a goal, authorize work or finish/deliver an agent run. It is not connected to chat and is not an accepted replacement for the live reply. Integration must preserve explicit goal selection, mixed-request behavior, controls, accounting and terminal delivery before any chat or latency improvement is claimed. The current UTF8 cohort remains on product `2488b5322db`. Nothing was deployed.


## Task-summary live screen finished: acceptance failed

The corrected UTF8 cohort finished all 30 conversations at harness `fc5b658fdec`, product `2488b5322db`: 60 completed turns, 30/30 correct final goal/task states, no missing or unresolved samples. The collector exited **1**, correctly failing the latency screen. Status p95 is **32.657s** (bootstrap 95% interval **24.287–33.329s**); Pause p95 is **35.423s** (interval **21.296–49.293s**). Both exceed the 30s target. This does not support a no-regression finding against the preceding observed 19.563s/27.825s cohort; sequential provider observations are not a causal attribution to the change.

All 30 status replies were inspected. Examples 3, 8, 10 and 24 still offer leaving work to a scheduled review while pursuit is disabled. Other replies imply a task change can overcome that disabled setting or offer enablement outside the fixture's tools. These examples are not an exhaustive semantic failure count. The original user-accepted scripted status/pause behavior remains the only manual acceptance.

All raw transcripts, logs, the durable journal, machine screen and frozen selected-model settings are retained in `bench/runtime/uat-chat-cohort-task-summary-utf8/`; augmented measurements and limits are in `uat-chat-cohort-task-summary-utf8-summary.json`. The factual renderer and report-channel prototypes were not active in these runs. No deployment, model selection change or full acceptance is claimed. Next work is native integration of the factual report without dropping mixed requests, bypassing permissions or continuing model work after a verified final report.


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


## Automatic CRM note readback

Following `483c11e3729`, runtime-bound native `create_note` uses the host-reserved effect UUID as its CRM note ID. The handler accepts this identity only from the active dispatch context after matching the tenant, owning run, tool name and argument fingerprint. Existing DAL callers retain randomly generated IDs; no model-facing ID parameter was added. A failed creation response is classified as uncertain.

The dispatcher now attempts trusted positive readback for uncertain note creation. The daemon recovery sweep performs the same readback later. Both query the exact tenant and reserved note ID and exclude deleted notes. Positive readback stores a confirmed receipt; repeating the same request returns that receipt without another create. Absence, deletion, foreign ownership, or storage errors never establish nonapplication. Unresolved background checks rotate so an old missing note does not indefinitely exclude later records from the bounded batch.

Canonical tests invoke the real native dispatcher, CRM handler, DAL insertion and readback on a disposable migrated database, inject response loss after commit, and verify immediate and deferred recovery each leave exactly one note. No model or live business provider is used. This does not exercise a full chat conversation. The canonical selection passed 34 with one skipped in 10.37 seconds. The focused engine/CRM/recovery/verification/size selection passed 300 in 12.23 seconds. Logs are indexed in `bench/runtime/uat-verification.json`.

This closes automatic readback for CRM note creation through this path. It does not qualify other providers, expose generic recovery in the chat outcome endpoint, fix goal bookkeeping during uncertainty, or establish overall user acceptance. The prior broad engine result predates these changes. Nothing was deployed.


## Chat reconnect reads runtime-effect receipts

Following `507d7836620`, `/chat/outcome` includes native effect receipts from the authenticated run, delegated children and continuations. Both the run family and each effect must match the tenant and principal. Unrelated, cross-tenant and different-principal effects are excluded. This endpoint reads stored evidence only; it dispatches no tools and makes no model calls.

Confirmed note creation is presented as a verified action even when a later failure left the run failed. The run is not upgraded to completed or verified. Unresolved child actions suppress an otherwise misleading parent success message and keep reconciliation polling active. Existing calendar receipts remain supported.

The focused chat/calendar/size selection passed 84 tests in 9.41 seconds. Canonical integration passed 34 with one skipped in 8.50 seconds, including real CRM creation, injected post-commit response loss, trusted recovery and a subsequent chat outcome read. Existing frontend recovery component tests passed seven tests. The broader engine selection was started as session 90842, logging to `/tmp/runtime-engine-audited-recovery-regression.log`; its result is pending and must be collected before claiming a new broad pass.

This is reconnect behavior, not a live conversational user acceptance result. Other provider-specific recovery and goal bookkeeping while effects are unresolved remain open. No deployment or overall acceptance is claimed.


## Audited recovery regression and safe goal bookkeeping

At `0323716b21a`, the broad engine selection completed with **11,229 passed, 29 skipped, 192 deselected, 394 warnings in 384.02 seconds**. Product source and collected tests remained unchanged while it ran. This includes the effect journal, terminal-owner recovery, CRM note readback and chat receipt projection. It supersedes the pending-run entry above. Warnings include deprecations and unawaited network coroutines; this is not warning-free or performance evidence.

Five new regression cases reproduced recovery admission denying goal bookkeeping. The subsequent change permits progress, wait, block, pause and cancel through the effect fence and goal recovery admission, while retaining existing role, lease, stop, version and goal-state checks. These actions do not clear `recovery_required`, settle an uncertain effect, authorize another business write or permit completion. Ten private-database cases cover both interrupted-run recovery and a live goal with an uncertain effect. Wait/block/pause/cancel yield the coordinator.

The focused goal/tool/dispatch/size selection passed 54 in 6.75 seconds. Canonical integration passed 36 with one skipped in 13.64 seconds. The native restart/crash test now covers two supported paths: an immediate safe wait preserving recovery_required (one model call), and rejection of premature completion followed by explicit reconciliation and waiting (three calls). Neither path runs another model turn after yielding, and interrupted-attempt token accounting remains intact. Prior canonical assertions that an early wait must fail were updated; an intermediate fixture still expected the old action sequence and failed before being corrected. All intermediate logs remain indexed in `bench/runtime/uat-verification.json`.

The broad pass predates this bookkeeping change, which has focused and canonical coverage. Generic uncertainty projection into the goal workspace, other provider verifiers and normal-chat UAT remain open. No production deployment or overall acceptance is claimed.


## Goal-family action evidence in factual reports and decisions

Following `4b797845bdd`, full goal snapshots include pending and confirmed runtime-effect counts for the goal and its descendants, scoped to the tenant. The factual chat renderer reports pending action verification separately from task completion and goal recovery flags. If an existing stored goal says complete while its effects are unresolved, the report qualifies that recorded status rather than presenting verified completion.

The store now refuses complete, approve, assess and reconciled commands while earlier family effects remain prepared, dispatching or uncertain. The current update command's own effect admission is excluded from that check. Goal-bound effect admission takes the same tenant goal transaction lock, so it cannot insert a new action between the decision's evidence read and commit. Waiting, progress and controls retain the previous safe behavior.

Tests cover child-to-parent projection, positive resolution without automatic goal completion, tenant/unrelated-goal isolation, refusal of all four decision commands, and admission blocked by an in-progress goal decision transaction. The focused goals/runtime/size selection passed 142 tests in 21.50 seconds; canonical integration passed 36 with one skipped in 9.59 seconds. Seven new cases initially failed because their fixtures omitted mandatory attempt identity; that setup failure is retained alongside the corrected result. Local reduced-schema goal fixtures and the isolated UAT server now apply migration 141. No shared database was migrated.

This exposes evidence in full goal reads and factual chat reports. It does not add list-summary counts or a dedicated frontend display, qualify further provider readbacks, or constitute live conversational UAT. The latest broad engine pass predates this change. Overall acceptance and deployment remain false.


## Goal detail view shows action evidence

Following `d28cf89572a`, the goal workspace detail panel displays pending and confirmed action counts across the goal family. Verified actions do not imply goal completion. Pending evidence disables completion approval and qualifies an existing recorded complete status. Recovery text allows safe progress/waiting and no longer directs the operator to retry an uncertain action. Existing five-second detail polling accepts updated receipts at the same goal version.

The full frontend selection passed **150 files / 1,665 tests in 9.03 seconds**. A final test-fixture lint cleanup was followed by eight passing goal-view tests (0.787 seconds) and strict eslint with zero warnings. Product source was unchanged between the full and final focused runs. Existing jsdom navigation notices remain; this is not a new real-browser run.

A first command invoked pnpm from the wrong directory and failed its version check without running tests. The corrected app-directory invocation used the configured version. One new assertion incorrectly assumed reads passed no options; GET calls include headers. The corrected test checks the method and rejects writes during polling. The initial full run's one failing assertion and all corrected results are retained in the evidence index.

This completes the detail-panel presentation for the existing action-count API. List-summary projection, additional provider readback and normal-chat manual acceptance remain open. No deployment or overall acceptance is claimed.


## Current-model chat diagnostic and authenticated note reconnect

At product revision `8101cc1b371`, the existing status/pause scenario passed once through native chat with the user's selected model chain (primary DeepSeek v4.1 Flash through OpenRouter). Status elapsed time was 6.160 seconds; pause 5.705 seconds. Five provider calls produced both factual reports, with estimated cost $0.0021, not billing-reconciled. The synthetic goal became paused and its tasks stayed DONE/TODO. No production business tools or goals were used. This is a current diagnostic, not another qualifying cohort or new manual acceptance.

The canonical CRM-note recovery test now also delivers its saved evidence through the actual authenticated `/chat/outcome` route twice. For both immediate and deferred readback, only one real isolated CRM note is created, reconnect makes zero model calls, and both HTTP responses match the stored outcome. The run retains failed status while the action receipt is verified; no whole-request completion is invented. The concrete responses are archived as `uat-note-chat-immediate.json` and `uat-note-chat-deferred.json`.

The first HTTP fixture omitted authenticated request state and got 503 before entering the route. It was corrected to supply fixture authentication through middleware while retaining the route dependency. Final canonical integration passed 36 with one skipped in 12.25 seconds. The failure log and final evidence are retained. No product source changed in this follow-up; only verification coverage and artifacts changed. Overall acceptance and deployment remain false.


## Goal list action evidence

Following `56aa76e6aa0`, goal discovery attaches current pending/confirmed action counts in a single aggregate query for the bounded goal list and its descendants. Full detail reads and list reads share this query. The list shows pending action verification without requiring selection and qualifies a stored complete status when effects remain unsettled. Counts update after readback without changing the goal version. Existing task summaries remain separate.

Focused goal/effect/bookkeeping/size tests passed 129 in 17.43 seconds; canonical integration passed 36 with one skipped in 11.11 seconds. Nine goal-view tests passed, strict eslint and Ruff passed, and the full frontend selection passed 150 files / 1,666 tests in 9.07 seconds. A missing JSX brace initially prevented UI test collection; its log is retained with the corrected results. Existing jsdom navigation notices remain.

This closes the list-summary presentation gap for recorded runtime effects. It is not a new broad engine or real-browser result, and does not qualify additional provider readbacks. The previous normal-chat recovery question has no user response yet and remains unaccepted. Overall acceptance and deployment remain false.


## Accumulated-history lookup screening

At `733a77122d4`, a disposable PostgreSQL probe with 20 goals and 100,000 effect records (99,980 finished; 20 uncertain) showed that the goal-count lookup joined all 100,000 rows. Across 30 repetitions, median was 56.937 ms and p95 58.478 ms. The query now excludes irrelevant states before joining, and additive migration 142 supplies a matching partial index. In a fresh equivalent synthetic fixture, the planner used an index-only scan over 20 rows. Across 30 repetitions, median was 1.809 ms and p95 2.673 ms. Every sample returned identical expected counts.

A deterministic 2,000-resample bootstrap gives conditional 95% p95 intervals of 58.240–58.551 ms before and 2.506–2.677 ms after. These are sequential local SQL measurements, not end-to-end agent speed, framework selection evidence or measured mixed-tenant operating limits. Raw samples and query plans are retained in `uat-runtime-goal-history-before.json` and `uat-runtime-goal-history-after.json`; the summary is `uat-runtime-goal-history-comparison.json`.

The focused goal/effect/history/size selection passed 120 tests in 18.29 seconds, canonical integration and populated upgrade passed 36 with one skipped in 12.35 seconds, and migration packaging passed eight. Migration 142 is registered in the canonical manifest and isolated fixture/upgrade harnesses; production was not migrated. The synthetic history contract is included in compatibility CI. Overall acceptance remains false and the previous normal-chat recovery question still awaits a user response.


## Broad regression and fresh-build browser verification

At `f8d9fdf9bca`, the broad engine selection passed **11,247 tests, 29 skipped, 194 deselected, 393 warnings in 390.30 seconds**. Source and collected tests remained unchanged throughout the run. This incorporates safe goal bookkeeping, family effect evidence in decisions and reporting, list aggregation, and the indexed history lookup. It excludes slow/integration/LLM/e2e/smoke markers; warning counts and the complete output are retained.

A fresh `pnpm build` passed. The private canonical harness with `--chat-browser` then passed **42 tests, one skipped, one warning in 30.83 seconds**. Its six real-browser cases cover native saved-plan recovery, repeated approvals and early stops. It includes the default canonical native tests and the fresh/populated migration checks. It does not newly drive goal action indicators in a browser; those have component coverage and the current full frontend suite has 1,666 passing tests.

The audit's opening matrix was refreshed to remove superseded claims that generic action fencing, automatic note recovery and current regression evidence were missing. Historical entries remain for traceability. Other provider readbacks, application rollback qualification and remaining acceptance gates are still open; the normal-chat recovery example has not received user acceptance. No production deployment occurred.


## Task creation acknowledgement recovery

Following `a9428d59d77`, a canonical regression test wrapped the real private CRM connection so `commit()` committed and then raised a synthetic lost-acknowledgement error. The DAL returned no task ID, the handler produced a generic error, and a repeated identical request created a second task. The failed case is retained in `uat-runtime-task-ack-before.log` (one failure, 36 other passes, one skipped).

New runtime-bound native task creations now use the host-reserved effect UUID as their task ID. Missing creation responses are marked uncertain/nonretryable, and the durable fence blocks another insert. A trusted task verifier checks that exact ID and tenant, excluding deleted tasks. Both immediate readback and the daemon recovery sweep can confirm task creation and return the recovered result. Missing/deleted/foreign tasks remain unresolved. A separate handler bug is fixed: a DAL validation-error dictionary is returned as an error rather than placed inside a purported task ID.

Canonical integration passed 38 with one skipped in 15.12 seconds. Its immediate and deferred task cases each leave exactly one task after two identical dispatch attempts. The focused task/CRM/effect/size selection passed 220 in 10.50 seconds. Existing task attribution, human-review forwarding and CRM behavior are included in that selection. The new focused contract is registered in compatibility CI; Ruff and diff checks passed.

This proves creation, not task completion or Redis event/notification delivery. Existing dedup paths may reuse an older task ID instead of the reserved one; recovery of a lost return from those paths is not established by this verifier. They retain uncertainty protection. The prior broad engine result predates these changes. No live-chat task UAT, overall acceptance or deployment is claimed.


## Existing-task deduplication receipts

Following `8034c6e3476`, the thread-key handler path records the existing task selected by its trusted read before returning. The goal-linked DAL path records its selected task in the same database transaction as that lookup. Both bind only the active `create_task` effect and an undeleted same-tenant task. A conflicting existing target or stale/non-dispatching owner cannot overwrite the binding. This metadata is internal and not a model-facing argument.

Task readback now uses that saved target when present, otherwise the reserved new-task ID. Positive recovery returns the existing task and `deduplicated: true`; it does not claim a new task was created. Canonical tests use actual handler and DAL deduplication for both paths, inject response loss after the result, and recover the original ID while leaving exactly one task. They passed as part of 40 canonical tests (one skipped, 12.67 seconds). The focused engine/CRM/goals/size selection passed 329 with two warnings in 24.69 seconds. Foreign/deleted targets, attempted rebinding and stale-owner mutation are covered. Ruff and diff checks passed.

This closes the previously unqualified lost-return case after a deduplication target has been durably recorded. A crash before that binding is durable can still leave an unresolved admission; the code does not replay an action to infer its outcome. Task completion and downstream notification delivery remain separate facts. No new broad engine, browser, live-chat manual acceptance or deployment is claimed.


## Verified success receipts survive replacement workers

Following `b1c5834779e`, canonical tests found that a successful `create_note` or `create_task` could be repeated by a different worker in the same request: the prior action was recorded as finished but had no confirmed result. Both red cases created two records and are retained in `uat-runtime-success-receipt-before.log`.

Trusted native CRM creation/dedup handlers now opt into positive readback for ordinary success as well as uncertain responses. The effect is kept unresolved until readback confirms it; the confirmed receipt is saved before the result is returned. Another worker in the same request receives that receipt without dispatching the handler. A distinct authorized request can still deliberately create identical content. Synthetic or adapter tools sharing a name do not acquire this verification capability automatically. Successful original result metadata is retained when adding the verified receipt.

The focused dispatch/CRM-recovery/tools/effects/size selection passed 102 tests in 7.77 seconds. Canonical integration passed 42 with one skipped in 11.94 seconds, including successful note/task replay and distinct-request creation. A negative-readback case confirms that a handler success claim alone cannot clear uncertainty or permit a repeat. Ruff and diff checks passed.

These cases use actual private CRM storage and different worker IDs, not an OS-level kill after successful return. Other providers still need their own readback semantics. The prior broad engine/browser results predate this change, and no manual acceptance or production deployment is claimed.


## Saved success after actual worker exit

At product revision `b1671409892`, the canonical private integration suite passed **44 tests, two skipped, one warning in 25.94 seconds**. New note and task cases each launch a separate process that performs the real native CRM dispatch, saves its verified receipt, then exits immediately with code 76 before reply delivery. A fresh replacement process recovers the same result ID. Its write handler is configured to fail if invoked; each case leaves exactly one CRM row and makes zero model calls. The log is `bench/runtime/uat-runtime-success-receipt-process-crash.log`.

This extends the earlier different-worker-ID evidence to actual OS process exits. It covers the native dispatcher and private CRM, not the complete AgentRunner or daemon process lifecycle. Audit publishing and event publishing are stubbed; the durable runtime receipt and CRM row are real. Two skips are harness-only worker cases. No product source changed. Other provider outcomes, broader regression qualification, manual acceptance and production deployment remain separate.


## Broad regression after CRM recovery changes

At `9b2ae888de3`, the broad engine selection passed **11,257 tests, 29 skipped, 203 deselected, 393 warnings in 386.87 seconds**. Product source and collected tests remained unchanged during the run. This result includes task creation and existing-task deduplication recovery, verified success receipts and their dispatch controls. Separately, the CRM and goal suites passed **238 tests, one deselected, two warnings in 14.18 seconds**. Both invocations excluded slow/integration/LLM/e2e/smoke markers. Full logs are retained in `bench/runtime/uat-runtime-engine-crm-receipts-regression.log` and `bench/runtime/uat-runtime-crm-goals-receipts-regression.log`; prior evidence remains available. Canonical integration separately passed 44 tests as recorded above.

No regression was detected in these scopes. This does not establish universal absence of regressions, full performance qualification or manual acceptance. The runtime compatibility workflow now also triggers on `robothor/crm/**` changes and runs the CRM regression selection, so standalone DAL edits cannot silently miss that gate. YAML parsing and diff checks passed; hosted CI was not executed. No production deployment occurred.


## Task-specific recovery in normal chat

Following `f9b4e684ac4`, two failing chat receipt tests established that verified task outcomes appeared only as a generic recorded action. Chat now uses trusted saved verification metadata to distinguish “The task was created” from “The existing task was found.” Both explain that the stored result was recovered without creating another task. A verified creation does not mark the task's work or a failed overall run complete. Unverified metadata cannot activate the deduplication claim.

The focused chat/readback selection passed **68 tests in 5.15 seconds**. Canonical integration passed **44 tests, two skipped, one warning in 21.02 seconds**. The real task acknowledgement-loss cases now read the chat recovery outcome twice after immediate/deferred readback and retain exactly one creation. The creation/dedup wording distinction is separately tested through the private chat store with saved receipts; this is not another live-model or browser run. Red and green logs are retained as `bench/runtime/uat-runtime-task-chat-{before,focused,canonical}.log`. Ruff and diff checks passed. The prior broad engine result predates this small presentation change. No manual acceptance or deployment is claimed.


## Local application-code rollback drill

At product revision `40b294adce7`, `.venv/bin/python -m bench.runtime.migrated_integration --application-rollback` boots the current daemon, prior compatible native revision `b1671409892`, then the current daemon again against the same canonically migrated private PostgreSQL data. The prior code is extracted into an isolated checkout; daemon source origins are asserted. Dependency declarations/lockfile, where present, must match and all phases use the same Python environment. Each daemon uses private Redis and refuses external network/provider connections.

All three phases became healthy, ran startup recovery and shut down cleanly. Explicitly stopped runs and their checkpoints, paused goals and descendants, unresolved calendar operations, and an unresolved generic effect remained preserved. Foreign-tenant work was not selected. Each revision separately ran its real native new-admission contract with a scripted provider in a fresh process: all three probes passed. These probes use the same private database but do not send an HTTP request to the booted daemon. The final canonical suite passed **44 tests, two skipped, one warning in 19.52 seconds**. An initial boot-only run also passed and is retained.

Artifacts: `bench/runtime/uat-runtime-application-rollback.json`, `uat-runtime-application-rollback.log`, and `uat-runtime-application-rollback-boot.log`. Reproduce from a clone containing the pinned target commit; the harness refuses a target outside current history. This establishes local compatible application-code rollback/recovery with preserved records, not container/dependency rollback, a production route switch, active-session migration, or deployment approval. The accepted integration baseline is not selected as a rollback target because it predates required durable controls. No product source changed in this follow-up.


## Full-daemon safe waiting after crash

At product revision `8e63c53ed3e`, the full-daemon crash drill exposed a stale assertion: it expected a `wait` call to be refused until reconciliation and therefore expected three recovery model calls and a cleared recovery flag. Safe bookkeeping now permits waiting without clearing that flag, as previously tested through the native runner. The initial full-daemon failure is retained in `bench/runtime/uat-runtime-daemon-goal-before.log`.

The drill now checks both actual SIGKILL/restart paths. Safe waiting performs one recovery model call, releases its lease and preserves `recovery_required`. Explicit reconciliation first proposes premature completion (denied), then reconciles the saved progress and waits, using three calls and clearing the flag. Both preserve exactly one progress action, interrupted-attempt accounting, and zero implicit run-resume attempts. After each goal waits, three completed coordinator ticks are observed with zero additional model calls. This is measured controller idleness, not a claim based only on elapsed time.

The final combined command `.venv/bin/python -m bench.runtime.migrated_integration --goal-crash` passed both daemon scenarios and **44 canonical tests, two skipped, one warning in 21.67 seconds**. Report and log: `bench/runtime/uat-runtime-daemon-goal-idle.json` and `.log`; the intermediate two-scenario run is also retained. Ruff and diff checks passed; import ordering alone was corrected after the final run. All data, Redis and provider responses were isolated/synthetic. Product behavior was unchanged; the prior rollback helper changes also work with goal-provider instrumentation. Manual acceptance and deployment remain unclaimed.


## Task recovery through the actual browser

At product revision `d694f5296c0`, a new Chromium case submits an ordinary task request through the real chat UI and Next proxy into the native runner. The scripted model calls the actual admitted `create_task` handler and private CRM DAL, then returns its reply. The browser transport deliberately discards that successful response. Chat recovers through `/chat/outcome` using the original request ID and displays the saved verified task receipt without sending another action.

The check confirms one browser submission, one native run, one CRM task and one confirmed effect receipt. There are two scripted model calls for execution and zero additional calls for reconnection. Polling stops after the receipt arrives. The displayed text is archived in `bench/runtime/uat-runtime-task-browser.json`; it states that the task was created and its stored result recovered without creating another task. Creation does not imply the task's work is complete. Unrelated dashboard responses, audit publication and event publication are fixtures; neither task notifications nor live-provider quality is certified here.

A fresh `pnpm build` passed. The canonical suite with `--chat-browser` passed **51 tests, two skipped, one warning in 46.55 seconds**, including the six prior approval/stop browser cases and this new task recovery case. The new case is included in the existing compatibility workflow via the canonical browser selection. Logs are `bench/runtime/uat-runtime-task-browser-build.log`, `uat-runtime-task-browser-canonical.log`, and `uat-runtime-task-browser.log`. Ruff, JavaScript syntax and diff checks passed. No product source changed. This is automated end-to-end verification, not manual user acceptance or deployment.


## Existing installation configuration compatibility

The isolated worktree intentionally has no installation-owned `docs/agents/main.yaml`; those local manifests are ignored by Git. The running workspace's main/default manifests were therefore read directly without modification. New read-only harness `bench.runtime.configuration_parity` parses those same files with both accepted integration `eea3252b1577e3f51e6d6a6216d6f242fd7ac4a1` and current `170bdd3140e2d8eb0c9b0d8a55417bd6c4455b79` code in separate processes. Source origins and unchanged input hashes are checked.

All **97 parsed fields** match for each of **manual, Telegram and cron** triggers. The selected DeepSeek v4.1 Flash primary and existing MiMo/DeepSeek v4 Flash/local Qwen fallback order, temperature 0.5, enabled task protocol, 104-tool configuration and existing unlimited timeout/iteration sentinels are preserved. Instructions and full config values are compared as hashes, not copied into artifacts. Report/log: `bench/runtime/uat-installation-configuration-parity.json` and `.log`.

Reproduce with `.venv/bin/python -m bench.runtime.configuration_parity --baseline /home/philip/robothor-runtime-baseline --workspace /home/philip/robothor --output /tmp/new-configuration-parity.json`; output creation is exclusive to preserve previous results. Both processes use the same scrubbed environment. This verifies installation file parsing compatibility, not production process environment overrides, prompt/memory bootstrap, full tool execution, provider latency or manual acceptance. It does not turn the earlier reduced-profile live cohorts into full-configuration performance evidence. No model, agent or business tool ran, and no installation configuration changed. Ruff and diff checks passed.


## Concurrent native receipt recovery

At product revision `24ebf661813`, new canonical note/task cases create a real private CRM record while temporarily withholding its readback verdict. Twenty independent readback threads then synchronize after observing that unresolved record and the actual CRM result, before any may commit its verdict. All receive the same verified result; exactly one compare-and-swap resolution commits (one version increment). Twenty concurrent same-request dispatch attempts afterward also return that saved result. Their underlying write handler is replaced with a failure assertion so an unexpected replay cannot pass unnoticed. Each case retains exactly one CRM row and one effect row.

Canonical integration passed **46 tests, two skipped, one warning in 23.82 seconds**. Evidence: `bench/runtime/uat-runtime-concurrent-receipt-recovery.log`. This verifies contested resolution and concurrent cached dispatch at the native/storage boundary, not twenty browser tabs, a twenty-tenant workload or a provider-latency cohort. Ruff and diff checks passed. No product changes were necessary; no additional manual acceptance or deployment is claimed.


## Native task dispatch overhead screening

At product revision `76c623c1e6e`, a new private canonical drill compared accepted baseline `eea3252b1577e3f51e6d6a6216d6f242fd7ac4a1` and current native `create_task` dispatch. Each source revision ran in a fresh process with the same dependency environment, private schema, argument structure and scoped permission fixture. Audit/event publication and models were excluded. One warmup and all 30 warm samples per revision are retained; every sample was independently checked against the stored task after timing, with exactly 31 task rows per revision. Current dispatch includes runtime admission, durable receipt persistence and positive readback; baseline lacks those added controls.

Baseline median/p95 were **1.879/3.837 ms**, versus current **11.814/16.240 ms**. The p95 increase is **12.403 ms** in this slice. A seeded 2,000-resample bootstrap gives conditional p95 intervals **3.182–4.085 ms** and **14.347–17.569 ms**, respectively. The increase is reported, not relabeled as a speed improvement. Sequential blocks do not isolate every environmental influence. These are dispatch/storage measurements, not complete harness overhead, user-visible agent latency or framework qualification. The 2-second harness and 30-second conversation targets cannot be certified by this slice.

Reproduce with `ROBOTHOR_RUNTIME_BASELINE_CHECKOUT=/home/philip/robothor-runtime-baseline .venv/bin/python -m bench.runtime.migrated_integration --crm-dispatch-screening`. Raw samples, warmups, worker output and conditional intervals are retained in `bench/runtime/uat-crm-dispatch-screening.json` and `.log`. The following canonical suite passed **46 tests, two skipped, one warning in 20.42 seconds**. Ruff and diff checks passed after equivalent lint-only import/function edits and adding revision metadata to future reports; product source was unchanged. No overall performance or manual acceptance is claimed.


## Readback optimization trial — reverted

After `882bfaa2422`, an experimental resolver reused the scoped journal snapshot and returned the successfully committed row, avoiding two success-path journal reads. Scope/version checks and the concurrent-loser reread remained. Focused effect/readback tests passed **41**, canonical integration passed **46 with two skipped and one warning (18.95 seconds)**, and six size checks passed. New tests refused a foreign tenant/principal snapshot before invoking verification.

The repeated 30-sample dispatch screening did not establish a latency improvement. Trial current median/p95 were **16.949/20.992 ms** (conditional p95 interval **20.068–22.024 ms**); baseline in that run was **1.403/3.104 ms**. Earlier unchanged-current p95 was **16.240 ms**. These sequential cohorts do not isolate causality, but fewer queries alone does not justify claiming a speedup. The experimental product/test edits were reverted exactly to HEAD. The retained implementation therefore remains the earlier verified one; no product optimization is claimed.

Preserved artifacts: `bench/runtime/uat-runtime-readback-trial.json`, `uat-runtime-readback-trial.patch`, and the focused/screening/size logs. The patch is an unapplied experiment, not runtime code. Further profiling or a controlled comparison is needed before selecting an optimization. Overall acceptance remains open; nothing was deployed.


## Authoritative core classification removes plugin discovery from writes

Following `76d3362818f`, optional dispatch profiling found that core task creation rediscovered plugin read-only declarations on every call: 30 scans, median **5.362 ms** in the initial profile. Core reads already bypass the effect journal through its static classification, and adapter/plugin registration reserves core handler names. Core writes now retain that authoritative classification without a dynamic plugin scan. Extension tools still use their runtime declarations; unknown extensions remain writes. The source change is confined to that classification branch in `effect_dispatch.invoke`.

A new failing test showed that an injected extension declaration naming core `create_note` could bypass the journal. It now leaves one fenced action instead of allowing repeat dispatch. Separate cases preserve dynamic read-only and write behavior for extension names. Focused dispatcher/classification/plugin/reserved-name/size checks passed **67 tests in 8.30 seconds**.

Separate timing blocks varied materially: instrumented current p95 changed from **22.290 to 8.160 ms**, while a later unprofiled current run measured **19.034 ms**. All raw runs are retained; the latter is not omitted to claim a cleaner speedup. A stronger seeded randomized comparison then interleaved **30 warm samples per implementation** in one worker with the same database, native handler and dependencies. It loaded the prior `effect_dispatch` module from `76d3362818f` and compared it with the edited module; other code was identical. Reference/current median were **21.585/15.225 ms** and p95 **24.004/18.956 ms**. Conditional 2,000-bootstrap p95 intervals were **23.642–26.326 ms** and **17.151–19.471 ms**. Every sample and both warmups verified creation; 62 tasks were stored. This supports removing the classification scan in this path, not an end-to-end latency or framework-selection claim.

The final canonical run passed **46 tests, two skipped, one warning in 21.08 seconds**. Reproduce profiling with `ROBOTHOR_RUNTIME_PROFILE_DISPATCH=1` on `--crm-dispatch-screening`, or the interleaved check with `.venv/bin/python -m bench.runtime.migrated_integration --crm-dispatch-ab` (requires the pinned reference commit). Inclusive stage times overlap and must not be summed. Evidence: `bench/runtime/uat-dispatch-classification.json` and its six referenced raw logs (`uat-dispatch-profile-{before,after}`, `uat-dispatch-classification-{red,focused,unprofiled,ab}`). Ruff and diff checks passed. Prior broad engine results predate this change; manual acceptance and deployment remain open.


## Broad regression after authoritative core classification

At `5ccfb199dcb`, the broad engine selection passed **11,262 tests, 29 skipped, 206 deselected, 393 warnings in 394.96 seconds**. Product source and collected tests stayed unchanged for the complete invocation. It excludes slow/integration/LLM/e2e/smoke markers. The separate private canonical suite with `--chat-browser` passed **53 tests, two skipped, one warning in 40.56 seconds**, including all seven actual-browser recovery/approval/stop cases. The prior successful frontend build was reused only after confirming app source, package lock and Next configuration were unchanged.

No introduced regression was detected in these selections. All warning/skip counts remain visible in `bench/runtime/uat-runtime-engine-classification-regression.log` and `uat-runtime-classification-browser-regression.log`. The opening audit matrix and current remaining-work list were refreshed to incorporate task browser recovery, configuration parsing parity, full-daemon idle evidence, compatible code rollback and current test counts. Superseded remaining-work text is preserved in `remaining_before_classification_audit`; historical evidence remains untouched.

These results do not qualify full representative-workload provider latency, production ingress/fallback limits, every simple-action classification, every external-provider readback, or user acceptance. The tested candidate rejection/retain-current decision is distinct from the requirements for a future qualifying replacement. The normal-chat audit-recovery question still awaits a human response; automatic goal continuations are not acceptance. No production deployment occurred.


## Full local chat-to-task harness screening

At product revision `ad4ab2ef976`, `.venv/bin/python -m bench.runtime.migrated_integration --task-chat-screening` adds a 30-warm-request screening case through authenticated ASGI `/chat/send`, the native runner, actual admitted task creation, private CRM storage, receipt verification and final response delivery. One initial warmup is retained separately. Each request uses its own session/request identity and must leave exactly one new task, one additional confirmed effect and one completed native run. Two scripted model calls occur per request; no external provider call is permitted. The test enforces p95 below 2 seconds for this case.

The final case passed all **30 warm requests plus the warmup**. Median was **36.350 ms**, p95 **48.759 ms**, with a seeded 2,000-bootstrap conditional p95 interval **43.453–58.200 ms**. The combined canonical invocation passed **47 tests, two skipped, one warning in 24.26 seconds**. `ROBOTHOR_RUNTIME_TASK_CHAT_SCREENING_ARTIFACT` writes an exclusive JSONL file and flushes each sample, including failure/timeout records; the summary is only produced after correctness checks pass.

Artifacts: `bench/runtime/uat-task-chat-screening.jsonl` and `.log`. Earlier passing runs before a resource-cleanup adjustment and before adding the explicit completed-run assertion are retained as `uat-task-chat-screening-initial` and `uat-task-chat-screening-resource-cleanup`. Their p95s were 48.677 and 55.348 ms; none was dropped in favor of a better number. The final value comes from the stronger final test. Ruff and diff checks passed.

This is a local harness result with an immediate scripted model and minimal agent profile, not the selected live-model cohort or full installation profile. It excludes TCP/Next/browser and provider latency, external audit/event delivery, and embeddings; independent post-response verification queries and background draining are outside the timer. Runtime receipt readback is inside it. It supports the local 2-second target for this scenario, not the 30-second live conversational target or complete acceptance. No product configuration/source changed and nothing was deployed.


## Known unfinished work keeps its originating task open

At base `9a03c15ed35`, an actual native worker with task protocol and todo lists enabled recorded one completed item and one pending item, then returned an accurate partial-progress response. Its completed turn nevertheless marked the default-tenant parent task DONE. The first failing invocation also exposed a test projection error in the isolated-tenant assertion; that assertion now reads actual task status and next_action directly from private SQL. Both failures are retained, not treated as two product defects.

Finalization now captures remaining checklist items and keeps the originating task open with the next action, independent of the optional model-verification mode. Repeated finalization without a session preserves that snapshot; a current completed checklist clears it. Legacy resolve/reopen calls now explicitly use the run tenant. Completed checklists retain existing completion behavior; this does not claim that a model checklist independently verifies all outcomes.

Focused policy, auto-task, verifier, escalation, promotion, checkpoint-todo and size checks passed **82 tests in 5.63 seconds**. The final disposable migration/native suite passed **50 tests, two skipped, one warning in 22.39 seconds**. Four actual worker cases cover pending versus completed checklists in the default and isolated tenants. All use a scripted provider and private CRM; no live models or production writes. Ruff passed. The earlier broad engine result predates this change.

Artifacts: `bench/runtime/uat-unfinished-task-{before,focused,after,focused-final,canonical-final}.log`. The intermediate focused failure was an old assertion omitting the now-explicit tenant; its outdated persistence mock was also corrected. Manual acceptance and overall acceptance remain open. The user's audit-log objection is recorded as a requirement to inspect and reconcile before reporting uncertainty, not acceptance of the earlier generic fallback wording. Nothing was deployed.


## Chat-triggered task and note reconciliation

The broad engine selection at `d249494d752` passed **11,272 tests, 29 skipped, 211 deselected, 393 warnings in 392.39 seconds**. Product source and collected tests remained unchanged for the full invocation. New readback tests were added separately after collection and are not included in that result. This verifies the unfinished-task change; it predates the following product changes.

Reviewing the user's audit-log requirement exposed a concrete delay: `/chat/outcome` triggered immediate calendar reconciliation, while uncertain CRM note/task writes depended on the stale-run cleanup cycle, scheduled every 20 minutes. The new HTTP tests reproduced pending outcomes after polling. The initial canonical fixture returned 503 because its authentication middleware was missing; after correcting the fixture, both real native CRM creation cases still failed because polling never performed readback. No provider credentials or production records were used.

The existing authenticated original-request callback now also invokes trusted task/note readback. A new host helper only reclassifies prepared/dispatched effects after checking the effect's own durable owner is terminal and matches tenant/principal. A terminal parent does not authorize revoking a running child's admission. Unsupported effects, missing records and foreign identities retain their existing fences. The helper never calls a model or business dispatcher; successful reads use the existing evidence verifier and compare-and-swap receipt update. Unrelated same-user requests are not swept by a chat poll.

Two additional failing cases drove corrections. An unavailable calendar provider used to abort the loop before an independent CRM record was checked; failures are now isolated per action. Also, definitively `not_applied` effects disappeared from chat's audit projection, allowing old successful completion prose and its verification flag to reappear. Those entries now remain visible, stop polling once settled, and refute that stale claim.

Final affected backend tests passed **115 in 15.15 seconds**. The private canonical migration/native suite passed **52 tests, two skipped, one warning in 18.35 seconds**. Its two new cases create an actual CRM task/note through the native dispatcher, inject lost acknowledgements and initially unavailable readback, then restore readback and recover through authenticated HTTP polling. Each verifies one stored record, one creation call, confirmed audit evidence and zero additional model/tool calls. The original failed run remains failed; a recovered action does not invent whole-request success. Chat recovery/streaming frontend tests passed **20 tests in two files in 1.36 seconds**. Ruff and diff checks passed.

All stages are retained under `bench/runtime/uat-runtime-chat-readback-*`: initial probe source/log, initial HTTP failures, canonical fixture failure, corrected canonical red result, intermediate passing runs, independent-provider and nonapplication failures, final focused/canonical results and frontend log. The broad engine artifact is `uat-runtime-unfinished-task-engine-regression.log`. Exact findings and counts are in `uat-verification.json`.

This establishes local chat-triggered reconciliation for supported CRM operations, not universal external-provider readback or loaded recovery-polling performance. The broad engine run predates these latest changes. No live model calls or deployment occurred. Manual and overall acceptance remain open; automatic continuation is not user acceptance.


## Verified retries preserve audit history without false incompletion

Following `d5019ab6b33`, a new private-database case found that retaining a definitive `not_applied` entry made a later verified retry of the same intent appear incomplete. The first run produced **two failures and 12 passes**: the completed request stayed unverified, and neither completed nor failed requests linked the earlier attempt to the positive evidence.

Chat projection now links an earlier non-applied attempt to a later valid confirmed receipt only when request identity and tool/argument fingerprint match inside the already authenticated tenant/principal/run family. It preserves both audit entries and their actual states. Different requests, arguments, lineage, principals, tenants and invalid evidence cannot satisfy the old intent. Matching runs never clear uncertain outcomes. Projection remains read-only; it does not initiate or authorize retries.

The intermediate focused run exposed another issue: a failed run with a stale `verified_status` tag could be returned as verified after all its action receipts were satisfied. Whole-run verification now additionally requires completed status. The intermediate **one failure/103 passes** is retained. Final focused audit/recovery/journal/size checks passed **104 tests in 13.50 seconds**. Canonical private migrations/native integrations passed **53 tests, two skipped, one warning in 23.17 seconds**.

A new native integration case proves definitive nonapplication for an undispatched failed worker, performs a separate permitted create_task attempt through the native dispatcher, verifies exactly one stored task, and checks that repeated chat audit reads retain both attempts while reporting the positive evidence. No model call, replay or production write occurs during those reads. Ruff and diff checks passed. Raw red/intermediate/final logs are `bench/runtime/uat-runtime-chat-retry-receipts-{red,focused,focused-final,canonical}.log`. Broad regression for this source is pending; manual and overall acceptance remain open.


## Broad regression after chat-triggered recovery and retry projection

At `e69866dfe64`, the broad engine selection passed **11,298 tests, 29 skipped, 214 deselected, 393 warnings in 396.54 seconds**. It excludes slow/integration/LLM/e2e/smoke markers. The separate private canonical suite with `--chat-browser` passed **60 tests, two skipped, one warning in 44.96 seconds**, including all seven existing real-browser cases. Source and collected tests remained unchanged throughout both invocations. The successful frontend build was reused only after verifying no changes to app source, package/lock or Next configuration from `170bdd3140e`.

No regression was detected in these scopes. Artifacts are `bench/runtime/uat-runtime-chat-recovery-{engine,browser}-regression.log`; skipped tests and existing warnings are retained. These runs include the unfinished-task fix, chat-triggered task/note readback, independent-provider failure handling, visible nonapplication and scoped verified retry projection. Earlier broad results remain recorded rather than overwritten without history.

Review of the multi-day acceptance deliverable found an evidence gap: `robothor/goals/tests/test_runtime_multiday.py` restores a day-old waiting state and completes child/parent through a simulated executor. Native mixed-workload tests and full-daemon crash/idle tests cover other portions separately. The combined multi-day hierarchy/ordinary-request scenario still needs the actual native runner on private storage; this is the next local verification target, not a claim that production goals are broken. It is now explicit in the remaining-work list. No live provider call, production write or deployment occurred. Manual and overall acceptance remain open.


## Combined native multi-day hierarchy and everyday requests

At base `c9bc4019f26` (product source unchanged from `e69866dfe64`), `test_native_multiday_goal.py` closes the combined native-execution gap identified above. The default canonical integration harness now includes this case. It runs the actual goal controller/current runtime/native runner with task protocol and automatic planning enabled, private canonically migrated storage, real native CRM task creation and a scripted planning/execution provider. The fixture pre-authorizes a parent and execution child; it does not create or resume a real goal.

The child registers tomorrow's reply condition. The parent takes one legitimate turn to acknowledge its waiting child, then also waits. Three completed idle coordinator ticks consume no provider calls. An everyday task request finishes while the first goal provider call is held, and another finishes while the hierarchy is waiting. The fixture restores a day-old wait timestamp and supplies a fresh synthetic provider receipt, replaces both runner/controller instances, and delivers the same reply event twice. A third everyday request completes while resumed goal work is held. The native goal tools record independently checked evidence and complete the child before the parent. A final tick performs no additional work.

Final evidence: **seven completed native runs**, **three created CRM tasks and three confirmed receipts**, **two attempts each for child and parent**, **four planning calls plus six goal execution calls**, and **1,500 goal-family tokens**. The six interactive calls are outside that family charge. The three diagnostic ordinary-request durations were **30.219, 35.879 and 36.020 ms**, each under the fixture's ten-second bound. These are three scripted-provider observations, not a latency screening cohort or live-model SLO qualification. No task/goal dispatch is mocked; event capture and external audit publication are isolated from production.

The final canonical invocation passed **54 tests, two skipped, one warning in 18.95 seconds**. Ruff and diff checks passed. No product fix was needed. Four earlier failed runs are retained: the first fixture disallowed the parent's legitimate dependency-review turn, causing its provider assertion to fail and retain conservative unknown-usage charge; later assertions omitted the engine's planning calls. The trace diagnosed those extra calls, and the final fixture answers planning requests with valid JSON and accounts for them explicitly. The first corrected run also passed all 54 cases; the final run adds persisted completed-run checks and the standalone artifact.

Artifacts: `bench/runtime/uat-runtime-native-multiday-{initial,parent-review,accounting,tool-trace,planning,final}.log` and `uat-runtime-native-multiday-final.json`. This is a simulated next day and fresh in-process runner/controller state, not an OS restart, real elapsed day, real calendar operation, live-model test or HTTP queue benchmark. Existing separate daemon crash/restart tests retain that scope. The combined native gap was removed from the active remaining list and preserved in `remaining_before_native_multiday`. Broad product-source regression remains the existing 11,298-pass result; this addition is integration-only. Manual and overall acceptance remain open. Nothing was deployed.


## Persisted reply survives lost browser storage and engine cache

At base `b618ec81e3a`, the native task browser fixture now covers both original-request receipt recovery and a fresh browser context. In the added case, the real native dispatcher creates one task and confirms its receipt in private canonical storage; the browser's response is deliberately dropped. The original context is closed. A **test-only** endpoint on the isolated fixture server waits until the assistant reply is persisted, clears the engine session cache and reinitializes chat with a new runner from that same private database. No product endpoint was added.

A fresh Chromium context receives no original request ID and has no chat request-journal entries. It restores the saved `Task creation recorded.` reply through normal chat history. After restoration the test waits 1.5 seconds and verifies there is no further send or audit polling. The backend independently checks exactly one task, one confirmed effect, one native run and the original two scripted model calls. An audit GET can occur in the old context before it closes; it is not a fresh-context dependency.

Final private canonical/browser verification passed **62 tests, two skipped, one warning in 48.92 seconds**. The initial passing run, before the additional idle assertion, passed the same count in 47.64 seconds and is retained. Both runs include the existing browser cases and the native multi-day scenario. Ruff, Node syntax and diff checks passed. App source/package/lock/Next configuration still match the previously built frontend, so that successful build was reused. Product source is unchanged from `e69866dfe64`; only test code and its Node driver changed.

Artifacts: `bench/runtime/uat-runtime-fresh-browser-{initial,final}.log` and corresponding directories containing each scenario's browser log and JSON result. This proves restoration after a **persisted completed reply** with fresh browser storage and a cleared in-process engine cache. It does not prove discovery after a worker died before persisting any reply when the original request ID is also lost, or a new OS process/full deployment restart. Those boundaries remain explicit in the active remaining-work list; the superseded wording is preserved. No live models, real business records or deployments were used. Manual and overall acceptance remain open.


## Saved handler responses prevent replacement-worker duplicates

At base `cb4835b797e`, a new regression reproduced a duplicate write for handlers without native readback: the journal retained `finished` but discarded the returned result, so replacement execution dispatched again. Both synthetic handler cases failed before the fix. Successful mapping responses are now stored atomically by the dispatched owner and recovered for the same tenant, principal, request and argument fingerprint. Recovery reports `verification=reported`; it does not manufacture independent proof. Native CRM task/note readback remains independently verified.

Tests cover 20 concurrent replacement calls with one original write, separately authorized new requests, response-persistence failure retaining the unresolved fence, and real worker exit before reply delivery followed by a separate recovery process. Chat includes saved-response receipts, overrides unsupported blanket completion claims, and does not mark them independently verified. Synthetic `send_email` tests use an in-process handler; no email is sent.

Evidence: original regression **two failed**; initial focused suite **44 passed**; expanded suite **69 passed**; canonical private migration suite **56 passed, two skipped, one warning in 29.19 seconds**; final targeted suite including chat projection **30 passed, nine skipped in 5.33 seconds**. The final targeted invocation lacks the canonical disposable DSN, so its nine native integration skips are explicit; the canonical run exercises those native receipt cases. Logs are `bench/runtime/uat-runtime-reported-result-{red,focused,expanded,canonical,final}.log`.

Scope: new successful mapping responses only. Historical `finished` records without saved responses cannot gain evidence retroactively. Error/nonmapping responses and unsupported external readback remain separate limitations. Audit persistence can itself fail after an external service acts; those attempts remain fenced for reconciliation rather than being blindly repeated. No model configuration, production records or deployment changed. Manual and overall acceptance remain open.

Broad engine verification on the final change passed **11,302 tests, 29 skipped, 218 deselected, 393 warnings in 389.55 seconds**. Selection excludes slow/integration/LLM/e2e/smoke markers; source and collected tests were frozen throughout the invocation. No regression was detected in this selection. The canonical suite above exercises separate native recovery cases. Log: `bench/runtime/uat-runtime-reported-result-engine.log`. Ruff formatting/lint and diff checks passed.


## Rollback qualification invalidated by new response receipts

At base `f0a6f652775`, source review found that the formerly qualified `b1671409892` rollback target only recovers independently confirmed effects. It does not understand newly retained `finished` tool responses. The application rollback drill now runs a behavioral preflight against current and archived target code in separate subprocesses with private canonical storage before any daemon boot. It seeds an acknowledged response and calls actual effect admission; no handler or model is invoked.

The current revision returned the original saved receipt. The older target reserved a fresh `prepared` attempt instead. The gate therefore returned `rejected_incompatible_receipt_recovery`, `rollback_qualified=false`, and `daemon_started=false`. This disproves compatibility for the new state; the earlier successful drill remains historical evidence for its earlier product revision only. A compatible rollback build still requires preparation and qualification. This is a local evaluation gate, not a deployed guard against arbitrary operator downgrades.

The same invocation completed the canonical suite: **56 passed, two skipped, one warning in 32.71 seconds**. Ruff and diff checks passed. Evidence: `bench/runtime/uat-runtime-rollback-receipt-gate.log`. No product source changed in this step, so the preceding 11,302-pass engine result still applies. Manual acceptance remains pending; no automatic continuation is counted as user approval.


## Compatible rollback build with saved-response backport

The old unmodified rollback target remains rejected. A separate local worktree and branch, `fix/runtime-rollback-saved-responses`, now pins `15cd18350035bb4a5205d285677a851786a4eba7`: parent `b1671409892` plus only the three runtime files needed to persist and recover successful handler responses. The exact reviewable backport is `bench/runtime/rollback-saved-responses.patch`. Reproducing the pinned drill requires that target Git object/branch in addition to the modernization branch; neither branch was pushed or deployed.

The drill checks the target parent, exact changed-file set and equal dependency pins. Its behavioral preflight recovers the original acknowledged response on both current and backported code. It then boots actual current → backport → current daemons with private canonically migrated PostgreSQL and Redis, checks health and clean shutdown, and verifies stopped/paused state, unresolved external-action records and saved tool-response records remain unchanged through every phase. Each source revision additionally passes the new native admission test in its own process with a scripted provider. Existing backport dispatch/journal tests pass **24 tests in 3.24 seconds**.

This qualifies the tested local same-dependency code rollback path, not arbitrary older revisions. The new admission probe runs separately from the booted daemon; HTTP admission within that daemon and container/dependency rollback remain unqualified. It does not prove parity for every newer product feature on the older code. No real business action, live provider call or production deployment occurred. Overall and manual acceptance remain open.

Final canonical result: **56 passed, two skipped, one warning in 25.84 seconds**. The initial drill, before the extra persisted-response snapshot check, also passed 56 tests in 31.51 seconds. Both logs and the 24-pass backport-focused log are retained under `bench/runtime/uat-runtime-rollback-backport-{initial,final,focused}.log`. Formatting/lint and diff checks passed.


## Installation-main task screening and observed deadline gap

At base `ddb6fb8f225`, the task/chat screening harness can load the actual installation main manifest via `ROBOTHOR_RUNTIME_SCREENING_INSTALLATION`. It preserves its settings (104 allowed tools, task protocol enabled, selected model/fallback configuration) rather than forcing the former one-tool simple profile. Execution still uses a private canonical database and isolated temporary workspace; production instruction/memory files are not staged. Native CRM creation and receipt verification are real. Execution and planning provider responses are scripted, with no live calls.

Both modes passed 30 measured requests plus one warmup, each with exactly one persisted task, one confirmed action and one completed native run. Main uses 62 execution plus 31 planning calls; minimal uses 62 execution and zero planning calls. The original standalone main screen had p95 **50.042 ms**, bootstrap interval **46.589–52.984 ms**. Final main and minimal invocations overlapped on separate private databases, giving diagnostic p95 **55.138 ms** (46.295–58.205) and **52.137 ms** (47.802–53.559), respectively; this is not a controlled performance comparison or live-provider latency claim.

The final fixture also reads persisted runtime deadlines: **all 31 main runs lacked a deadline**, whereas the simple minimal runs received one. The existing router treats more than 20 available tools as complex, regardless of a short message, and the main manifest does not explicitly declare simple difficulty. Thus successful fast runs do not establish the required 60-second bound. The remaining-work list now records this observed gap rather than describing it only as unproven. No global timeout or model configuration was changed.

Canonical results: main **57 passed, two skipped, one warning in 32.61 seconds**; minimal **57 passed, two skipped, one warning in 35.03 seconds**. Initial main also passed 57 tests in 33.55 seconds. Logs and per-sample JSONL artifacts: `bench/runtime/uat-runtime-{main-profile-screening,main-profile-screening-final,minimal-profile-screening-final}.{log,jsonl}`. Ruff and diff checks passed. Product source is unchanged; prior broad engine evidence remains applicable. This does not qualify full prompt/memory execution, the general workload, provider SLOs or manual acceptance. Nothing was deployed.


## Deadline enforcement after native simple classification

At base `03730405686`, native runtime admission now retains its original admission time for classification. When the existing router/planner identifies eligible interactive work as simple, the engine tightens its existing native timeout to admission plus 60 seconds, retains shorter limits, installs the deadline in the trusted execution context and writes it to the tenant/request-scoped run record. It does not invoke an additional model or replace the selected models. A native timeout that expired cannot become successful completion merely because a provider returned after swallowing cancellation.

Explicit moderate/complex manifests take precedence. Goal-bound work, children, resumption, read-only/deep-plan modes, benchmarks and noninteractive triggers do not receive this new classification policy. Tests check these exclusions, the original clock, cancellation of a stalled execution and preservation of shorter native limits. The focused policy/deadline suite passed **56 tests in 2.71 seconds**. Native integration additionally checks both cooperative and cancellation-resistant providers, with actual persisted cancelled run/deadline/cause and zero business calls.

Two initial integration failures are retained as fixture corrections: the first expected `timeout` instead of the existing host-deadline `cancelled` state; the second expected a return value even though the runtime correctly persisted cancellation then propagated `RuntimeDeadlineError` from post-cancellation dispatch denial. Neither was changed to accept completion. The final assertions require a cancelled row and the exact deadline reason, plus propagated error for the cancellation-resistant case.

All 30 main-profile screening requests plus warmup now persist a deadline and create exactly one verified task each; previously all 31 lacked a deadline. This uses actual installation settings, 104 allowed tools and task protocol, but scripted providers and an isolated workspace without production instruction/memory files. It records 62 execution and 31 planning calls. The diagnostic p95 is **48.326 ms** (bootstrap interval **46.177–48.666 ms**); the broad suite overlapped, so this is not a controlled speed comparison or live-provider claim.

The full 60-second acceptance contract remains incomplete: setup or planning can still stall before a simple classification arrives, and failed/unknown classification does not automatically assign a deadline. This change prevents a fresh clock after classification and bounds subsequent work; it does not claim to solve those earlier phases. Long-work exclusions and the remaining gap are explicit. No production state or model configuration changed; nothing was deployed.


The first broad run retained **16 failures, 11,298 passed, 29 skipped, 220 deselected, 393 warnings in 373.16 seconds**. Seven tests intentionally replace the timeout context with a no-op returning `None`; the post-loop expiry check now tolerates that disabled context while checking real timeouts. Eight identity/HTTP tests mock run creation but had not isolated the new deadline metadata write; their shared mocked-run fixtures now also mock that write. The real canonical tests continue to execute the database write. The remaining failure was the runner function-size ratchet; optional planner-context formatting was extracted into `run_lifecycle._attach_plan_context`, preserving its non-fatal behavior. No ratchet threshold was raised. The affected selection passed **109 tests, one warning in 38.74 seconds**; module-size checks separately passed **two tests in 1.27 seconds**. The failed broad run and corrected focused logs are retained, not discarded.


Final verification after those corrections: **11,314 passed, 29 skipped, 220 deselected, 393 warnings in 397.68 seconds** for the broad engine selection (excluding slow/integration/LLM/e2e/smoke); product source and selected tests were unchanged throughout the rerun. Final private canonical/main-profile screening passed **59 tests, two skipped, one warning in 34.34 seconds**. No regression was detected in those scopes. Ruff and diff checks passed. Final artifacts: `bench/runtime/uat-runtime-classified-deadline-engine-final.log`, `uat-runtime-classified-deadline-canonical-complete.log`, and `uat-runtime-classified-deadline-screening-complete.jsonl`. Intermediate logs and the failed broad invocation remain alongside them. The classification/setup gap and manual acceptance remain open.


## Bound setup and planning before interactive classification

At base `a1fbf899941`, eligible unclassified interactive requests receive a temporary admission-based 60-second window around native setup and planning. It counts time already spent resolving the profile. Existing earlier runtime deadlines retain ownership; explicit moderate/complex profiles and the existing goal/child/resume/read-only/deep-plan/benchmark exclusions retain their policy. Once native classification identifies simple work, the limit transfers to the existing native timeout using the same admission timestamp. An explicit supported moderate/complex planner result releases the temporary window. Failed parsing, missing difficulty and malformed difficulty do not release it. The guard invokes no additional model.

Provider/tool authorization checks reject work after this window expires, including a provider that swallows cancellation; a late classification cannot revive the request. A delegated guard cannot release its parent's temporary window. The host records a scoped terminal audit outcome if setup expires before the native run row exists, while retaining an existing native interruption record instead of duplicating it. The audit lookup and write are bounded separately; unavailable audit storage remains an honestly reported limitation rather than proof of completion.

The new native tests exercise setup, planning, invalid JSON, absent difficulty, malformed difficulty and valid complex classification against private canonical storage. They require terminal unverified outcomes recoverable through normal chat audit reads, no business dispatch, exactly one run record, and cleaned-up watchdog state. The complex case continues beyond a compressed setup window and completes under its existing policy. These tests exposed a pre-existing cleanup gap: setup/planning can exit before the run-loop `finally`. A new outer native watchdog scope stops a newly created watchdog and restores any parent context on every exit; normal loop cleanup is preserved. The initial two failing watchdog assertions are retained. Their audit and terminal-state assertions already passed before this fix.

Focused policy checks passed **63 tests in 2.74 seconds**, then **69 tests in 7.49 seconds** including size ratchets. Intermediate canonical checks passed **60 tests** after cleanup and **63 tests** with additional cases/screening. A first broad run was deliberately interrupted after **2,010 passed, 15 skipped, 224 deselected in 58.21 seconds** when review found the parser-default difficulty release gap; it is not a completed regression result. Its partial log is retained. Final source adds explicit raw-difficulty validation and the malformed-value case before starting a fresh broad run.

This verifies the implemented admission/setup/simple-execution policy on scripted local workloads. It does not establish real-model classification accuracy, live-provider latency, production ingress behavior, or universal real-time cancellation of external requests already dispatched. Existing audit/readback remains responsible for those effects. No live provider calls or production changes occurred. Manual acceptance remains open.


Final verification: **11,321 passed, 29 skipped, 226 deselected, 393 warnings in 392.75 seconds** for the broad engine selection, with final product source and selected tests unchanged throughout. The final private canonical/main-profile invocation passed **65 tests, two skipped, one warning in 37.75 seconds**. All 31 main-profile requests retained deadlines and exactly one verified task; diagnostic p95 **47.718 ms**, bootstrap interval **45.490–57.076 ms**, with scripted providers. No regression was detected in these scopes. Ruff and diff checks passed. Final artifacts are `bench/runtime/uat-runtime-classification-window-engine-final.log`, `uat-runtime-classification-window-canonical-complete.log`, and `uat-runtime-classification-window-main-final.jsonl`. Initial failures, intermediate passes and the intentionally interrupted broad run remain alongside them. Overall acceptance is still open.

## Selected-model native task screen: acceptance failed

At product revision `229837f906d`, a new opt-in `--live-task` canonical fixture ran 30 sequential requests with the installation's selected primary/fallback models, temperature, 104 allowed tools and task protocol. No model settings changed. Only native task creation/read tools, task bookkeeping and tool metadata could execute, against disposable canonical storage; forbidden direct/deferred business calls and foreign-tenant writes were checked before the first model call. Production instruction/memory files were not staged. This is direct native execution, not HTTP/browser queue timing or a matched baseline comparison. Each request has a 65-second harness cutoff and a 12-LiteLLM-invocation diagnostic limit; these are not additional production guarantees.

All **30 requests stored exactly one task with the requested title/body and TODO status**, with one confirmed action each and no post-return model calls. Only **27 runs completed**. Samples 19, 24 and 26 reached their native 60-second deadline after confirmed creation and persisted cancellation instead of a final answer. They remain failed repetitions. All timings are retained: p50 **35.363 seconds**, p95 **60.010 seconds**, bootstrap 95% interval **52.079–60.011 seconds**. **21/30** exceeded 30 seconds; three returned just over 60 seconds (approximately 60.01 seconds including cancellation handling). The 30-second p95 target fails. Native cancellation occurred before the 65-second fixture cutoff.

Complete persisted traces show **30 create_task calls across 30 requests**, plus 20 list_tasks, 16 get_task and two search_records calls. They do not show repeated creation attempts. In particular, `recovered: true` currently appears even on successful first-call verification readback; neither that flag nor model prose proves an attempted duplicate. The earlier one-request diagnostic also remains archived: 40.113 seconds with a fixture refusal of search_records, and only title/existence verification. The final cohort allowed tenant-scoped native search and independently checked body/status too. It does not discard or replace that earlier result.

The actual chat outcome projection read all three interrupted native records and returned confirmed task receipts, no reconciliation pending, and “The task was created” without a model or business dispatch. Request-key mapping was injected because these were direct native invocations rather than HTTP chat. The returned text still starts with a technical cancellation notice; whole-run verification remains false. This establishes audit recovery, not successful initial answer delivery. Next work must make normal chat use retained evidence promptly and clearly, while preserving honest partial-work reporting. Readback scope must not be inflated into proof that an arbitrary multi-step request is complete.

The run used 137 LiteLLM invocations: 128 selected primary, five MiMo fallback and four DeepSeek-v4-flash fallback. These count library invocations, not independently observed SDK HTTP retries. Native recorded cost estimate is **$0.141206**, not a billing measurement. No production business state or deployment changed.

Canonical invocation: **one failed, 64 passed, two skipped, one warning in 1168.95 seconds**; the failed test is the live cohort assertion. Report-accounting tests passed **three tests in 0.07 seconds**, including preservation of failed/unfinished populations and a confirmed write followed by an interrupted reply. Product source was unchanged, so the preceding 11,321-pass broad result remains applicable to that scope; it does not override this live failure. Ruff and diff checks passed. Full artifacts are `bench/runtime/uat-runtime-native-task-live-{cohort,diagnostic}.{log,jsonl}`, `-report.json`, `-traces.json`, `-recovered.json` and `-report-tests.log`. Manual and overall acceptance remain open. No replacement selection or causal regression/speedup claim is supported by this unmatched cohort.

## Deliver retained evidence in the first interrupted chat reply

At base `9236ee1360c`, normal chat now reads tenant/principal-scoped saved action receipts when execution returns an interrupted run or raises after persisting one. The latter path was exposed by the native HTTP test: a deadline exception previously bypassed final-result formatting. The read does not dispatch tools or invoke a model. Missing, ambiguous, foreign, still-running or unavailable audit records cannot produce an audited final result. The asynchronous lookup has a one-second wait bound and each SQL statement a 750 ms timeout; this does not promise that a cancelled background database thread has already terminated.

Interrupted replies lead with the recorded action evidence, followed by “Execution was interrupted. Any remaining work is not confirmed.” The persisted run stays cancelled/failed/timed out. Recovery uses the same text. Unverified effects remain described as unverified; this change does not infer whole-request success from a task record. Returned interrupted plan runs also use the shared formatter; plan exception handling and other ingress paths are not newly qualified by the ordinary-chat test.

A host-only `audit_outcome` field allows the frontend to display settled recorded outcomes immediately despite an interrupted run status. The host does not set this field for absent receipts or receipts requiring reconciliation. Without that field and nonempty text, failed/cancelled/aborted streams retain the existing automatic recovery behavior. Thus arbitrary partial model prose still cannot become verified completion. Normal completed responses add no audit lookup.

The native private-storage test uses real `/chat/send`, native task dispatch and readback, and a scripted provider that stalls while preparing the reply. It requires one persisted TODO task, one confirmed action, cancelled status, an audited first SSE response, matching reconnect text, matching chat history, and no additional provider calls. Direct saved-result queries for a different user or tenant must return nothing. The compressed half-second deadline tests delivery behavior, not live-provider performance. The previous live screen's 30-second latency failure remains open. Nothing was deployed.

Final verification: canonical **65 passed, two skipped, one warning in 38.99 seconds**; affected engine/chat and size checks **457 passed, one skipped, 11,126 deselected, seven warnings in 41.32 seconds**; frontend streaming/result checks **18 passed across two files in 1.86 seconds**; TypeScript checking passed. Final selected source was unchanged during these final invocations. No regression was detected in these scopes; the earlier 11,321-pass broad result is historical and was not rerun for this change. Ruff and diff checks passed.

Retained initial failures include the simulated provider signature mismatch, the actual exception-delivery gap, and a function-size ratchet failure (`run_approved` reached 201 lines). The final event assembly reuses the already constructed result mapping; no size threshold was raised. Intermediate checks and all failures remain alongside final evidence in `bench/runtime/uat-runtime-chat-delivery-*.log`. The full live latency acceptance failure remains open; manual acceptance is not inferred from these tests.

## Task receipt snapshots and live-fixture correction

At base `688856c6882`, native task verification now reads and retains the stored title, body and status, alongside the task ID. The result explicitly identifies `verification: verified` and `verification_scope: stored_task_snapshot`. These are observed database fields, not a claim that arbitrary requested criteria or the task's work are complete. Recovered receipts retain that saved snapshot rather than pretending to be a fresh read of later edits. Native success/replacement tests require the same stored fields and one task across both admissions.

A successful first native acknowledgement followed by host readback now returns `recovered: false`; uncertain responses and cached replacement-worker results retain `recovered: true`. Native note readback shares this fresh/recovered distinction. The old flag did not establish that a duplicate creation was attempted, and prior prose interpreting it that way was unsupported. No model, permission, runtime-stop or goal-completion policy changed. Verification does not terminate an arbitrary multi-step request.

The initial focused run retained one failure and 25 passes: the minimal test database lacked the real CRM task `body` column. Its schema now includes that field; production schema was unchanged. Focused recovery/dispatch/effect/goal-store tests passed 68 tests in 5.72 seconds. Canonical native checks passed 65 tests, two skipped, one warning in 37.30 seconds, including lost acknowledgements, deduplication, actual replacement workers and the interrupted initial chat reply.

An initial live diagnostic retained a correct single creation but took 37.049 seconds and five LiteLLM invocations. Its model reply exposed a fixture artifact: `live-task-<uuid>` contains `sk-` followed by a long random value, matching the existing credential detector. The model consequently discussed a nonexistent credential in the synthetic tenant field. The fixture now uses `runtime-live-<uuid>` and captures tool-step timing plus recovered/verification-scope metadata inside each sample. It does not weaken the detector. Earlier diagnostic and 30-request cohort artifacts remain retained; their accidental fixture pattern limits causal interpretation.

The corrected one-request diagnostic also created exactly one task correctly, but took 30.785 seconds and five primary-model invocations. Its trace shows search_records → create_task → get_task with four execution model calls plus planning. Creation returned `recovered: false` and the stored snapshot scope, yet the model still reread the task. Thus this change has not demonstrated fewer calls or a speed improvement. These one-request diagnostics are not qualifying screens, and the broad suite overlapped them. The previous 30-request latency failure remains open. Both full canonical invocations passed: 66 tests in 78.25 seconds for the original fixture and 66 tests in 65.03 seconds for the corrected fixture, each with two skips and one warning. No production state was changed and nothing was deployed.

Final broad verification passed **11,327 tests, 29 skipped, 228 deselected, 393 warnings in 391.51 seconds**. Product source and selected tests were frozen throughout; the excluded live fixture correction was exercised separately as described above. This also covers the preceding interrupted-chat delivery implementation. No regression was detected in that selection. Ruff and diff checks passed. Evidence, including the initial fixture failure and both live diagnostics, is retained as `bench/runtime/uat-runtime-task-snapshot-*`. Overall/manual acceptance and the live latency target remain open.

## Explain receipt verification and measure auxiliary planning

At base `06e12ef8220`, the first-party create_task description now explicitly explains that a returned verified stored-task snapshot already verifies its ID, title, body and status. It asks the model not to repeat that same check, while preserving checks for additional fields or later changes. It explicitly excludes completion of the task's work or remaining request steps. No execution shortcut or model/configuration change was introduced.

Focused tool tests passed **63 tests in 1.36 seconds**, and search/ranking/registry parity tests passed **166 tests in 1.63 seconds**. Both subsequent canonical/live diagnostic invocations passed **66 tests, two skipped, one warning**, in 89.43 and 67.75 seconds respectively. These are one-request diagnostics, not qualifying screens. The prior broad result is historical; only the affected checks were rerun for this description change. Ruff and diff checks passed.

The first diagnostic made one create_task call with no extra read and returned a correct verified task, using three provider invocations. Total request time was still **51.297 seconds**, despite execution model steps totaling about 11.7 seconds. The live harness now records each LiteLLM invocation's elapsed time, phase and error type when applicable, without recording provider messages or credentials. The phase label recognizes the known planning prompt in this controlled fixture; it is not a general production tracer.

The timed diagnostic also created exactly one correct task without rereading it, but took **30.042 seconds**. Planning consumed **22.753 seconds**: 2.659 seconds on the configured primary followed by 20.094 seconds on its configured MiMo fallback. Two execution/answering calls consumed **7.214 seconds**. There were no post-return calls. This identifies auxiliary planning as an observed latency source for that request; two unmatched diagnostics neither establish p95 nor prove a causal speed improvement.

Read-only inspection confirms the installation main profile has `planning_enabled=false`, no explicit planning model and no difficulty override. The existing router nevertheless enables planning when more than 20 tools are available. The next investigation should address that automatic interactive planning path while preserving explicitly enabled planning, plan mode and goal/delegated work. The description change itself does not alter those policies. All prior failed cohorts and both diagnostics remain retained under `bench/runtime/uat-runtime-task-description-*`. No production state or selected models changed; overall and manual acceptance remain open.

## Bound optional automatic interactive planning

At base `ce5d64a66d5`, auxiliary planning selected automatically for ordinary interactive work has a five-second phase budget. It reuses the existing runtime deadline owner and provider-dispatch checks, without another retry loop or detached planning task. A successful plan arriving in time is retained. When only this phase expires, execution proceeds without the optional plan under the existing request deadline; an expired outer deadline remains a failure. Cancellation-suppressing return values cannot become accepted late plans.

The policy applies only within trusted admission for eligible root webchat/telegram requests. Explicit planning enablement, a configured planning model, a manual difficulty override, approved-plan triggers, goals (including active goal context), delegated/parented work, resumption, read-only/deep-plan modes, benchmarks and noninteractive triggers retain their prior planning policy. Model selection, provider ordering, action authorization, confirmation requirements and task/goal completion criteria are unchanged. A failed optional classification does not release the existing unclassified-request deadline.

Focused planning/deadline tests initially passed 52 tests in 1.96 seconds. Two additional active-context exclusion cases were added before the final broad run. Size checks passed six tests in 2.33 seconds. Canonical native tests passed 67 tests, two skipped, one warning in 35.40 seconds. The new native cases stall planning with cooperative and cancellation-suppressing providers, require one planning invocation with no dispatched fallback after expiry, then execute the real task handler and readback. Each ends with one confirmed action, one TODO task and exactly two execution-model invocations.

A live diagnostic with the existing selected models returned one correct task in 22.153 seconds, with no post-return calls. Its planning call finished in 3.238 seconds, so it did not exercise the five-second cutoff. Three execution calls included an additional list_tasks check. This is neither a p95 qualification nor proof that redundant reads are eliminated. The canonical invocation including this diagnostic passed 68 tests, two skipped, one warning in 62.52 seconds. A complete 30-request screen is still required; all earlier failures and diagnostics remain retained. No production change or deployment occurred.

Final broad verification passed **11,344 tests, 29 skipped, 230 deselected, 393 warnings in 391.30 seconds** with final product source and selected tests frozen throughout. No regression was detected in this scope. Ruff, formatting and diff checks passed; artifacts are retained under `bench/runtime/uat-runtime-automatic-planning-*`. The full live screen remains required. User acceptance of repeated confirmation and saved-evidence recovery is recorded separately in `uat-chat-confirmation.json` and `uat-chat-recovery-acceptance.json`; it does not imply performance qualification or deployment approval.


### Automatic planning screen and saved deadline follow-up (2026-09-21)

The unchanged `474816619eb` source completed its full 30-request native task cohort using the existing installation model configuration. All 30 produced one correct TODO task, one confirmed action and a completed reply; no duplicate or post-return model work was observed. p50 was 16.012 seconds, p95 29.526 seconds, with bootstrap 95% interval 24.273–38.361 seconds. One request exceeded 30 seconds; none exceeded 60. There were 103 provider invocations (99 primary, four MiMo), with native estimated cost $0.120742, not a billing measurement. This passes this screen's point-estimate criteria, but the uncertainty crosses the target. The isolated instructions/memory, direct native ingress and unmatched comparison limitations still apply. Earlier failures remain retained.

Sixteen planning invocations reached the five-second cap. Those 16 successful requests retained the in-memory classification timeout but did not persist a deadline. The screen therefore does not qualify the restart deadline contract. Subsequent code preserves that original classification owner, saves its original expiry, and restores saved deadlines during compatible checkpoint admission. A shorter incoming limit wins. Expired saved requests cannot reach execution; malformed timestamps fail closed. A validated new goal attempt can retain its separately granted limit. Legacy checkpoints with no recorded limit remain compatible; the patch cannot reconstruct historical missing deadlines.

The native continuation test now checks the restored timestamp on the new run, plus expired admission in both a new runner and a fresh Python process connected only to the disposable database. This is a checkpoint admission proof, not a full-daemon crash drill for this change. Tests also retain valid complex-plan behavior and existing classification-expiry reporting. Initial canonical runs caught changed interruption behavior, then a repeated-application bug that released the guard; both failed logs are preserved and a regression test now covers repeated application. Partial broad runs were deliberately interrupted while those fixes were made and do not count as passes.

Final verification for the saved-deadline fix passed **81 focused tests**, **68 canonical native tests (two skipped)**, and **11,363 engine tests (30 skipped, 220 deselected, 393 warnings; 487.03 seconds)**. The broad command was `.venv/bin/python -m pytest -q robothor/engine/tests -m 'not integration'`; unlike the earlier selection, it also included slow tests. Final product source remained unchanged throughout that broad run. Ruff, formatting and diff checks passed. No regression was detected in this tested scope. The logs, including failed intermediate checks and intentionally interrupted partial runs, are retained under `bench/runtime/uat-runtime-resume-deadline-*`. The preceding live measurements remain attributed to `474816619eb`; changed-source live requalification, representative baseline comparison, full prompt/memory parity, remaining manual goal controls and production qualification are still open. No deployment occurred.


### Approved-plan exception delivery (2026-09-21)

Approved normal/deep plan execution now consults the owned saved outcome when the runner raises or is cancelled, using the same bounded audit lookup as ordinary chat. A saved terminal result is delivered to the initial stream and chat history without another model call. Only that approval is retired; durable approval receipts still prevent re-execution. When no owned saved result is available, the existing error/cancellation response and consumed approval remain intact. The shared helper keeps the existing plan handler within its size limit.

The private canonical database test executes an approved normal plan, creates exactly one TODO task and one confirmed effect, then stalls the reply until the native deadline. Initial HTTP delivery and reconnect both report the stored task and unconfirmed remaining work. Repeating the same approval returns the existing 409/already-received contract, makes no new model call, and leaves one task. Wrong-tenant/principal audit reads remain denied. Mocked-runner route tests additionally cover normal/deep exceptions, cancellation, unavailable saved results and repeated approvals. This does not claim a new real deep-worker exception test.

Verification: **61 focused tests**, **465 chat/size regression tests (one skipped, 11,156 deselected, seven warnings; 38.73 seconds)** and **69 canonical native tests (two skipped, one warning; 36.96 seconds)** passed. Ruff, formatting and diff checks passed. The first size-check failure was fixed by extracting delivery; an initial native test wrongly expected HTTP 200 for a repeated approval and was corrected to assert the existing 409 contract and unchanged action/model counts. Those failed logs are retained. Final chat and canonical tests ran against unchanged product source. Evidence: `bench/runtime/uat-runtime-plan-interruption-*`. No production deployment or broader acceptance is claimed.


### Native approved deep-plan stop and late evidence (2026-09-21)

An added HTTP integration test now exercises approval, abort and outcome recovery with the real native deep runner, a real worker thread and the canonical private database. A deterministic external reasoning function pauses behind a test barrier. Chat records the durable stop while that worker remains active, acknowledges cancellation, and reports `stopping` with no terminal/verified claim. Repeating the approval returns the existing already-received refusal. When released, the worker's attempted new tool call is denied by durable controls; its late synthetic analysis and $0.12 fixture cost are retained on the sole run. Outcome recovery then reports cancellation and recorded evidence without claiming goal/request completion. Exactly one worker invocation occurs. The test's cleanup releases and drains the worker on either success or failure.

Verification passed **18 focused deep/control/chat tests (2.24 seconds)** and **70 canonical native tests (two skipped, one warning; 42.07 seconds)**. Ruff, formatting and diff checks passed. Product code did not change in this step. Logs are `bench/runtime/uat-runtime-deep-plan-chat-{focused,canonical}.log`. This closes the native chat-to-worker stop evidence gap; it does not qualify live external reasoning internals, provider latency, p95 stopping targets or process-crash behavior for this route. No production deployment occurred.


### Current-source live task requalification — latency failed (2026-09-21)

The frozen `7f000740037` revision completed a new 30-request native cohort with the existing installation model/settings and private synthetic CRM. All 30 had one correct TODO task, one confirmed action, a completed reply and a persisted deadline. No duplicates or post-return model calls occurred; none exceeded 60 seconds. This directly confirms that the earlier missing-deadline population is fixed in this scenario.

Performance did **not** pass: p50 **16.417 seconds**, p95 **45.344 seconds**, bootstrap 95% interval **30.694–50.873 seconds**; **seven** requests exceeded 30 seconds. The model invocation counts were 100 primary and two MiMo; native estimated cost was $0.122642, not provider billing. Canonical plus live behavioral tests passed 71 tests (two skipped, one warning; 654.95 seconds), but that test exit code is not latency qualification. The report explicitly records `screening_passed: false`. All attempts and prior populations remain retained.

Recorded model time after the successful `create_task` tool turn had p95 **31.833 seconds** (interval 15.181–40.473). One 45.344-second request spent 40.473 seconds in its only post-creation model call. Another slow request made two additional `list_tasks` calls after the host had already verified a stored-task snapshot. Other slow requests spent time before creation. These observations identify multiple costs; they do not prove that the deadline change caused the slower population. No matched/interleaved causal baseline is available, and isolated prompt/memory and direct native ingress limitations remain.

Next work should investigate bounded, factual delivery of verified task-creation results, while preserving requests with further work, controls, evidence checks and the model's ability to handle unsupported outcomes. Merely lowering planning time would not remove the observed 40-second post-creation reply delay. No untested delivery design is approved by this measurement, and no product change was made during the cohort. Evidence: `bench/runtime/uat-runtime-current-deadline-cohort.{log,jsonl}` and `bench/runtime/uat-runtime-current-deadline-cohort-report.json`. Overall acceptance and deployment remain unapproved.


### Explicit final task receipt and hierarchy UAT acceptance (2026-09-21)

The user accepted the normal-chat parent/child pause case: “Yes, that matches.” The accepted behavior pauses both the goal and its unfinished child, leaves the task TODO, and marks neither goal complete. `uat-chat-goal-hierarchy.json` records that specific acceptance; overall UAT remains open.

`create_task` now offers an explicit `finalReport` option for an ordinary standalone title/body/status creation request. The model chooses whether that report answers the entire request. The host independently checks the authenticated durable effect and matching stored task fields, then publishes a factual reply through the existing scoped final-report channel without another model turn. Ordinary tool data cannot publish the report. Unsupported fields, unconfirmed/mismatched evidence, goals, delegation, multi-tool turns, background/read-only/benchmark use, resumed checkpoints, approved plans and unfinished checklists retain normal execution. Pending steering/interruption and final output validators still take precedence. Cached receipt wording identifies previously recorded data; it does not claim a fresh observation or that the task's work is done. This is not a natural-language proof that the model understood every obligation.

The native private-database HTTP test creates one task and one confirmed action, delivers and persists the reply, and recovers it without another write. It records one execution-model call with explicit final reporting, versus two without it, with task protocol enabled. Effect fingerprints exclude the presentation option, so changing it on a replacement worker reuses the same action; distinct requests still create independently authorized tasks. The first native assertion expected byte-identical reconnect text, but the existing recovery API appends an audit explanation. The corrected test checks the original reply plus that explanation and unchanged task/model counts; the failed log is retained.

Verification: 214 focused tests, 58 final replay-wording/report/control tests, 72 canonical native tests (two skipped), and 11,394 engine tests (29 skipped, 227 deselected, 393 warnings; 472.34 seconds) passed. The broad run covered final execution/guard logic; a cached-receipt label was refined while it ran and covered by the separate final focused check, including four extra replay cases. An earlier partial broad run was intentionally interrupted to add unfinished-checklist/resume/approved-plan guards and is not counted as a completed pass. Ruff, formatting and diff checks passed. Live model selection, compound-request behavior and latency remain unqualified.

The rollback preflight now probes task-report fingerprint compatibility as well as saved handler responses. It rejected `15cd1835003` before daemon startup. New separate backport `5a8ce0323d8eb9b751bda4d0635e7105e0f14d6c` preserves both behaviors and passes the existing current/rollback/current private daemon, state-preservation and separate native new-admission checks. Its exact three-file diff is `bench/runtime/rollback-task-report-identity.patch`, with unchanged dependency pins. This drill does not qualify HTTP admission inside that daemon, deadline restoration by downgraded checkpoints, or container/dependency rollback. No deployment occurred.


### Final task receipt diagnostic and reconnect polling (2026-09-21)

One live diagnostic on product revision `5728c35430e`, using the existing selected model configuration and private synthetic CRM, created and verified one task in **4.367 seconds**. It used one planning call and one execution call, then delivered the trusted receipt without a post-creation model call. This is a single observation, not a qualified latency screen or proof of compound-request intent handling. The previous failed 30-request population remains retained.

The combined diagnostic command exited with two failed reconnect assertions, 71 passes and two skips: both new native report tests polled before the asynchronous terminal run record was persisted. The test now polls the same request identity for at most two seconds, without executing it again; reply, audit explanation, task count and model count assertions remain. No product change was made. The canonical recheck passed **72 tests, two skipped, one warning in 40.15 seconds**. Raw diagnostic failures, the live sample and the passing recheck are retained under `bench/runtime/uat-runtime-task-final-{live,recovery-poll}*`. Overall acceptance remains open; no deployment occurred.


### Thirty-request final task receipt screen (2026-09-21)

Frozen `ceb9d85f68d5614c81ee23e51e5106694a952c0c` (product implementation `5728c35430e`) passed all 30 native task requests with the existing installation model configuration. Each produced exactly one correct TODO task, one confirmed action, a completed reply and a saved deadline. All 30 used the trusted final task receipt; no duplicates or post-return model calls occurred. p50 was **8.693 seconds**, p95 **21.046 seconds**, bootstrap 95% interval **15.417–22.969 seconds**. None exceeded 30 or 60 seconds. There were 62 primary and five MiMo provider invocations, with native estimated cost $0.090823 (not billing). The accompanying canonical suite passed **73 tests, two skipped, one warning in 350.93 seconds**. Source was unchanged throughout.

This meets this scenario's 30-request screening target. The previous 45.344-second p95 population remains retained; these sequential populations are not an interleaved causal comparison or accepted-baseline parity proof. The private workspace omits production instructions/memory and uses direct native admission rather than HTTP/browser ingress. Compound-intent selection and representative-workload qualification remain open, as does overall acceptance. Artifacts: `bench/runtime/uat-runtime-task-final-cohort.{log,jsonl}` and `uat-runtime-task-final-cohort-report.json`. No deployment occurred.


### Compound request diagnostic (2026-09-21)

The live harness now supports an explicit `task_and_calculation` scenario: create the requested task with its exact body, then separately answer 17 × 19. Three diagnostic requests on unchanged product `5728c35430e` all created one verified task and returned **323** separately. None used the task-only finalizer; each retained the model reply after creation. The final harness checks the answer at numeric word boundaries; this small assertion tightening was applied after the run and all three retained replies were revalidated offline.

Durations were **50.003, 29.834 and 26.225 seconds**. One exceeded the simple-action 30-second target; none exceeded 60. The diagnostic therefore does not qualify broader latency, nor is three examples sufficient for general compound-intent acceptance. All nine invocations used the selected primary model; estimated cost was $0.009746, not billing. The combined canonical run passed **73 tests, two skipped, one warning in 140.52 seconds**. Ruff, formatting and diff checks passed. This adds evidence that the explicit task report preserves extra work in these examples; it does not override the remaining performance and baseline comparison gaps. Evidence: `bench/runtime/uat-runtime-task-compound.{jsonl,log}` and `uat-runtime-task-compound-report.json`.


### Rollback checkpoint deadline compatibility (2026-09-21)

The rollback preflight now tests saved deadlines in a fresh process against the private canonical database. It requires expired checkpoints to deny execution, future deadlines to reach execution unchanged, and malformed deadlines to be rejected. Prior target `5a8ce0323d8` admitted all three without any restored deadline; the new gate rejects it before daemon startup even though its action receipts remain compatible. The first harness run exposed a UUID/text-array cleanup error; explicit UUID casting fixed it, and that failed log is retained.

Separate backport `53f588cb6bcd89f6f2f86a3cf3cf990b9232d3f6` combines the prior receipt fixes with checkpoint deadline restoration and bounded resumed execution. It changes exactly six files from `b1671409892`, with dependency pins unchanged. The exact diff is `bench/runtime/rollback-deadline-compatibility.patch`. Both current and backport pass the fresh-process deadline and receipt probes. All three actual daemon phases (current, rollback, restore) become healthy and stop cleanly, preserve stopped state and unresolved/saved action records, and pass separate native new-admission tests. Backport checkpoint/runtime tests passed **42 tests in 4.74 seconds**. The accompanying current canonical suite passed **72 tests, two skipped, one warning in 33.35 seconds**. Ruff and diff checks pass.

This closes the demonstrated downgrade deadline-admission gap. The current product code did not change. This drill still does not exercise HTTP admission inside the running daemon or dependency/container rollback, and does not authorize deployment. Results and the initial failure are retained under `bench/runtime/uat-runtime-rollback-deadline-*`; the report identifies both rejected and accepted targets.


### Accepted-baseline native task parity and overhead (2026-09-21)

New `--native-task-comparison` runs the same portable worker against accepted baseline `eea3252b1577e3f51e6d6a6216d6f242fd7ac4a1` and current product `f69694be958`, in separate fresh processes connected to the disposable canonical database. Each revision runs 30 warm standalone task requests and 30 requests with a separate calculation, retaining one warmup per scenario. All **124 requests** passed: exactly one matching TODO task, completed reply, exactly two scripted model calls and no calls after return. Compound replies retained the calculation. The final-report option is off on both sides, demonstrating preservation of the existing path rather than relying on the new opt-in.

Standalone p95 was **123.673 ms baseline → 44.970 ms current** (bootstrap 95% intervals 122.346–128.356 and 38.836–47.643 ms). Compound p95 was **127.607 → 48.704 ms** (126.124–129.020 and 44.962–50.823 ms). These sequential synthetic populations use the same minimal profile, scripted provider, task protocol and private storage. Product prompts and tool schemas come from their respective revisions; this is not a matched live-provider/framework qualification or full production profile test. TCP connections are refused, and audit/event delivery and embeddings are stubbed. No production state or credentials are used.

The accompanying canonical integration suite passed; its exact summary is retained in `uat-runtime-native-task-comparison-report.json`. After the successful run, subprocess-timeout handling was strengthened and a focused test passed: partial output survives, missing requests are recorded, the other revision still runs, and the comparison cannot pass. Ruff, formatting and diff checks passed. Raw outputs, all samples, uncertainty and the timeout test are retained under `bench/runtime/uat-runtime-native-task-comparison-*`. Overall acceptance remains open.

Reproduce with `ROBOTHOR_RUNTIME_BASELINE_CHECKOUT=/home/philip/robothor-runtime-baseline .venv/bin/python -m bench.runtime.migrated_integration --native-task-comparison` from the modernization worktree.


### Full compound-request screen — failed (2026-09-21)

Frozen `f5be69acf4486067960fedaa1082ddcae8301a8d` completed all 30 task-plus-calculation attempts with unchanged selected models/settings and private CRM. All 30 created exactly one correct TODO task with a confirmed receipt, but only **27 returned complete answers**; **three hit the host deadline after creation**. No duplicate or post-return model work occurred. P50 was **31.123 seconds**, p95 **60.014 seconds**, bootstrap 95% interval **46.517–60.016 seconds**. Seventeen requests exceeded 30 seconds; three measured just over 60 seconds including timeout delivery overhead. This screen fails correctness/completion and latency acceptance. The separate passing standalone-task cohort does not override it.

There were 91 primary and nine MiMo provider invocations; native estimated cost was $0.119536, not billing. The 61 stored execution-model steps were captured read-only before database teardown. Successful calls typically reported about 21,000 input tokens and short outputs. Cancelled calls with zero recorded usage do not establish zero billable usage. Delays occurred both before and after creation; these traces cannot distinguish provider queueing from processing time. The combined command reports the live test failure separately from the other canonical tests. All samples, failures and token records are preserved under `bench/runtime/uat-runtime-task-compound-cohort*` and `uat-runtime-task-compound-tokens.json`.

A read-only schema construction diagnostic found that existing opt-in deferred tool discovery reduces advertised schema characters from 73,163 to 17,546 (108 to 24 tools), with task creation and goal controls still directly available. Character counts are not provider token counts or proof of faster execution. The option remains disabled in the installation; a separate isolated evaluation is the next experiment. No product code, model selection or production configuration changed during this screen.


### Smaller tool-context diagnostic — not promoted (2026-09-21)

The existing deferred-tool mode was enabled only inside the live test using a new explicit `deferred_tools` setting; pytest restores the environment afterward. The harness now records advertised tool/schema sizes and per-step model/token counts. Models, fallbacks, temperature, allowed-tool policy and task protocol remain unchanged. The installation flag remains off, and no product implementation change was made.

All **160** discovery/ranking/permission checks passed (3.15 seconds). Three live compound examples all created one correct task and separately answered the calculation, without task-only finalization or duplicates. Input tokens fell to roughly 7,400 on the initial execution call, but durations were **53.0, 14.3 and 35.5 seconds**. This does not establish the 30-second target or general behavior parity. The accompanying canonical suite passed **73 tests, two skipped, one warning in 145.89 seconds**. The option is **not promoted**; the previous 30-request failure remains authoritative for the existing configuration.

Artifacts: `bench/runtime/uat-runtime-task-compound-deferred-report.json` and matching `.jsonl`/`.log`, plus `uat-runtime-deferred-tools-contracts.log`. Sequential small samples do not establish a causal speed comparison. Further work must preserve configured model order, budgets, reliable fallback and all supported tool access; reducing context alone has not solved the observed long model calls.


### Per-model allowance diagnostic (2026-09-21)

Code inspection found that the native model chain defaults to a 120-second per-model allowance, whereas ordinary unresolved/simple interactive requests have a 60-second outer deadline. A slow first model can therefore consume the request window before fallback. The live harness now supports an explicit, test-local `model_slice_seconds` override and records the actual timeout passed to LiteLLM. The existing shared retry allowance, selected models and fallback order remain in place. No installed setting or product default changed.

With a 15-second allowance and ordinary full tool context, three compound examples completed correctly in **19.5, 21.4 and 15.0 seconds**. Crucially, none of their execution calls hit the allowance: this is not evidence that earlier fallback caused faster results. The accompanying canonical suite passed **73 tests, two skipped, one warning in 95.17 seconds**. These are diagnostic examples, not a screening cohort; the setting is not promoted. Planning retains its existing five-second bound. Artifacts: `bench/runtime/uat-runtime-task-compound-slice15-report.json` and matching `.jsonl`/`.log`. A full screen is needed to observe the policy under naturally slow calls and check correctness, not just speed.


### Host task-report request boundary — reproduced and fixed (2026-09-21)

The 15-second experimental model-allowance screen on `244a3978782` was stopped after a critical failure: after primary timeout, the selected fallback requested `finalReport=true` for a task-plus-calculation request. The host created the task and prematurely ended the reply, omitting the calculation. Three finished samples exhibited this failure; one passed, one was started but unfinished when the test was interrupted, and 25 were not started. All retained finished tasks were correct; the whole-request completion was false. The experiment is rejected, not a completed 30-request screen. No production setting was changed.

A native HTTP regression reproduced the failure without live models: a deliberately wrong finalReport flag skipped the additional answer (one failed, 73 passed, two skipped). The host now recognizes only a complete quoted title/body task-creation command with exactly matching arguments and TODO status before publishing its final task receipt. Additional instructions, ambiguous/unquoted wording, loaded skill instructions, consumed steering and any earlier tool calls retain ordinary model execution. Existing evidence, identity, permission, checkpoint, plan, checklist and late-control guards remain. This is deliberately a bounded command recognizer, not a general natural-language intent classifier. It does not restrict the tasks Robothor may handle; it restricts when the host can independently finish their reply.

The fixed HTTP regression returns the separate calculation despite the wrong flag, persists one TODO task and one confirmed action, recovers the complete reply, and records two model calls without a task-only checkpoint. Recognized standalone commands retain one-model-call delivery. Final validation passed **74 focused tests (0.91 seconds)** and **74 canonical tests, two skipped, one warning (35.59 seconds)**. An earlier corrected canonical pass (38.77 seconds) preceded the additional prior-tool guard. All intermediate/failing logs are retained in `bench/runtime/uat-runtime-task-intent-*`; the interrupted live population remains in `uat-runtime-task-compound-slice15-cohort*`. Broad engine checks and live requalification of the new boundary remain pending. Earlier passing live screens do not qualify the changed source.


### Goal-report request boundary and compound reply recovery (2026-09-21)

A deterministic check reproduced the task-report failure class in the separate goal-report channel: a factual goal-only report could end a request that additionally asked for a calculation. The running task-fix broad suite was intentionally interrupted to fix both paths together; **8,378 passed, 28 skipped, 229 deselected, 384 warnings in 299.96 seconds** is a partial run, not broad acceptance. Its log is preserved.

Goal-report finalization now requires a recognized standalone status/control request. Explicit requested goal IDs must match the tool target. Control confirmations additionally require a successful matching control step and a matching freshly read status. Loaded skills, unknown consumed steering, prior unrelated tools, resumed checkpoints, approved plans and unfinished checklists prevent early completion. Requests outside that supported grammar still receive the authorized goal facts, with `report_prepared=false`, so the model can answer their additional questions. The tool remains available; its data cannot itself authorize early completion. Existing tenant/principal/run scoping and late-control checks remain. Known supplemental execution-status steering remains supported because the factual report includes that information.

The native private-database test deliberately selects the report tool for a goal-plus-calculation request. It now records two model calls, no final-report checkpoint, the full factual reply plus **323**, and recovers the original saved reply twice without another model call or action. Simple recognized reports retain one-call delivery. A stale successful pause trace cannot override the currently queued goal state. The first integration attempt exposed audit truncation from returning duplicate rendered text alongside the whole goal; the final handler returns only the existing goal facts and completion flag, preserving the audit payload. The fixture/request-text failure and truncation failure are retained.

Final verification passed **94 focused tests (one warning, 5.24 seconds)** and **75 canonical tests (two skipped, one warning, 38.10 seconds)**. Ruff, formatting and diff checks pass. The combined broad run and changed-source live qualification remain pending. The earlier rollback target's report-boundary behavior must be reassessed before promotion. No deployment or installed setting change occurred. Evidence: `bench/runtime/uat-runtime-goal-report-*` and `uat-runtime-task-intent-engine-interrupted.log`.


### Complete-request reporting and rollback gate (2026-09-21)

Product revision `0bad835df29` completed all three native task-plus-calculation diagnostics using the existing selected models and no experimental timeout or deferred-tool override. Each stored exactly one matching TODO task and confirmed receipt, returned the additional answer, and made no post-return model calls. Durations were 22.250, 38.708 and 37.512 seconds; two exceeded the 30-second target. This diagnostic is **not** a screening qualification. Canonical integration including this live diagnostic passed 76 tests, with two skipped and one warning, in 130.18 seconds. Raw events and report: `bench/runtime/uat-runtime-report-boundaries-live*`.

A portable fresh-process behavioral probe confirms current code preserves standalone goal reporting while leaving compound requests open. Rollback revision `53f588cb6bc` fails: it finalizes both. The application rollback gate now requires this property before starting a daemon, alongside receipt identity and deadline preservation. The older rollback receipt/deadline success is retained but does not qualify rollback after this fix. Evidence: `bench/runtime/uat-runtime-report-boundary-rollback.json`. No production deployment or shared business-data changes occurred.

The combined full engine regression at `0bad835df29` passed: **11,434 passed, 29 skipped, 230 deselected, 393 warnings in 465.72 seconds**. Product source remained unchanged during the run; only independent benchmark rollback checks and evidence were added. The earlier interrupted task-only suite is not counted as a pass. Raw output: `bench/runtime/uat-runtime-report-boundaries-engine.log`.


### Compatible reporting rollback verified (2026-09-21)

Rollback revision `43b4af630738cae68850f49192e3082715407202` builds on the receipt/deadline backport `53f588cb6bc`. It adds the same complete-request goal-report admission, successful-control and fresh-status checks as current code; it retains the older ordinary task execution loop. No models or dependencies changed. The exact cumulative patch from `b1671409892` is retained in `bench/runtime/rollback-reporting-compatibility.patch`; the harness verifies its fourteen-file boundary.

The rollback checkout passed 46 focused tests (one warning, 5.08 seconds) and its canonical disposable-database integration suite passed 43 tests (one skipped, one warning, 12.75 seconds). The latter includes both native standalone and compound goal replies and audit recovery. Current code then passed the actual private current → rollback → current daemon drill with all compatibility gates: saved receipts, task fingerprint reuse, persisted deadlines, and standalone-versus-compound goal reporting. Each phase retained stopped state, unresolved effects and saved response records and passed a separate native new-admission contract. The current canonical suite passed 75 tests (two skipped, one warning, 36.23 seconds).

Evidence: `bench/runtime/uat-runtime-rollback-reporting-{focused,canonical,drill}.log` and `uat-runtime-rollback-reporting-report.json`. This closes the newly discovered report-boundary rollback incompatibility; the prior failing target remains rejected. HTTP admission inside the booted daemon and container/dependency rollback remain outside this drill's verified scope. Production was not changed. Overall acceptance remains open, including live latency and pending normal-chat UAT.


### Ordinary-chat catalogue routing (2026-09-21)

At `c342a50cd74`, ordinary interactive requests no longer acquire an auxiliary planner solely because more than twenty tools are available. Large catalogues retain moderate execution defaults; no tools, model selections, validators or permission checks are removed. Explicit planning, configured planning models/difficulty, long-context complexity, goals, delegated work, resumed runs, approved plans and background requests retain their previous routing policy. The router's legacy default remains unchanged outside the eligible interactive context.

Focused routing/exemption tests: 37 passed in 0.29 seconds. Canonical private-database tests: 76 passed, two skipped, one warning in 35.26 seconds. The new native case uses a real catalogue of more than twenty tools, makes zero planning calls, creates exactly one verified task through normal execution, and preserves the root deadline. Existing long-context planner expiry tests still pass. Initial fixture failures (missing goal attempt identity; unsupported wildcard tool selector) are retained, with corrected fixtures using registered schema names.

Three selected-model compound requests then completed correctly in 9.540, 7.534 and 22.972 seconds, with zero planning calls, duplicate tasks or post-return model calls. This is an unpaired diagnostic, not proof of comparative speed or screening qualification. The combined canonical/live command passed 77 tests, two skipped, one warning in 76.94 seconds. Evidence: `bench/runtime/uat-runtime-catalogue-routing-*`. Full engine regression and a new 30-request live screen are running; earlier populations do not qualify this changed product source.


### HTTP admission across actual rollback daemons (2026-09-21)

The rollback harness now sends a real loopback `POST /chat/send` to each booted current, rollback (`43b4af63073`) and restored-current daemon, verifies the completed SSE reply, and retrieves the same request through `/chat/outcome`. Each phase records exactly one scripted provider call; outcome recovery makes no additional call. The fixture is loaded from the driving checkout while native product imports are asserted to originate in the selected source checkout. Network/executable guards, private PostgreSQL/Redis, empty business tool policy and explicit handler denial prevent real business effects. Stopped work, unresolved effects and saved response records remain unchanged across all three phases.

This closes the previously recorded in-daemon HTTP admission gap. The run also passed 76 canonical tests (two skipped, one warning, 32.47 seconds). Evidence: `bench/runtime/uat-runtime-rollback-http-drill.log` and `uat-runtime-rollback-http-report.json`. It remains a same-dependency application-code rollback with a scripted provider, not container rollback or live-provider performance evidence. Product source was unchanged while the separate engine regression and 30-request live screen ran.

Full engine regression for catalogue routing `c342a50cd74`: **11,443 passed, 29 skipped, 231 deselected, 393 warnings in 470.31 seconds**. Product source was unchanged during the run. Integration/slow tests were excluded from this command and covered separately where stated. Raw result: `bench/runtime/uat-runtime-catalogue-routing-engine.log`. The thirty-request live population is still in progress and is not yet scored.


### Complete catalogue-routing screen (2026-09-21)

Product `c342a50cd74` completed all thirty native task-plus-calculation requests correctly: one matching TODO and confirmed receipt per request, the additional answer present, no duplicate tasks, no post-return model calls, and zero planning calls. It used sixty primary execution calls and a native estimated cost of $0.113221 (not billing). p50 was 16.727 seconds; p95 **33.872 seconds**, bootstrap interval 26.466–36.009. Three requests exceeded thirty seconds; none exceeded sixty. **Performance screening fails** despite correctness passing. The combined canonical command passed 77 tests, two skipped, one warning in 595.26 seconds. Full engine regression separately passed 11,443 tests as recorded above.

Slow samples were dominated by the primary model calls carrying about 21,000 input tokens. This is attribution, not proof that reducing input size will resolve latency. A separate thirty-request private deferred-tool screen was started with the same product and selected models, the existing flag enabled only inside that fixture. It is not promoted or configured in the running installation. No per-model timeout override is applied. Prior populations remain retained; the experiment is not a paired baseline comparison. Source stayed fixed; independent rollback harness work ran concurrently for part of the completed screen.

Evidence: `bench/runtime/uat-runtime-catalogue-routing-cohort.{log,jsonl}` and `uat-runtime-catalogue-routing-cohort-report.json`. New in-progress events: `/tmp/runtime-catalogue-deferred-cohort.jsonl`.


### Deferred dispatch recovery verified (2026-09-21)

The reduced-tool-context experiment must preserve supported action recovery through the `tool_call` wrapper. Extended the existing real-CRM lost-note-response integration to use both direct `create_note` and wrapped `tool_call(create_note)`. Both immediate readback and delayed terminal-sweep recovery verify exactly one note, reuse the saved result on repeated calls, and expose the same outcome through authenticated chat recovery with no model calls. All writes use disposable canonical storage. This tests actual nested dispatch and its effect ledger, not just a mocked permission boundary.

The canonical suite passed 78 tests, two skipped, one warning in 43.16 seconds. Discovery, search ranking/vocabulary and discovery-truth tests passed 186 tests in 1.70 seconds. Logs: `bench/runtime/uat-runtime-deferred-note-canonical.log` and `uat-runtime-deferred-discovery-current.log`. No product code or installed flag changed. The separate 30-request deferred live screen remains in progress, so these checks do not authorize or qualify its promotion.


### Reduced-context screen completed; latency not qualified (2026-09-21)

The deferred-tool experiment on product `c342a50cd74` verified all thirty task-plus-calculation requests, with one matching TODO and confirmed receipt each, no duplicates, no post-return model calls and no request over sixty seconds. Sixty primary execution calls were recorded. Advertised schemas decreased from 112 to 24 (17,810 schema characters), with roughly 7,500 initial input tokens instead of 21,000. Native estimated cost was $0.043299 (not billing).

The latency gate **fails**: p50 13.628 seconds, p95 39.362 seconds (bootstrap interval 23.736–42.529), three requests over thirty seconds. This sequential unpaired population does not establish a reliable comparative latency benefit. Reduced context is not promoted and the installed/default flag remains off. Canonical integration including the screen passed 77 tests, two skipped, one warning in 559.17 seconds. Evidence: `bench/runtime/uat-runtime-catalogue-deferred-cohort.{log,jsonl}` and `uat-runtime-catalogue-deferred-cohort-report.json`.

A separate thirty-request experiment now caps each model attempt at ten seconds inside the live test only, keeping the full tool catalogue, current native product and selected model/fallback order. This uses the existing fallback mechanism to test whether slow calls can be bounded without losing obligations or repeating writes. It changes no production model selection, timeout or flag. It is not qualified until its complete correctness, latency and accounting results are inspected. In-progress events: `/tmp/runtime-catalogue-slice10-cohort.jsonl`.


### Accepted-baseline comparison with selected main settings (2026-09-21)

Extended the portable native comparison to opt into the selected main execution profile, retaining its 104 allowed tools, task protocol, primary/fallback settings and automatic routing policy. Execution and optional planning are scripted identically in both source checkouts; TCP connections are denied. Private instructions/memory and outbound delivery are excluded. The legacy finalReport-off path still uses two execution calls in both revisions, so this does not measure the new host fast path. The isolated minimal-profile mode remains available.

All 124 requests passed (thirty warm standalone plus thirty warm compound requests and two warmups per revision), each with one matching TODO, preserved additional calculation and no post-return execution calls. Recorded selected configurations match. Baseline `eea3252b1577` made 62 auxiliary planning calls; current product `c342a50cd74` made none. Standalone p95: 134.992 ms (bootstrap interval 111.097–137.621) versus 54.318 ms (48.122–55.111). Compound p95: 133.687 ms (128.228–135.787) versus 56.106 ms (48.141–58.234). These are local overhead measurements with scripted providers in sequential blocks, not live-provider speed or general framework superiority.

The surrounding canonical suite passed 78 tests, two skipped, one warning in 38.36 seconds. The comparison's timeout-retention regression also passes. Evidence: `bench/runtime/` artifacts `uat-runtime-main-profile-baseline-comparison.log`, `uat-runtime-main-profile-baseline-comparison-report.json` and `uat-runtime-main-profile-comparison-contract.log`.


### Ten-second model allowance: correctness passes, latency gate fails (2026-09-21)

The test-only ten-second model allowance completed thirty of thirty task-plus-calculation requests correctly, with one matching TODO and confirmed receipt each, no duplicate actions or post-return model work, and no sixty-second deadline failure. Existing selected fallbacks were exercised: nine primary calls, 52 MiMo calls and seven secondary DeepSeek calls. p50 was 10.711 seconds; p95 **30.046 seconds** (bootstrap interval 17.816–33.792). Two requests exceeded thirty seconds, so the strict latency gate fails; do not round this into a pass. The combined canonical/live command passed 79 tests, two skipped, one warning in 432.97 seconds.

The two slow requests each waited ten seconds on both primary and first fallback before a later fallback completed both turns. Eight provider calls ended without a result. The native $0.106894 estimate omits any unreported usage on interrupted calls and is not billing. The summary now explicitly counts such calls and marks incomplete cost coverage, even when fallback produces a successful result. Four accounting/retention tests pass. Earlier raw journals retain the original per-call evidence.

Artifacts: `bench/runtime/uat-runtime-catalogue-slice10-cohort.{log,jsonl}`, `uat-runtime-catalogue-slice10-cohort-report.json` and `uat-runtime-provider-cost-coverage.log`. A separate eight-second-per-model experiment is running, using the same full tool catalogue, selected model order and current product. This tests a larger remaining window for fallback/answer delivery; no timeout default or installed setting changes. In-progress journal: `/tmp/runtime-catalogue-slice8-cohort.jsonl`.


### Short-deadline experiments and shared provider health (2026-09-21)

Code inspection and a reproducing unit check show that caller-imposed eight/ten-second timeouts feed the ordinary shared model breaker. Two failures with a threshold of two open the circuit, which subsequent calls—including longer work—consult. The preceding ten-second screen's model mix must therefore not be interpreted as all requests independently attempting a healthy primary. Its earlier raw journals lack explicit breaker snapshots; the interaction is proven by the code/unit check, not asserted as an observed per-sample state trace.

Added a **test-only** `isolate_short_timeout_health` option, valid only with an experimental model allowance. It bypasses model blame for caller-induced `TimeoutError`, preserves native handling of other failures, respects explicitly lower per-call limits, and restores original functions after the fixture. Future live journals record breaker-open models before and after each request and the isolation setting. Two new contract tests passed in 2.43 seconds; 49 existing model-breaker and retry tests passed in 1.42 seconds. No product timeout or breaker implementation changed.

The running eight-second population still uses the original shared-breaker behavior loaded at its start. It will be retained in full; even a latency pass will not alone qualify a new product policy preserving long-goal model selection. A separate isolated-health live evaluation remains necessary before considering promotion. Logs: `bench/runtime/uat-runtime-isolated-timeout-health-contract.log` and `uat-runtime-short-timeout-breaker-contracts.log`.


### Model exhaustion after a saved task: failure reporting corrected (2026-09-21)

The eight-second shared-breaker experiment was stopped with SIGINT after fifteen completed attempts: twelve verified, three failed, one additional attempt started but unfinished, fourteen not started. Thirteen tasks were confirmed, with no duplicates. Seventeen finished provider calls lacked results; cost remains incomplete. Two failed requests created no task; another had a confirmed task but exhausted its remaining models. The run's persisted error incorrectly said it had stopped without doing the work. The experiment is rejected and must not be promoted or counted as a completed thirty-sample screen. Raw results remain in `bench/runtime/uat-runtime-catalogue-slice8-cohort*`.

A private native HTTP reproduction showed the incorrect no-work error can reach initial chat delivery. A diagnostic extra DB read made delivery recover the task, exposing a race: the terminal native result can arrive before its asynchronous run-row update. `chat_delivery` now permits the returned interrupted status only for the same authenticated run when its stored row is still pending/running, while obtaining action facts from durable receipts. Missing rows, mismatched runs and already-terminal records are not reinterpreted. The generic model-exhaustion message no longer asserts that no work occurred.

The native regression creates one real private task, deliberately returns no model answer on the next turn, and verifies initial chat, reconnect and history report the saved task while keeping the request failed and the additional calculation unconfirmed. It verifies one confirmed receipt, no duplicate write, no final-task checkpoint and two model invocations. Separate deterministic unit cases cover pending/running persistence races and rejected identity/terminal mismatches. Results: 34 unit tests passed in 2.68 seconds; five focused native cases passed in 5.40 seconds; full canonical integration 79 passed, two skipped, one warning in 34.76 seconds. Initial failure logs and diagnostic evidence remain under `bench/runtime/uat-runtime-provider-exhaustion-*`.

Broad regression and live qualification of this changed source remain open. Rollback `43b4af63073` predates this correction and needs compatibility/backport verification. No live provider experiment remains running; the timeout-health isolation option has not been live-qualified.


### Rollback supports initial saved-receipt delivery (2026-09-21)

The new private-storage compatibility probe rejects `43b4af63073` before daemon startup because it lacks initial saved-receipt delivery. Replacement `7324c4d031336a9c7145983b77386cabcfe3d9e8` backports the initial chat delivery integration and its shared receipt renderer, current effect receipt projection, neutral model-exhaustion wording, and the latest same-run pending-status correction. Its cumulative twenty-file patch from `b1671409892` is retained as `bench/runtime/rollback-chat-receipts-compatibility.patch`. Initial missing-module/helper integration failures are retained and were resolved before qualification.

The new gate runs each checkout in a fresh process against private canonical storage with a seeded confirmed note receipt and a still-running run row. A returned failed run must recover that receipt without modifying either durable state; another principal must not see it, and the generic failure must not assert no work occurred. Both current and new rollback targets pass. This is a controlled seeded-record contract, not a new claim of note creation; actual write/readback tests remain in the canonical suite.

Backport results: 34 focused tests passed in 1.34 seconds; 43 canonical tests passed, one skipped and one warning in 11.33 seconds. The current old-target rejection command passed its separate 79 canonical tests (two skipped, one warning, 35.91 seconds). The new target passed actual current → rollback → current daemon HTTP admission, saved reply recovery, stopped/effect state preservation and separate native admission; its surrounding current canonical suite passed 79 tests (two skipped, one warning, 46.57 seconds). All dependency pins match. No production action occurred.

Evidence: `bench/runtime/uat-runtime-rollback-chat-receipts-*`, including separate rejected-target and successful-target JSON reports. Current product `e988eb920b9` remains unchanged while its broad engine suite runs. Live qualification and broader performance work remain open.

The full engine regression for `e988eb920b9` completed: **11,448 passed, 29 skipped, 234 deselected, 393 warnings in 468.95 seconds**. Product source stayed fixed while separate rollback harness/backport work ran. Integration/slow tests were excluded and are recorded separately. Raw output: `bench/runtime/uat-runtime-provider-exhaustion-engine.log`. No live provider test is currently running; isolated-health performance evaluation and current-source live qualification remain open.


### Cloud-only allowance with isolated timeout health (2026-09-21)

Harness revision `229c7bd99e8` adds a cloud-only experimental allowance: `uses_ollama_timeout` models retain exactly their native timeout policy. The optional short-timeout health isolation applies only to models whose allowance is limited; genuine local timeouts and other provider failures still follow the normal breaker policy. Both mechanisms are process-local fixture patches and restore afterward. Seven model-allowance/reporting tests passed in 3.58 seconds. Log: `bench/runtime/uat-runtime-cloud-allowance-contracts.log`.

Started a separate thirty-request task-plus-calculation screen against unchanged product `e988eb920b9`: ten seconds per cloud model, full tool context, selected model order unchanged, local timing unchanged, short cloud TimeoutError excluded from shared health failures. The ordinary total deadline remains authoritative, including local fallback. Each journal sample records breaker state before/after. This is an experiment, not a production policy, and must preserve correctness plus the latency and cost-coverage gates. Events: `/tmp/runtime-cloud10-health-isolated-cohort.jsonl`; command output: `/tmp/runtime-cloud10-health-isolated-cohort.log`. No production configuration or business data is changed.
