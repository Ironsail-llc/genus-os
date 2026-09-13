# Runbook — Benchmark sandbox fixtures

> **Scope.** The flags, the ladder and the fixture rules are platform: every
> instance that runs the benchmark harness needs them. The dated scores are
> measurements from the first instance. Purely single-machine runbooks live in
> `docs/instance/` in the repository.

**Flags:** `ROBOTHOR_BENCHMARK_SANDBOX_ENABLED` + `ROBOTHOR_BENCHMARK_SANDBOX_MODE`
**Ladder:** `off` → `observe` → `alert` → `enforce`
**Owner:** ops · **Manifest:** `infra/flags.yaml` · **Live values:** `infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf`

## What this fixes

`crm-hygiene` scored **0.1818** on 2026-08-21 (0 of 4 cases passed; 7-day mean
0.3454) on a suite it **could not pass**. Its rubrics demanded an action —
"takes a scrub/flag/deactivate action", "cleans or flags the phone field",
"acts rather than leaving it open" — while the harness intersected the
sub-agent's tools down to a read-only allow-list that denied `update_person`,
`create_task`, `update_task` and `resolve_task`. The records the prompts named
did not exist either: `crm_people.id` is a `uuid`, so `p-9999` is not
representable at all, and the "200 stale TODOs" were 2.

The only way to score was to narrate an action the agent was forbidden to
perform. That is a fabrication trainer, and it carried weight 5.0 on the
agent's `passes-its-job` quality goal.

An earlier attempt prepended `[BENCHMARK DATA] Person p-9999 exists…` to the
prompt. That is worse, not better: it rewards accepting an asserted-but-false
premise. It was reverted. **Seed real rows instead.**

## How it works

1. **Sandbox tenant** — `benchmark-sandbox` (migration `102`), no parent, no
   child data access. `crm_people.tenant_id` / `crm_tasks.tenant_id` are FKs to
   `crm_tenants(id)`, so the row must exist before anything can be seeded;
   `benchmark_sandbox.ensure_sandbox_tenant()` re-creates it idempotently so a
   fresh instance works before the migration runs.
2. **Fixtures** — `docs/benchmarks/<agent>/fixtures.yaml`, loaded next to the
   suite. Each task names the fixture keys it needs; those rows are written
   before the run and the prompt interpolates their real uuids via
   `{{fixture.<key>.id}}`.
3. **Tool split** — `EXTERNAL_SIDE_EFFECT_TOOLS` (mail, calendar, `exec`,
   `invoke_skill`, `write_file`, notifications, spawn, browser, desktop, memory
   writes) are denied in **every** mode, for ever. `SANDBOX_WRITE_TOOLS`
   (create/update person, company, note, task; `resolve_task`) are allowed
   **only** while the run is scoped to the sandbox tenant. Deletes and merges
   are never allowed: they are irreversible, and "never deletes" is what the
   hygiene suite grades.
4. **State checks** — `expected.state_checks` read the sandbox database back
   after the run: `row_present`, `field_equals`, `field_changed`,
   `field_matches`, `field_not_matches`, `rows_match`. An unknown kind or a
   checker error scores as a **failure**, never a pass.
5. **Teardown** — every row in the sandbox tenant is deleted when the task
   ends, in a `finally` block, so a timeout or crash cannot leave last night's
   state to be graded tonight.

## The ladder (read this before promoting)

This ladder is not the usual bookkeeping one.

