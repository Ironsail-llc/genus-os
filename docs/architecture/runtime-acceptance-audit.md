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
| Trusted identity / compatible checkpoints | Runtime context and envelopes; migration 127; runtime/checkpoint tests | Native contracts covered; alternate runtimes not yet equivalent |
| One execution loop / retry / compaction / delivery owner | Thin native adapter; native runner retains these responsibilities; deadline/retry tests | Covered natively, including nested deadline ownership |
| Shared family budgets / delegated work | `runtime/budget.py`, goal reservations and stale-worker tests | Covered for explicitly token-capped native goal families; unknown usage stays charged. Candidate provider-boundary wrappers and the durable attempt ledger pass concurrent reservation, reconstruction, cancellation, parent-limit and unknown-usage checks using a host-supplied bound; candidate goal-controller lifecycle binding now passes parent/child, capped/uncapped, uncertain-usage and shared native/candidate request tests; the host must still supply a conservative request bound |
| Draft versus execution / duplicate confirmation / ambiguous writes | Calendar operations and attendee suites; 100-sample native calendar cohorts | Covered for the supported calendar operation; not proof for every external business tool |
| Deterministic confirmation | `uat-deadline-calendar.json` | Zero model calls, verified single write and no later tools in all recorded confirmation samples |
| Completion without further model work | Native workflow-completion/output-validation tests and live host-contract cohorts | Covered when the host supplies an independent supported outcome predicate; arbitrary conversation is not independently verified |
| Bounded simple action | Explicit runtime-deadline contracts and deterministic-operation cap | Deadline enforced when supplied; blanket classification of all conversational requests into a 60-second policy remains unproven |
| Acknowledgement / progress / effective Stop | `uat-native-http.json`, real local TCP + native runner + durable private store | 30 acknowledgement/stop samples and two progress arrivals; not deployment-ingress p95 certification |
| Descendant controls / restart / stale workers | Runtime-control, goal-store, controller and checkpoint suites | Native local contracts covered; already dispatched effects still require reconciliation |
| Goal workspace: criteria, milestones, children, tasks, budgets, evidence, wake/review | Actual `GoalsView`, goal router/store, managed browser checks | Implemented and locally exercised; user acceptance still pending |
| Reviewable decomposition / steering / revisions | Goal API/store/model and browser tests | Explicit authorization retained; revisions invalidate prior evidence |
| Waiting without model polling / duplicate wakes / multi-day continuation | `test_runtime_multiday.py`, event/store/controller suites | Restored day-old waiting state wakes once and completes child then parent; simulated time, not a multi-day live deployment |
| Fresh recurring evidence / independent versus judgment-based outcomes | Goal evidence tests and browser approval/revision scenarios | Supported calendar receipts checked independently; broader judgment requires operator review |
| Tenant isolation / delegated controls / memory | Native engine and goal suites, including RLS and tenant/control tests | Native behavior tested; no equivalent candidate evidence |
| Provider fallback | Native injected-primary-failure verified-outcome test | One verified write after fallback and no post-success calls; real configured-chain outage/load behavior remains unqualified |
| Everyday requests competing with goals | `uat-mixed-runtime-final.jsonl` | 1,560 verified synthetic actions across 1/5/20 tenants; local resource limits published, not production capacity |
| No introduced regressions | Latest broad native run at `b68204804a5`: 10,982 passed; goals: 69 passed. Latest frontend: 1,629 passed; scopes and warnings in `uat-verification.json` | No observed regressions in those scopes. This does not prove every possible workflow or constitute manual acceptance |

## Alternative runtimes and rollout

| Requirement | Current evidence | Finding |
|---|---|---|
| Bounded Pydantic AI / Deep Agents integrations through public APIs | `bench/runtime/candidates.py`, isolated pins and action screening | Action adapters now have a shared request/result/control bridge tested with an explicit host; run/control-store host tested with real migrations; ordinary manifest/tool/memory admission and checkpoint resumption remain incomplete; goal-controller lifecycle accounting is now exercised end to end |
| OpenCode assessment without a permanent fork | `bench/runtime/opencode/result.json` | Public SDK serialization/abort transport tested; server enforcement, budget admission and recovery remain unverified |
| Candidate tools through native admission and dispatch | `test_native_dispatch.py`, prepared `NativeGateway`, private run/step/receipt tables | Both frameworks exercise native gates and durable step recording; duplicate proposals stop after verified success; uncertain step acknowledgement stops queued work. Native manifest/memory bootstrap, interactive approval lifecycle and general external-write reconciliation remain incomplete |
| Pinned dependencies and one upgrade per finalist | Candidate lock, `upgrade-before.json`, `upgrade-after.json` | Public action/API upgrade drill completed; checkpoint upgrade parity is not proven |
| Identical model/prompt/tool/resource comparison | Comparator refuses mismatched/unknown configuration | Existing live native/candidate prompts differ. Those cohorts cannot establish comparative superiority |
| Correctness, ≥20% overall p95 improvement, no critical scenario >10% slower | Executable comparator gates, including per-scenario outcomes | No candidate has qualified; 100 matched repetitions per finalist still required before selection |
| Lowest qualifying p95, uncertainty/integration burden tie-break | Bootstrap measurement support | No qualifying finalists to rank; retain current runtime |
| New-session rollout, active checkpoint compatibility, rollback without replay | Native compatibility/stop-preservation contracts and documented admission rollback | Local contracts only. No production promotion or rollback was performed or authorized |
| Monthly compatibility / prompt security updates | `.github/workflows/runtime-contracts.yml` | Monthly/PR synthetic checks configured; a local run is not evidence that hosted CI or operational update review has run |

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

For runtime selection, the next implementation work is shared-contract candidate
integration and matched configuration capture. Further unmatched smoke repetitions
cannot close those gaps. There is no justification to replace the current engine from
the existing measurements. The local MiMo deadline failure is preserved as an incomplete
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

The reproducible command `.venv/bin/python -m bench.runtime.migrated_integration` initializes a disposable PostgreSQL 16 cluster with pgvector, UTF-8 and Unix-socket-only access, applies the full canonical chain (150 migrations), verifies a repeated apply is a no-op, then runs the actual identity and person/timeline integration modules. All 13 tests passed (0.55s), resolving the six prior schema-related failures on a compatible isolated database. The shared test schema was not changed. The first harness attempt used initdb's SQL_ASCII default and failed to encode migration SQL; explicitly selecting UTF-8 corrected that setup issue.

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
