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
| Pinned dependencies and one upgrade per finalist | Candidate lock, `upgrade-before.json`, `upgrade-after.json` | Public action/API upgrade drill completed; checkpoint upgrade parity is not proven |
| Identical model/prompt/tool/resource comparison | Comparator refuses mismatched/unknown configuration | Existing live native/candidate prompts differ. Those cohorts cannot establish comparative superiority |
| Correctness, ≥20% overall p95 improvement, no critical scenario >10% slower | Executable comparator gates, including per-scenario outcomes | No candidate has qualified; 100 matched repetitions per finalist still required before selection |
| Lowest qualifying p95, uncertainty/integration burden tie-break | Bootstrap measurement support | No qualifying finalists to rank; retain current runtime |
| New-session rollout, active checkpoint compatibility, rollback without replay | Native compatibility/stop-preservation contracts and documented admission rollback | Local contracts only. No production promotion or rollback was performed or authorized |
| Monthly compatibility / prompt security updates | `.github/workflows/runtime-contracts.yml` | Monthly/PR synthetic checks configured; a local run is not evidence that hosted CI or operational update review has run |

## Next acceptance work

The next user-facing case should be ordinary task work: show what remains unfinished,
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