| Mode | Runs as sandbox tenant | Seeds fixtures | Sandbox CRM writes | State checks | Score |
|------|------------------------|----------------|--------------------|--------------|-------|
| `off` | no | no | no | no | unchanged (today's harness) |
| `observe` | **every task run** | suites that declare them | yes | recorded on the task result | **unchanged** |
| `alert` | every task run | suites that declare them | yes | recorded + error-logged | unchanged |
| `enforce` | every task run | suites that declare them | yes | recorded | **folded into the task score** |

`observe` already changes what a benchmark sub-agent can **do** — that is the
defect being fixed. What it does not change is how the run is **graded**.

### Every task run is the sandbox tenant, and names its parent

With the sandbox on, **every** task run the harness launches executes as the
sandbox tenant (`_suite_execution_tenant`, resolved once per suite) and is
linked to the benchmark runner's own run through a `SpawnContext`, whatever the
suite declares — unless the task or suite declares
`execution_tenant: production-read-only`, see below. Seeding and tenancy are
separate decisions: a suite with no `fixtures.yaml` seeds nothing and still runs
in an empty sandbox, which is the right environment for suites that state their
scenario inside the prompt and the required one for the fleet honesty cases,
whose `missing_record` case is only valid while the record really is absent.
Teardown sweeps the sandbox after every task, seeded or not, because a
fixture-less child can still file its own CRM rows there, and one suite at a
time holds the tenant (`sandbox_suite_lock`) so a concurrent suite cannot sweep
another's fixtures mid-task. The grade ledger does **not** move: the
`benchmark_results` row is written after the tenant scope closes.

### Suites that cannot be graded in an empty tenant

An empty tenant is not neutral for every case, and the first version of this
change claimed it was. That claim came from scanning `must_contain` /
`must_not_contain` keyword lists for numeric literals — which misses the two
places the dependency actually lives: **LLM judge rubrics**, and tools that read
tenant-scoped data (`search_memory`, `get_agent_stats`, `get_knowledge_gaps`,
and the tenant-scoped `agent_memory_blocks`). Seven tasks across three suites
grade an agent on reading the instance's own data:

| Task | What it reads | Why an empty sandbox breaks it |
|---|---|---|
| `main::memory-recall` | `search_memory` | graded `must_not_contain: ["no information\|don't know\|cannot find"]` — the honest answer scores 0 |
| `agent-architect::fleet-analysis` | `get_agent_stats`, `performance_baselines`, `architect_evolution_log` | rubric demands stats for 3 underperforming agents; the sandbox has zero runs and zero blocks |
| `agent-architect::cross-pollination` | `autoagent_learnings` block | rubric demands a specific pattern read out of it |
| `curiosity-engine::basic-gap-analysis` | `analyze_knowledge_gaps()` over `memory_entities` | zero entities ⇒ zero gaps; the rubric requires several |
| `curiosity-engine::efficiency-completion` | same | `must_contain: [finding, gap]` cannot be met honestly |
| `curiosity-engine::dedup-prior-findings` | prior-findings block | an empty block makes every topic novel; the case stops measuring dedup |
| `curiosity-engine::safety-store-concrete` | same gap analysis | no gap to find, so concreteness is never exercised |

Each declares `execution_tenant: production-read-only` **on the task**, not the
suite: `curiosity-engine::session-goal-alignment` seeds a fixture and the
`_honesty` cases positively require an empty tenant, so a suite-wide flip would
have broken them. Those cases run under the owning tenant with the deny-list and
the write boundary armed exactly as when the sandbox is off — their reads are
real, and none of their writes can land. That is today's behaviour for them, so
their grades do not move.

`agent-architect::cross-pollination` is the one case neither posture fits: its
rubric needs a tenant-scoped block AND a created CRM task. It keeps
`production-read-only` (today's grade exactly), and its `state_checks`
consequently do not run — the harness logs a WARNING per task when that
happens, and `TestShippedSuitePostures.STATE_CHECKS_GO_INERT` names it so the
trade stays declared rather than discovered.

> **Follow-up — `cross-pollination`'s read-backs are inert.**
> Fixing it properly needs a **memory-block fixture**: something that seeds an
> `autoagent_learnings` block into the sandbox tenant the way `fixtures.yaml`
> seeds CRM rows. The fixture system writes CRM tables only
> (`SEEDABLE_COLUMNS`, `_SWEEP_ORDER`), so this is a feature, not a config
> change — and the sweep has to learn to delete `agent_memory_blocks` in the
> sandbox before anything seeds one there, or the block outlives its task and
> the next night grades last night's fiction. Until then the case runs
> `production-read-only` with its `state_checks` skipped, which is exactly what
> it did before the sandbox existed.

**Adding a posture is not free.** An opted-out task is a task the sandbox is not
protecting. The posture is validated on load — an unknown value fails the suite
with `success: false` rather than silently grading against production — and
`test_benchmark_child_tenant.py::TestShippedSuitePostures` fails the build if
the shipped set drifts from the audited one.

This used to depend on the suite: the sandbox was reached only as a side effect
of seeding fixtures, so a suite declaring neither `fixtures:` nor
`state_checks:` ran as the graded agent's own tenant. In the 2026-09-13 fleet
benchmark that was 75 of 78 task runs. The write boundary refused 60 writes on
the way past so nothing leaked, but the runs were still *attributed* to
production and their DAL reads ran against production rows with fixture
identifiers (`get_person {"id": "bob.quill@example.com"}` → an
`InvalidTextRepresentation` from a uuid comparison).

Lineage is not gated on the decontamination rollout — excluding benchmark
traffic from production *metrics* is a judgement, recording which run spawned
which is a fact. Audit a night by parent, not by time window.

### Which connection you run the audit on decides what it can see

Read this before running any query in this runbook. Migration `081` puts a
`tenant_isolation` policy on **every** table with a `tenant_id` column —
`agent_runs` included — with `FORCE ROW LEVEL SECURITY`, so the table owner is
subject to it too. The policy applies to each *reference* in a query, both
aliases of a self-join included. The parent run lives in the owning tenant and
its children live in `benchmark-sandbox`, so a self-join sees them both only
from a connection that is bound to neither.

Run the audit the way the operator runs a migration: as the ledger owner, on a
connection with **no tenant binding** (`psql` as the migration role, or
`SELECT set_config('app.tenant_id', '', false);` first). The policy's
permissive branch is an empty `app.tenant_id`, which is also what
`_apply_tenant_scope` writes when there is no scope to bind.

```sql
-- UNBOUND (psql as the ledger owner, app.tenant_id empty).
-- Every task run of last night's benchmark, and the tenant it ran as.
SELECT child.agent_id, child.tenant_id, count(*)
  FROM agent_runs child
  JOIN agent_runs parent ON parent.id = child.parent_run_id
 WHERE parent.agent_id = 'benchmark-runner'
   AND parent.started_at > now() - interval '1 day'
 GROUP BY 1, 2;
-- PASS: every row reads tenant_id = 'benchmark-sandbox'
--       (plus the production-read-only suites, which read the owning tenant
--        by design — see "Suites that cannot be graded in an empty tenant").
-- FAIL: any other tenant_id.
```

From an engine connection you cannot get that answer, and the query that looks
like it should is the one that misleads: bound to the **owning** tenant the
join drops every sandbox child, so a clean night and a blind query both return
nothing. Bound to the owning tenant, ask the opposite question — and read an
empty result as the pass:

```sql
-- BOUND to the owning tenant (an ordinary engine connection, RLS on).
-- Parent joined by id only; a sandbox child is invisible here BY DESIGN,
-- so this can only ever return a child that ran in YOUR tenant.
-- The declared production-read-only tasks belong here, so they are excluded
-- by the posture the harness records on the run itself.
SELECT child.agent_id, child.tenant_id, count(*)
  FROM agent_runs child
  JOIN agent_runs parent ON parent.id = child.parent_run_id
 WHERE parent.agent_id = 'benchmark-runner'
   AND parent.started_at > now() - interval '1 day'
   AND child.trigger_detail LIKE 'benchmark:%'
   AND child.trigger_detail NOT LIKE '%:production-read-only'
 GROUP BY 1, 2;
-- PASS: EMPTY — the only children in this tenant are the declared read-only
--       ones, and they are filtered out above.
-- FAIL: any row — that child ran in the tenant you are bound to and did not
--       declare that it needed to.
```

Empty is only the pass for the *bound* form. If the unbound query also returns
nothing, the benchmark did not run — check the schedule, not the isolation.

The exclusion is not a blind spot: check the set it removes against the table
in "Suites that cannot be graded in an empty tenant", from the same bound
connection.

```sql
-- DECLARED production-read-only children of last night's benchmark.
-- Run this from the SAME bound connection as the query above.
SELECT child.agent_id, child.tenant_id, count(*)
  FROM agent_runs child
  JOIN agent_runs parent ON parent.id = child.parent_run_id
 WHERE parent.agent_id = 'benchmark-runner'
   AND parent.started_at > now() - interval '1 day'
   AND child.trigger_detail LIKE '%:production-read-only'
 GROUP BY 1, 2;
-- PASS: exactly the agents in the production-read-only table — today
--       main, agent-architect and curiosity-engine. Anything else is a suite
--       that opted out without anyone auditing it.
```

The posture is recorded as a suffix on the child's `trigger_detail`
(`benchmark:<suite>:<task>:production-read-only`, written by
`_task_trigger_detail`), because a declared read-only child is otherwise
indistinguishable from a leak: same tenant, same `benchmark:` prefix, same
parent. Before it existed this query reported the seven shipped opt-outs as
leaks every night — and a promotion gate that cries wolf nightly is one the
operator stops reading.

## What a benchmark run may touch

Read this before changing anything in the harness. The one-line rule:

> **A benchmark run may write exactly one tenant — `benchmark-sandbox` — and
> writes nothing at all when the sandbox is off.**

That is now true by construction rather than by convention, and it is enforced
at three depths. Each one alone has already failed in production:

| Depth | What it is | What it catches |
|-------|-----------|-----------------|
| Tool allow-list | `benchmark_allowed_tools()` intersected into the child's `tools_denied` | the graded agent never sees the tool |
| Handler guard | `ctx.is_benchmark` in `handlers/crm.py`, `memory.py`, `gws.py`, `vision.py` | a tool reached some other way — a skill, a force-added tool |
| Write boundary | `run_context.benchmark_write_refused()` in `memory/facts.py`, `memory/blocks.py`, `memory/write_jobs.py`, `memory/outcomes.py` | a write that never passes a tool at all |

Two rules that are easy to get wrong when editing any of the three:

* **The deny-list fails closed.** If the registry cannot be enumerated,
  `_every_registered_tool()` falls back to every name the static sets know —
  never to the empty set. An empty deny-list is the original defect, and the
  first version of this remedy could reach it through its own error path.
* **Adapter / MCP tools are always denied.** They are dispatched to their MCP
  session *before* `ToolContext` exists, so no handler guard can ever see the
  call and the write boundary is in another process. The deny-list is the only
  place they can be stopped, so they are denied whatever the manifest grants.

### A read tool that writes

`search_memory` stays available to a graded child — the suites need it — and it
writes one `fact_access_log` row per consulted fact. Those rows are the only
input to `fact_access_rollup` and hence to the memory decay scorer, so a
benchmark run was quietly steering fact retention for eleven days. The tool
keeps its place; `memory/outcomes.py` asks the boundary instead. Such tools are
listed explicitly in `BOUNDARY_GUARDED_TOOLS`, which the derivation test
subtracts — adding a name there re-opens a write path, so nothing belongs in it
without a test proving its guard.

### What is still outside the boundary

* **Shell hooks** (`hook_registry._run_command`) are a separate process. Sync
  Python hooks *are* covered: `_run_in_executor` copies the context, because
  `loop.run_in_executor` — unlike `asyncio.to_thread` — does not.
* **Out-of-process MCP callers** reaching `robothor/api/mcp.py` directly. They
  carry no run context, so the boundary cannot see them.

### Incident 2026-09-12 — why the third depth exists

With the sandbox `off` this instance ran ~220 graded child runs over eleven
days under its own tenant. The CRM deny-set held. The memory path did not: a
suite fixture's fictional person became 25 `memory_facts` rows, a real agent
recalled them as established fact, and from them created a production person
record and eight production tasks. The operator found fictional people in his
CRM.

Three things were true at once, and all three are fixed:

1. the allow-list is computed as `agent.tools_allowed - allowed`, and an agent
   whose manifest lists no `tools_allowed` has an **empty** deny-list — so the
   least restricted agents were restricted least as benchmark children;
2. `log_interaction`, `leave_breadcrumb`, `record_procedure` and
   `report_procedure_outcome` had no handler guard at all;
3. nothing below the tools asked whose tenant it was writing.

### The two settings

| Setting | Effect |
|---------|--------|
| `ROBOTHOR_BENCHMARK_SANDBOX_ENABLED` | `0`/unset pins the mode to `off` whatever the mode says |
| `ROBOTHOR_BENCHMARK_SANDBOX_MODE` | `off` → `observe` → `alert` → `enforce` |

**With the sandbox off, a benchmark is read-only by construction.** The child
still runs under the graded agent's tenant — that has not changed — but every
durable write is refused: a structured `benchmark sandbox: <tool> writes are
disabled` result from the handler, or a dropped write and one content-free
WARNING from the boundary. A refused tool is a tool *result*, so the run
completes and is graded on what it did with the refusal.

**With the sandbox on**, the CRM writes in `SANDBOX_WRITE_TOOLS` are re-allowed
and only inside the sandbox tenant. Memory writes are **not** re-allowed in any
mode (`MEMORY_WRITE_TOOLS` ⊂ `EXTERNAL_SIDE_EFFECT_TOOLS`): teardown sweeps CRM
tables, so a fact written under the sandbox tenant would outlive the task and
be recalled by the next night's run — the same self-reinforcing fiction with a
smaller blast radius rather than a fixed one. No suite grades a memory write.

### Alerts from a benchmark child

They do not page, and they do not enter the operator's inbox. `alerts.alert()`
routes anything raised inside a graded run to a `benchmark_digest` notification
row with `[benchmark]` on the subject; the heartbeat's alert reader
(`warmup.ALERT_DIGEST_TYPES`) does not read that type, and `get_agent_inbox`
excludes it unless asked for by name. The rows are still written — a suite that
trips the runaway-token guard nightly is a real finding about the suite, just
not an interrupt. Read them with
`get_inbox(typeFilter="benchmark_digest")`.

Two cases need more than the current task's context:

* **Out-of-band detectors** (`detectors.py`) run on the daemon's loop and alert
  *about* a run they are not inside, so `in_benchmark_run()` is False there
  however benchmark the subject is. They call `alerts.alert_about_run(...)` and
  pass the subject run's `trigger_detail`, so the routing follows the subject.
  A test fails the build if any detector calls `alert()` directly again.
* **The soft-runaway batch** (`runner._soft_runaway_pending`) is module-global
  and flushed by whichever run crosses next. A benchmark child flushing a batch
  of *production* crossings would have relabelled the whole summary
  `benchmark_digest` and lost a real page, so graded runs never enter the batch
  at all — they report immediately, into their own digest.

### Checking an instance

`genus doctor` runs `benchmark.isolation`: it fails when an enabled schedule
belongs to an agent that can call the benchmark tools while the sandbox mode is
`off`.

## Promotion evidence (required before `enforce`)

**Run ONE suite before the fleet, and make it one of the three the audit
touched** — `main`, `agent-architect` or `curiosity-engine`. They are the
suites whose grades are most sensitive to the tenant their children read, and
a per-task posture that is wrong shows up there first. Compare the per-task
scores against the previous night's for the same suite: any task that moved and
is not in the table under "Suites that cannot be graded in an empty tenant" is
a finding, not noise.

1. One full fleet night with `state_checks` recorded on `crm-hygiene`'s task
   results, hand-compared against the transcripts. Specifically: a task the
   agent genuinely completed must show **passing** read-backs. Zero passing
   checks means the mechanism is inert, not that the agent is bad — that is the
   "PROBE, don't trust silence" failure this instance keeps re-learning.
2. `crm_people` / `crm_tasks` row counts in `benchmark-sandbox` back at **zero**
   after each run (teardown works). **Unbound connection** — bound to the
   owning tenant these counts are zero whether teardown works or not:
   ```sql
   SELECT 'people', count(*) FROM crm_people WHERE tenant_id = 'benchmark-sandbox'
   UNION ALL
   SELECT 'tasks',  count(*) FROM crm_tasks  WHERE tenant_id = 'benchmark-sandbox';
   ```
3. Zero rows written to any other tenant by a benchmark run — walk the night by
   `parent_run_id` (the lineage queries above, on the binding each one names;
   there is no `is_benchmark` column, the flag lives on the run object only, and
   `trigger_detail LIKE 'benchmark:%'` is the fallback for a detached harness
   that invented its own label) and confirm the CRM audit log shows no mutation
   outside `benchmark-sandbox`. With the sandbox on, every child row of a
   `sandbox`-posture suite must itself read `tenant_id = 'benchmark-sandbox'`.

### What moves on the operator's surfaces the night you turn this on

Expect these, and do not read them as regressions:

* **Benchmark spend is no longer scoped to the owning tenant.** The children
  execute as `benchmark-sandbox`, so the `benchmark_runs` /
  `benchmark_cost_usd` break-out on `/costs`, in fleet health and in
  `analytics.get_agent_stats` reads `agent_runs` without a tenant predicate and
  relaxes its own transaction's RLS binding to do it
  (`db.connection.read_every_tenant_in_transaction`). Without both halves those
  numbers go to **0.0** — roughly $30/month on this instance — and
  `analytics._report_contamination` early-returns on a zero count, silencing
  the decontamination rollout's `observe` and `alert` rungs. If you see zero
  benchmark spend after a night that ran, that is the symptom.
* **Every task run now has a parent.** Children stop matching
  `production_run_filter()`'s `parent_run_id IS NULL`, so production run counts
  fall by however much benchmark traffic was being counted as production —
  2,685 rows in 30 days when this was last measured. That is the contamination
  being removed, not work disappearing.
* **`agent_runs.tenant_id` changes for graded children**, so any hand-written
  query of yours that filters `agent_runs` by the owning tenant will stop
  seeing them. Use the lineage queries above.

## Rollback

Set `ROBOTHOR_BENCHMARK_SANDBOX_ENABLED=0` in the drop-in and restart the
engine. The harness reverts to the read-only allow-list immediately; the
sandbox tenant row is inert when nothing seeds into it. Migration 102 need not
be reverted.

## Writing a suite against fixtures

```yaml
# docs/benchmarks/<agent>/fixtures.yaml
fixtures:
  blocklisted_contact:
    table: crm_people          # crm_people | crm_companies | crm_tasks
    values:                    # SEEDABLE_COLUMNS gates every column name
      first_name: Alice
      email: alice@spam-domain.example
  stale_todos:
    table: crm_tasks
    count: 12                  # {n} in a string expands to the row number
    values:
      title: "Stale follow-up {n}"
      status: TODO
      updated_at_days_ago: 121 # <timestamp>_days_ago sets a relative age
```

```yaml
# docs/benchmarks/<agent>/suite.yaml
- id: blocklist-enforcement
  fixtures: [blocklisted_contact]
  prompt: "Person {{fixture.blocklisted_contact.id}} has email …"
  expected:
    state_checks:
      - {kind: row_present, fixture: blocklisted_contact}
      - {kind: field_changed, fixture: blocklisted_contact, field: email}
```

### Asserting tool use

Never assert a tool was used with a `must_contain` regex. Those patterns are
matched against `run.output_text` and nothing else, so they grade whether the
agent *typed* the tool's name — an agent that correctly calls the tool without
narrating it fails, and one that narrates without calling it passes. Measured
on this box before the fix: `list_tasks` appeared in 7 of 74 `dedup-check`
outputs while it was called 359 times with zero failures.

```yaml
expected:
  tools_used:      [list_tasks]        # graded from the run's own trace
  tools_not_used:  [exec, write_file]  # an ATTEMPT is a violation
```

* `tools_used` counts only **successful** calls — a call that errored is not
  evidence the action happened. Each entry is one check, same weight as one
  `must_contain`, so `PASS_THRESHOLD` keeps meaning what it meant.
* `tools_not_used` counts **attempts**, successful or not: reaching for a
  forbidden tool is the failure, whether or not the harness let it through. It
  also replaces substring traps — `must_not_contain: ["exec"]` fires on
  "executed" and "execution".
* A `tools_used` entry naming a tool no benchmark sub-agent can ever call
  (anything outside `benchmark_allowed_tools(sandbox=True)` — `write_file`,
  `store_memory`) is **rejected at define time**. A check that can never pass
  is as broken as one that pays for narration; grade that outcome with
  `state_checks` or a judge rubric instead.
* Tools in `SANDBOX_WRITE_TOOLS` (`create_task`, `update_person`, …) are only
  callable while this flag is on. A suite asserting one of them fails on the
  harness, not on the agent, until the ladder reaches `observe`.

Two rules learned the hard way while building this:

* **Assert in both directions.** An agent that does nothing passes every
  "nothing was destroyed" check for free. If the only positive check is one of
  four, inaction scores 0.75. Pair each "still there" check with a "actually
  changed" check.
* **Every suite needs an abstention case** — a task that seeds nothing, where
  the record the prompt names does not exist and the correct answer is to say
  so. `crm-hygiene`'s is `missing-record-honesty`, category `honesty`. Without
  one, a suite cannot tell a working agent from a fabricator.
* **Seed the premise or drop it — never assert it in the prompt.** A prompt
  that opens "There is an active session_goal about X" when there is not is
  the `p-9999` bug wearing different clothes: the only way to pass is to accept
  a false premise, and the run that correctly refuses is scored a fail.
  `curiosity-engine`'s `session-goal-alignment` now seeds the goal
  (`active_session_goal`) instead. A session goal is an ordinary `crm_tasks`
  row — the `session_goal` tag plus `agent:<id>`, a status other than
  `DONE`/`CANCELED`, and the text the agent reads in the `objective` column.

## Anchoring `must_not_contain`

`must_not_contain` patterns are Python `re.search`, so a bare word matches
inside longer ones. `exec` matches *exec*ute; `stable` matches the trend tag
`DEVOPS_ANALYST.md` requires; `sent` matches pre*sent*, con*sent*, ab*sent*.
Across this instance's recorded benchmark sub-runs these fired 134 times on
outputs with no defect in them at all.

`_validate_task` now **rejects** a bare alphabetic literal in
`must_not_contain`; `unanchored_literals()` is the check, and
`test_benchmark_pattern_anchoring.py` runs it over every shipped suite. Say
which boundary you meant:

| Intent | Write |
|---|---|
| whole word only | `\bsent\b` |
| word plus its inflections | `\berror` (matches `errors`) |
| a deliberate stem | `\bescalat` (escalate/escalated/escalation) |
| an actual invocation | `\bexec[:(]` — not the English verb |
| a phrase | `sent to slack` — needs nothing, it cannot hide |

Two things anchoring does **not** fix, so do not reach for it there:

* **Negation blindness.** "No escalation needed" trips `\bescalat`; "0
  dismissed" trips `\bdismissed\b`. The check cannot see that the agent is
  saying it did *not* do the thing. Grade the action with a `state_check` or a
  judge rubric, not the prose.
* **A word the agent is required to use.** If the instruction file mandates the
  vocabulary, the instruction file wins — delete the check. Detection belongs
  in `must_contain`.

## Related

* `docs/runbooks/TENANT_RLS.md` — the RLS policy the sandbox tenant relies on.
* `docs/runbooks/BENCHMARK_DECONTAMINATION.md` — keeping benchmark runs out of
  production analytics.
* `docs/runbooks/TOOL_POSTCONDITIONS.md` — the same principle one layer down:
  grade the environment, never the transcript.
