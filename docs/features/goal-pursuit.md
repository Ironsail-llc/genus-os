# Operator goal pursuit

Goal pursuit is opt-in per tenant. Short-term goals continue across runs until
verified complete, blocked, paused, canceled, or budget-limited. Long-term goals
coordinate short-term execution children, existing CRM tasks, dates and event
watches. Ongoing goals assess a target repeatedly instead of completing.

## Enable and use

Apply the canonical migration chain, including `crm/migrations/126_goal_pursuit.sql`, before
running the new engine or bridge. No existing session goal starts executing as a
result of migration. The tenant switch starts disabled.

126 is not a hard dependency of anything else. Without it the engine runs
exactly as it did before goal pursuit existed: the agent task inbox and the
thread claim keep returning tasks, and the feature reports itself off. The same
holds after rolling 126 back. The check is a single probe per process, so the
task inbox pays nothing per query for it.

The Goals view supports creation, execution enable/disable, progress inspection,
pause/resume, cancellation, steering and completion approval. Chat has four tools:
`create_pursuit_goal`, `get_pursuit_goal`, `list_pursuit_goals`, and
`update_pursuit_goal`. Main owns these goals; specialists execute delegated work.
Create goals only on explicit operator requests or as execution children of an
already authorized long-term goal. Tool permissions and action approvals remain
in force.

All four tools require a verified owner or admin on the run. An unattended run —
cron, heartbeat, scheduled: the runs that read inbound email — carries no
identity and is refused, so content the agent reads cannot create, steer or
cancel a goal. The single exception is the goal executor itself, which is
already running an authorized goal and is recognised by that goal's lease
rather than by a role. Autonomous goal creation is not available; if it is ever
wanted it will be a per-tenant setting an operator turns on.

CLI examples:

```sh
genus goals --tenant example enable
genus goals --tenant example create "Prepare the report" --criterion "Report delivered and receipt verified"
genus goals --tenant example create "Keep reports current" --kind long --mode ongoing --criterion "Latest report covers the previous day" --review-seconds 86400
genus goals --tenant example list
genus goals --tenant example get GOAL_ID
genus goals --tenant example pause GOAL_ID
genus goals --tenant example create "Prepare the report" --criterion "Report delivered" --token-budget 200000 --cost-budget-usd 2 --max-attempts 20 --deadline-seconds 604800
genus goals --tenant example resume GOAL_ID --token-budget 200000
genus goals --tenant example update GOAL_ID --file update.json
```

`update.json` is a `GoalUpdate` object, including the current `version`. For example:

```json
{"action":"wait","version":4,"note":"Waiting for the reply","event_type":"email.new","event_match":{"thread_id":"abc"},"wake_at":"2030-01-02T14:00:00Z"}
```

API: `GET/POST /api/goals`, `GET/PATCH /api/goals/{id}`,
`PATCH /api/goals/settings`, and `POST /api/goals/adopt`. These routes require a
verified owner/admin in the request tenant, and reject service credentials.
Creation accepts objective, criteria, kind (`short`/`long`), mode
(`finite`/`ongoing`), optional parent, token budget, human review, review interval,
priority and idempotency `request_key`. Reusing a request key returns the existing
goal. Automatic child keys include the parent, criteria and ongoing assessment
period; use an explicit different key for intentionally distinct repeat work.

Updates support progress, evidence, wait, block, complete, assess, pause, resume,
cancel, approve, steer, revise, link_task, and reconciled. Evidence identifies a
zero-based criterion, reference, explanation and `satisfied` assessment. Latest
assessment for each criterion controls completion; tests/commits are not required
for non-coding goals. `revise` is operator-only and clears old criterion evidence;
the previous contract remains in history. `resume`, `approve`, and `steer` are also
operator controls. Autonomous creation and lifecycle tools do not enable the
execution switch.

Legacy session/performance tools remain available, clearly labeled as legacy.
`genus goal` and its explicit alias `genus legacy-goal` preserve their old behavior.
`genus goals adopt LEGACY_TASK_ID` explicitly enrolls a legacy objective, preserving
its source/evidence for reassessment. Adoption is idempotent and does not mutate
the source record.

## Execution and recovery

