# Runtime acceptance audit

Audited locally on 2026-09-20, against accepted integration revision `eea3252b15`.
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
| Lost chat delivery / automatic audit recovery | Scoped original-request recovery, calendar readback and native continuation tests; `uat-native-plan-browser.json` | Saved-plan recovery now passes through the real browser, Next proxies, native engine and private canonical DB without another preparation or approval; other external effects still need provider-specific checks |
| Failed draft / saved-plan readiness | `uat-plan-exploration-outcomes.json`, strict draft persistence and matching-plan recovery tests | Failed, timed-out and cancelled exploration cannot publish a partial plan; only a matching pending, unexpired saved draft is restored |
| Deterministic confirmation | `uat-deadline-calendar.json` | Zero model calls, verified single write and no later tools in all recorded confirmation samples |
| Completion without further model work | Native workflow-completion/output-validation tests and live host-contract cohorts | Covered when the host supplies an independent supported outcome predicate; arbitrary conversation is not independently verified |
| Bounded simple action | Explicit runtime-deadline contracts and deterministic-operation cap | Trusted deadlines bound admission and execution; eligible deterministic calendar confirmations receive a 60-second cap. Blanket classification of all conversational requests remains unproven |
| Acknowledgement / progress / effective Stop | `uat-native-http.json`, real local TCP + native runner + durable private store | 30 acknowledgement/stop samples and two progress arrivals; not deployment-ingress p95 certification |
| Descendant controls / restart / stale workers | Real daemon goal crash/restart, scoped cleanup, continuation Stop inheritance, exclusive startup claims and database-session-loss admission checks | Synthetic native cases pass; network partitions and production failover remain unqualified. Already dispatched effects require reconciliation |
| Goal workspace: criteria, milestones, children, tasks, budgets, evidence, wake/review | Actual `GoalsView`, goal router/store, managed browser checks | Implemented and locally exercised; user acceptance still pending |
| Reviewable decomposition / steering / revisions | Goal API/store/model and browser tests | Explicit authorization retained; revisions invalidate prior evidence |
| Waiting without model polling / duplicate wakes / multi-day continuation | `test_runtime_multiday.py`, event/store/controller suites | Restored day-old waiting state wakes once and completes child then parent; simulated time, not a multi-day live deployment |
| Fresh recurring evidence / independent versus judgment-based outcomes | Goal evidence tests and browser approval/revision scenarios | Supported calendar receipts checked independently; broader judgment requires operator review |
| Tenant isolation / delegated controls / memory | Native engine and goal suites, including RLS and tenant/control tests | Native behavior tested; no equivalent candidate evidence |
| Provider fallback | Native injected-primary-failure verified-outcome test | One verified write after fallback and no post-success calls; real configured-chain outage/load behavior remains unqualified |
| Everyday requests competing with goals | `uat-mixed-runtime-final.jsonl` | 1,560 verified synthetic actions across 1/5/20 tenants; local resource limits published, not production capacity |
| No introduced regressions | At `90617b97d52`: broad engine 11,126 passed, zero failed. Latest frontend 1,661 passed; canonical native/browser 33 passed. Prior goal results, scopes and failure logs retained in `uat-verification.json` | The broad engine invocation excludes slow/integration/LLM/e2e/smoke tests; selected canonical integrations are verified separately. Existing network coroutine warning remains; these scopes do not establish full acceptance |

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
| New-session rollout, active checkpoint compatibility, rollback without replay | Native compatibility/stop-preservation contracts and documented admission rollback | Private canonical fresh/populated schema upgrades and real daemon restart drills preserve stopped work, paused goals, checkpoints and uncertain operations. Synthetic full-daemon active-goal recovery and native saved-message continuation pass. Application rollback remains unverified. No production promotion or rollback was performed or authorized |
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
