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
| No introduced regressions | Latest broad native run: 10,888 passed; frontend: 1,617 passed; subsequent focused checks recorded in `uat-verification.json` | No observed regressions in those scopes. This does not prove every possible workflow or constitute manual acceptance |

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