The scheduler owns a background controller. It polls for newly ready work every
second and continues immediately after each coordination run. One durable lease
per tenant prevents concurrent goal coordinators, including across processes.
Priority then oldest-ready ordering rotates work at run boundaries. Waiting goals
consume no model calls. The ordinary task inbox excludes work owned by inactive
goals. Pausing a parent pauses its children; resuming restores the children it
paused. A running tool cannot be undone; cancellation stops subsequent work.

Goal tool admission checks the lease and lifecycle state. A recovered execution
must inspect prior tool results and external state and record `reconciled` before
mutating tools are allowed. CRM tasks created within an execution are linked in
the same transaction and deduplicated across retries by goal, title, body and
assignee. Execution children also deduplicate. External actions retain their
existing idempotency contracts; the recovery gate does not claim exactly-once
external execution.

Leases renew every five seconds and expire after 90 seconds. Usage snapshots are
saved with lease renewals. A recovered lease bills its saved usage once and
preserves a reconciliation requirement. Token budgets cover coordinator runs,
spawned runs, and execution children; separately scheduled CRM tasks keep their
existing task budgets. Token limits are checked at iteration/tool boundaries;
already in-flight model calls can overshoot. System hard caps also stop pursuit.

### Ceilings

Every goal is created with four ceilings, whether or not the caller asks for
them. Leaving one unset means the platform default, never "unlimited":

| Ceiling | Default | Why |
|---------|---------|-----|
| Token budget | 1,000,000 tokens | Two to four dozen coordination runs — room to work a problem, not a week's spend. |
| Cost ceiling | $5.00 | Tokens are not cost: model prices differ by two orders of magnitude. |
| Maximum runs | 50 | Bounds the goal that loops while spending almost nothing per run. |
| Deadline | 30 days | A backstop for the forgotten goal, not a work limit. |

Each is overridable per goal at creation (`--token-budget`, `--cost-budget-usd`,
`--max-attempts`, `--deadline-seconds`, the matching `CreateGoal` fields, and the
Goals form), and raisable on `resume`. Reaching one blocks the goal with a
blocker naming the ceiling and the numbers; `resume` refuses until the ceiling
that stopped it is raised. Blocking on a ceiling is a request for an operator
decision, never a claim that the goal succeeded or failed.

Coordination runs are paced: at most one run every 30 seconds per tenant. A
single ready goal can no longer hold the controller loop, and a parent cannot
spin turns while its execution child waits for the lease.

Three consecutive repeated blocker reports stop automatic pursuit. Three runs
without a new progress checkpoint or evidence also block — but note that the
pursuit prompt asks for a progress note every run, so that guard resets on the
behaviour it asks for and is not a substitute for the ceilings above. Technical
failures retry with bounded backoff and block after three failed runs. Limits
never imply success.
A finite goal cannot complete with unfinished execution children. Ongoing
assessments require fresh evidence for each period and have a default daily review.

Task-change wake records commit in the same PostgreSQL transaction as task updates.
Existing Redis streams are replayed into a durable inbox with transactional
per-tenant cursors and idempotent event IDs. Only explicitly tenant-scoped events
can match a watch. Event capture runs at most once a minute between coordination
turns. No new external watcher integrations are installed. Every wait has a timed
fallback, covering missing or trimmed stream events. Duplicate wakes coalesce,
old events cannot satisfy newly registered watches, and paused goals cannot be
restarted by events. A missed recurring interval causes one current assessment.

## Operations and validation

Inspect goal history and attempts for dispatches, wake events, recovery, retries,
usage and lifecycle changes. Goal history records who changed the objective or
criteria. Important completion, review, blocker, promotion and assessment changes
also enter Main's existing notification inbox. Disabling execution prevents new
dispatches and stops in-flight pursuit; history stays available.

Run `pytest robothor/goals/tests` for lifecycle, controller, HTTP, tool, event and
transaction tests. PostgreSQL tests launch and stop their own temporary cluster
on a private Unix socket; they do not connect to the live database. UI tests live
in `app/__tests__/components/goals-view.test.tsx`.

Roll out by applying the migration, deploying engine/bridge/UI with execution
disabled, then enabling one tenant. Disabling the switch is the runtime rollback;
retain the additive tables and history. Apply code rollback only with execution
disabled. No production migration, service restart or tenant activation is part of
the development/test workflow.
