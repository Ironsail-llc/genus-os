# Runbook — Benchmark sandbox fixtures

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

| Mode | Seeds fixtures | Sandbox CRM writes | State checks | Score |
|------|----------------|--------------------|--------------|-------|
| `off` | no | no | no | unchanged (today's harness) |
| `observe` | yes | yes | recorded on the task result | **unchanged** |
| `alert` | yes | yes | recorded + error-logged | unchanged |
| `enforce` | yes | yes | recorded | **folded into the task score** |

`observe` already changes what a benchmark sub-agent can **do** — that is the
defect being fixed. What it does not change is how the run is **graded**.

**Blast radius is opt-in.** A suite with no `fixtures.yaml` and no
`state_checks` is not scoped to the sandbox tenant at all and behaves exactly
as it does today, reads included. Only suites that opt in move.

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

1. One full fleet night with `state_checks` recorded on `crm-hygiene`'s task
   results, hand-compared against the transcripts. Specifically: a task the
   agent genuinely completed must show **passing** read-backs. Zero passing
   checks means the mechanism is inert, not that the agent is bad — that is the
   "PROBE, don't trust silence" failure this instance keeps re-learning.
2. `crm_people` / `crm_tasks` row counts in `benchmark-sandbox` back at **zero**
   after each run (teardown works):
   ```sql
   SELECT 'people', count(*) FROM crm_people WHERE tenant_id = 'benchmark-sandbox'
   UNION ALL
   SELECT 'tasks',  count(*) FROM crm_tasks  WHERE tenant_id = 'benchmark-sandbox';
   ```
3. Zero rows written to any other tenant by a benchmark run — check
   `agent_runs` rows whose `trigger_detail` starts with `benchmark:` (there is no `is_benchmark` column; the flag lives on the run object only) and confirm the CRM audit log shows no
   mutation outside `benchmark-sandbox`.

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
