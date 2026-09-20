# Genus OS — Agent Builder Reference

You are a Claude Code session that knows how to build agents for the Genus OS system. This file teaches the **unit agent + workflow** paradigm: focused agents composed into pipelines.

---

## 1. Unit Agent Philosophy

Every agent is a **focused unit** — one config (YAML manifest) + one prompt (instruction Markdown).

Units perform a **singular task**: classify, analyze, respond, monitor, check links, generate PRs. Not "handle emails" — that's a pipeline of 3 units (classify → analyze → respond).

Units are composed into workflows, not run in isolation. The system's power comes from composition:

- A **unit** is simple enough to test, debug, and reason about in isolation
- A **pipeline** chains units via CRM tasks, event hooks, or workflow YAML
- The **main agent** can dynamically spawn units as sub-programs at runtime

The CRM is the central coordination layer. Tasks are the inter-unit message bus. Memory blocks are shared state. Status files are peer awareness.

---

## 2. The Two Layers

### Unit Layer — Focused Workers

Each unit has precise tool access, runs with `delivery: none` (silent), and is triggered by workflows or events.

| Unit | Does exactly one thing |
|------|----------------------|
| `email-classifier` | Reads inbox, classifies emails, creates tasks for downstream units |
| `email-analyst` | Analyzes complex emails flagged by classifier, creates response tasks |
| `email-responder` | Drafts and sends replies for tasks in its queue |
| `link-checker` | Validates URLs, flags broken links |
| `vision-monitor` | Detects people via camera, creates alerts |
| `failure-analyzer` | Queries agent run failures, classifies root causes |
| `overnight-pr` | Implements fixes as draft PRs from tasks created by analyzers |

### Supervisor Layer — Main Agent

The main agent handles interactive requests and delegates complex work:

- **Simple requests** → handle directly (no spawning)
- **Moderate complexity** → `spawn_agent` one focused unit
- **Complex tasks** → `spawn_agents` multiple units in parallel, synthesize results

The main agent has `v2.can_spawn_agents: true` and access to all tools. Units have narrow tool access and cannot spawn sub-agents (unless explicitly configured).

---

## 2a. Honoring parent objectives — Stage 4/5 contract

Any agent that can receive spawned work (i.e. anything called via `spawn_agent`) **must respect three rules**:

**1. Read the `--- PARENT TASK ---` header.**
If a spawn was called with `parent_task_id`, the runner prepends a block to the child's message:
```
--- PARENT TASK <uuid> ---
Objective: <the parent task's objective>
Next action: <what the planner said to do>
Autonomy: <summary>
DO NOT offer options that contradict the objective.
--- END PARENT TASK ---
```
The child must treat that block as the goal — not whatever the surface message says. If the inbound email offers a meeting but the objective says "without scheduling a meeting", the child refuses the meeting and redirects.

**See** this instance's `brain/agents/EMAIL_RESPONDER.md` Section 2a ("Before replying: honor the parent objective") for the canonical example. Every new worker should have an equivalent section when its action space could diverge from the parent objective.

**2. Plan with `todo_write` if the run has ≥3 steps.**
Call `todo_write` early in the run with the concrete steps. Mark `in_progress`/`completed` as you go. The list drives the runner's reminder injection every ~10 turns and surfaces to Telegram for human-facing runs.

**3. Don't silently drop unfinished work.**
If the run exhausts iterations/budget with `pending` or `in_progress` items AND `parent_task_id` was passed in, the runtime automatically writes `Continue: <first unfinished item>` to the parent's `next_action` and promotes the parent to a thread if not already tagged. This is `_escalate_unfinished_todos` in `robothor/engine/runner.py` — the agent doesn't have to do anything. But the agent DOES have to (a) actually use `todo_write` for this to work, and (b) leave items in the list truthfully — a `completed` status means the work is actually done.

**4. (Opt-in) Promote leftover items to real CRM subtasks.**
When the manifest sets both `todo_list_enabled: true` AND `task_protocol: true`, AND the engine env carries `ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED=1`, unfinished todo items at run-end are ALSO converted into real CRM subtasks under the parent thread (up to `MAX_PROMOTIONS_PER_RUN = 5`). Promoted subtasks carry the `promoted_todo` tag, the parent's `assigned_to_agent` and `priority`, and a content-hash body marker that makes the operation idempotent across retries. A parent already tagged `promoted_todo` is skipped (one-level cycle guard). The Helm task board renders the queue and the planner gets discrete units to re-plan instead of one free-text hint. Implemented in `robothor/engine/todo_promotion.py`.

---

## 3. Orchestration Patterns

### Pattern A: Event-Driven Pipeline

Python cron script publishes to Redis → hook triggers unit → unit creates CRM task → next unit picks it up.

```
email_sync.py (*/10 cron)
  → publishes "email.new" to Redis Stream
    → hook fires email-classifier
      → classifier creates task(assignedToAgent="email-responder", tags=["reply-needed"])
        → responder picks up task on next run
```

This is the **primary** trigger mechanism. Crons on individual agents are safety nets at relaxed frequencies (every 2-6h).

### Pattern B: Workflow Chain

YAML workflow defines: trigger → step → condition → step. The workflow engine runs steps sequentially with conditional branching, and a `parallel` step fans branches out concurrently and joins before the next step.

```yaml
# docs/workflows/nightwatch.yaml
id: nightwatch
name: Nightwatch Pipeline
triggers:
  - type: cron
    cron: "0 2 * * *"
    timezone: America/New_York

steps:
  - id: analyze
    type: agent
    agent: improvement-analyst
    on_failure: abort

  - id: check_tasks
    type: condition
    condition: "has_tasks(assignedToAgent='overnight-pr', status='TODO')"
    if_true: create_prs
    if_false: done

  - id: create_prs
    type: agent
    agent: overnight-pr
    on_failure: skip

  - id: done
    type: noop
```

Independent steps need not queue. A `parallel` step carries nested full step
definitions; branches run concurrently (optionally capped by
`max_concurrent`), each branch's result lands in `steps.<branch_id>` for later
templating exactly like a top-level step's, and the join completes only when
every branch has. A failing branch fails the join after its own
`retry_count`; the parallel step's `on_failure` then decides abort-vs-skip:

```yaml
steps:
  - id: gather
    type: parallel
    max_concurrent: 3
    parallel_steps:
      - id: gather.email
        type: agent
        agent_id: email-analyst
        message: "Summarize unread email"
      - id: gather.calendar
        type: tool
        tool_name: gws_calendar_list
    on_failure: abort

  - id: brief
    type: agent
    agent_id: morning-briefing
    message: "Email: {{ steps.gather.email.output_text }} Calendar: {{ steps.gather.calendar.tool_output }}"
```

Branches may be any step type except `condition`, `approval`, and nested
`parallel` — flow control stays at the top level, enforced at load.

#### Asking a human mid-workflow

An `approval` step suspends the run until a person decides. The run stops
occupying a worker and a deadline; the question becomes a row in
`workflow_approvals` and the operator is paged once. When they answer, the
engine resumes at that step — from a different process if the box restarted
in between, which is the point.

```yaml
  - id: confirm-send
    type: approval
    prompt: "Send the Q3 report to {{ steps.prepare.tool_output.recipients }}?"
    approval_timeout_hours: 24
    on_timeout: abort      # abort (default) | approve | reject
    on_reject: notify-team # omit to stop the run instead
```

- `prompt` is rendered from run context at ask time and is required.
- `on_timeout` is what happens when nobody answers by the deadline. The
  default is `abort`, because silence is not consent; `approve` exists for
  low-stakes gates and is opt-in per step so the choice is visible in the
  YAML. Either way the row is stamped `expired` and kept — "nobody answered"
  is the fact you want three weeks later.
- A rejection ends the run as `cancelled`, not `failed`: the operator was
  asked, said no, and the workflow correctly did nothing. Cron failure paging
  deliberately does not fire for it.
- The decision lands in `steps.<id>.tool_output` (`approved`, `decided_by`,
  `note`) for later templating.

Answering, from the terminal:

```
robothor engine workflow pending
robothor engine workflow approve <run-id> --step confirm-send --note "checked"
robothor engine workflow reject  <run-id> --step confirm-send
```

Or in chat: a delivery agent with the `list_pending_approvals` /
`approve_workflow_step` / `reject_workflow_step` tools relays the operator's
answer, and the engine picks it up on the next watchdog tick (≤1 min).

Or from the Helm: `GET /api/approvals` lists everything waiting on a person —
workflow steps and `ask_user` questions in one list — and
`POST /api/approvals/{kind}/{id}` answers one. Both are operator-scoped and
audited.

A **tool-permission escalation** is the third kind and is deliberately not in
that list: it lives in the engine's memory with a coroutine blocked on it, so a
listing would be stale the moment it rendered. It is announced over the run's
own SSE stream as `approval_required` with `kind: "escalation"`, and the Helm's
chat answers it in place — Allow once, Allow for this session, or Deny — at
`POST /api/approvals/escalation/{id}`, which the bridge proxies to the engine
process holding the request. The card counts down `timeout_seconds`; past that
the engine has already denied the tool for itself.

#### Asking mid-run: the `ask_user` tool

A workflow approval is a gate the *author* declared. `ask_user` is a question
the *agent* decides to ask, in the middle of a turn, when a wrong guess would
be expensive: which recipient, which account, delete or keep. Add it to an
agent's `tools_allowed` to enable it — it is an ordinary opt-in tool, not an
implicit capability.

- On Telegram the question arrives as an inline keyboard (with `options`) or as
  plain text (without), and the agent's run stays blocked until the operator
  answers or the timeout runs out.
- The question is written to `agent_questions` **before** the channel is asked,
  so a process restart does not lose it and a late answer is still usable.
- On a webchat run there is no channel to ask over yet, so **the run does not
  wait**: it records the question, emits an `approval_required` status event
  over the SSE stream, gets `{"answered": false, "delivered": false}` back in
  the same tick, and carries on. The Helm answers through
  `POST /api/approvals/question/{id}` *after* the run has finished, and a later
  turn is what sees the answer. The tool says exactly that rather than implying
  a wait it never did.
- On a **scheduled or sub-agent run the tool refuses**, and says why. Nobody is
  watching those, so asking would block the run until it timed out. Agents that
  need a human on a cron path should file a CRM task instead.
- Nobody answering is never an answer: the tool returns
  `{"answered": false, "question_id": ...}` and the agent is expected to proceed
  on its best judgement and say what it assumed.

### Pattern C: Dynamic Sub-Agent Dispatch

The main agent spawns units at runtime based on the request:

```
User: "Research competitor pricing and summarize"
Main agent:
  → spawn_agents(["web-researcher", "price-analyzer"])
  → receives structured results from both
  → synthesizes into a single response
```

Sub-agents inherit budget constraints from the parent. Delivery is forced to `none` on children.

`tools_override` may narrow a child's declared `tools_allowed`, but it cannot add
tools outside that list. The engine rejects such a request before starting the
child. An empty override preserves the manifest; it does not mean unrestricted
access or disable all tools. A child with no declared allowlist can be narrowed
to an explicit list, and its `tools_denied` restrictions still apply. Change the
child manifest deliberately when it needs an additional capability.

Set `v2.spawn_allowed_agents` to a list of agent IDs when delegation must stay
inside an approved team. The engine checks the target before loading its manifest.
This restriction follows the entire spawn tree: each child's own nonempty list
can only narrow its ancestor's list. Disjoint lists permit no further targets.
An omitted or empty list adds no restriction, preserving existing configurations;
`can_spawn_agents: false` still disables spawning. Target permission does not grant
additional tools or change the child's service role.

If the parent runs from a managed fleet release, its children and further
descendants inherit that exact release ID. Before each child starts, the engine
verifies the staged artifact and loads the child's manifest and knowledge from
it. Drift, a missing release, or a child outside that release refuses the spawn;
there is no fallback to live workspace manifests. Unpinned runs retain their
normal manifest-loading behavior. Spawned model requests also inherit any active
funded request-budget scope; delegation does not create a new allowance.

Use `v2.max_spawn_total` (an integer from 0 to 100) to limit admitted child
attempts across repeated batches and the whole descendant tree. Each attempt
consumes one slot from every applicable ancestor allowance. Children may add a
stricter allowance; zero adds no limit and never removes an inherited one.
Admission is atomic across concurrent spawns. Failed, deduplicated, and cancelled
attempts keep their slots, so retrying cannot replenish the allowance. Invalid
targets or configurations are rejected before admission. This complements the
per-batch, nesting-depth, concurrency, and funded request-budget limits.
Automatic error-recovery helpers use the same admission path: they cannot load
an unapproved helper from the live workspace or bypass an exhausted allowance.
`SpawnContext.nesting_depth` is the executing run's depth: roots use zero and
first children use one. The runner persists that value without incrementing it
again; benchmark child contexts use the same convention.

`spawn_allowed_agents` and `max_spawn_total` — like every other security field
on the manifest — are carried into a `heartbeat:` or `worker:` override run
unchanged. The override blocks name what they CHANGE (schedule, instructions,
delivery, warmup, budget, model, tools) and everything else is inherited, so a
field nobody remembered to list cannot silently reset to its permissive default
on the runs nobody is watching. That is not automatic: both builders once
reconstructed the config field by field, and the fields they forgot took the
dataclass default — which is how every drain run came to execute with no
guardrails in a local sandbox, and how these two allowances came to be absent
from every heartbeat and drain run.

Native integrations may attach an internal per-result callback to the parallel
spawn handler to checkpoint completed children before siblings finish. It receives
the input index and native result; it does not alter spawn admission, identities,
funding or cancellation. This callback is not a model-callable tool argument.
Callback failures are batch failures, and the remaining children are still awaited.

Trusted workflow integrations can use `required_tool_scope` to require one
already-granted tool while a workflow prerequisite is pending. LLM dispatch sends
an exact function `tool_choice`; it refuses a missing capability rather than
adding it. This scope is not model-controlled and closes for inherited async tasks
when its owner exits. It does not validate tool outcomes or confer success: the
owning workflow must retain its result checks. Tool-less auxiliary requests remain
unchanged, and outside the scope the engine uses normal automatic tool selection.

### Pattern D: Cron Safety Net

Python crons fetch data and publish events. Unit agents process the data. Crons are NOT the primary trigger — they catch anything the event hooks missed.

```yaml
# Agent runs every 6h as a safety net — primary trigger is the hook
hooks:
  - stream: email
    event_type: email.new
    message: "New email received. Check triage inbox and classify."
schedule:
  cron: "0 6-22/6 * * *"    # safety net every 6h
```

---

## 4. Building a Unit Agent

### Step 1: Scaffold

```bash
robothor agent scaffold <agent-id> --description "One-line purpose"
```

This creates both files (manifest YAML + instruction Markdown) with the correct structure.

### Step 2: Edit the Manifest

Required fields:

```yaml
id: my-agent                    # kebab-case, unique
name: My Agent                  # human-readable
description: One-line purpose   # what this agent does
version: "2026-03-04"           # YYYY-MM-DD of last change
department: custom              # email|calendar|operations|security|communications|crm|briefings|core|custom
```

Schedule and session:

```yaml
schedule:
  cron: "0 6-22/4 * * *"       # APScheduler cron expression
  timezone: America/New_York
  max_iterations: 10            # infinite-loop protection; runs complete when the agent is done
  session_target: isolated      # isolated (fresh each run) or persistent (keeps history)
```

> **No wall-clock timeouts, no cost budgets.** Runs are not killed on
> elapsed time or dollar spend. Cost and duration are tracked in
> `agent_runs` for dashboards, never enforced. `max_iterations` is the
> only cap — and it exists to stop infinite loops, not to rush work.
> Add `stall_timeout_seconds` only if this agent talks to a known-flaky
> provider and you want a hang detector; default is off. See
> `docs/agents/_defaults.yaml` (instance-local, not shipped).

Delivery — units are silent:

```yaml
delivery:
  mode: none                    # ALWAYS none for unit agents
```

**Only 3 agents talk to the user:** main (via heartbeat), morning briefing, evening wind-down. All other agents use `delivery: none`.

Coordination — who this unit connects to:

```yaml
reports_to: main
escalates_to: main
creates_tasks_for: [email-responder]
receives_tasks_from: [email-classifier]
task_protocol: true
```

Event hooks — primary triggers:

```yaml
hooks:
  - stream: email
    event_type: email.new
    message: "New email received. Check triage inbox and classify."
```

Warmup — pre-loaded context:

```yaml
warmup:
  memory_blocks: [operational_findings, contacts_summary]
  context_files:
    - brain/memory/my-agent-status.md
  peer_agents: [related-agent]
```

**Context hooks you get for free.** Beyond what the manifest names, the
platform injects a `SITUATIONAL CONTEXT` block from the hooks registered in
`robothor/engine/warmup.py`. Nothing to configure — each hook decides for
itself whether your agent gets it:

| Hook | What it adds | Who gets it |
|------|--------------|-------------|
| `_date_context` | Today's date, weekday, upcoming US holidays | Every agent |
| `_travel_status` | The `travel_status` memory block, if non-empty | Every agent |
| `_weather_context` | The instance's weather status file, if present | Every agent |
| `_git_status_context` | Branch, working-tree status, last five commits | Agents whose `tools:` include a git tool |
| `_thread_pool_context` | The thread pool, after an auto-sweep | `main`, on cron beats only |
| `host_state_context` | Live engine uptime, platform version and last-24h model reach, headed "as of now" | `main` and any agent with a `heartbeat:` block — on **scheduled and interactive** runs alike |

`host_state_context` is what stops an agent answering "has the engine been
restarted?" or "is the fleet on fallbacks?" from a memory fact that was true
last week. It probes the host directly, says so in its own text, degrades each
fact to a one-line "unknown" rather than failing, and is memoised for 60
seconds. Workers deliberately do not get it: they act on CRM tasks, not on
platform health, and the probe is not free.

Three details worth knowing if you are writing a heartbeat agent:

- It reaches **interactive** turns too (Telegram, webchat, channel wake), not
  only scheduled ones. The other hooks in this table that target `main` are
  cron-only; this one is not, because the question it answers is one the
  operator asks in chat. `CHANNEL_EVENT` takes the interactive path for *any*
  agent, so a heartbeat agent woken by a channel event gets it there too.
- A `heartbeat:` block is by itself enough to make your agent build a warmup
  preamble. You do **not** need to declare a `warmup:` section to get this —
  live engine state counts as a reason to warm.
- It reads your manifest's `model.primary` to say whether the fleet is actually
  reaching it. If it cannot see a config it says "the busiest model was …"
  rather than claiming you have no primary configured — those are different
  facts, and only one of them is about your manifest.

To turn the section off for an instance, call
`robothor.engine.host_state.set_host_state_enabled(False)`. That is the single
switch: it gates the hook, the interactive builder and the warm decision alike.
Dropping the hook registration only disables the scheduled path.

Status file — written at end of every run:

```yaml
status_file: brain/memory/my-agent-status.md
```

v2 engine features (all optional):

```yaml
v2:
  error_feedback: true          # inject error analysis when tools fail (default ON)
  planning_enabled: false       # pre-execution planning phase
  guardrails: []                # no_destructive_writes, no_external_http, no_main_branch_push
  can_spawn_agents: false       # allow spawning sub-agents
```

### Step 3: Write the Instruction File

Every instruction file follows this contract:

```markdown
# Agent Name

You are **Agent Name**, an autonomous agent in the {{ai_name}} system.

## Your Role

2-3 sentences. What you DO and what you DON'T do. Be specific about boundaries.

## Tasks

Numbered list — what to do each run:
1. What inputs to read (task inbox, files, memory blocks)
2. What processing to perform
3. What outputs to produce (status file, tasks, notifications)

## Output

Write to `brain/memory/<agent-id>-status.md`:
- One-line summary + ISO 8601 timestamp
- Example: "Processed 3 emails, created 2 tasks. — 2026-03-04T14:00:00Z"
```

Add conditional sections when the manifest enables them:

| If manifest has... | Add section... |
|---------------------|----------------|
| `task_protocol: true` | **Task Protocol** — `list_my_tasks()` → set IN_PROGRESS → process → `resolve_task()` |
| `review_workflow: true` | **Review Workflow** — set tasks to REVIEW (not DONE) when approval needed |
| `hooks:` (event-driven) | **Trigger Context** — explain what event data is available |
| `v2.can_spawn_agents: true` | **Sub-agents** — when/how to spawn helpers |

**Anti-patterns to avoid:**
- No localhost URLs (engine's `web_fetch` blocks loopback)
- No hardcoded chat IDs (use delivery config)
- No file paths outside workspace (use instance-relative `brain/` paths)

### Step 4: Validate and Deploy

```bash
python scripts/validate_agents.py --agent <agent-id>
robothor engine run <agent-id>   # test manually first
```

A manifest the engine has not been told about does not fire. Reconcile rather
than restart:

```bash
curl -XPOST localhost:18800/api/admin/scheduler/reconcile   # needs engine:control
```

The watchdog reconciles every five minutes anyway, so a restart is never
required — it is just the impatient version of waiting.

### Step 5 (or instead of 1–4): the Helm's agent builder

`/api/agent-manifests` is the same flow from the browser, with no ssh session
and no restart. It renders the same templates, runs the same validation, writes
the same two files, and calls the engine reconcile itself.

| Route | What it does |
|-------|--------------|
| `GET /api/agent-manifests` | The fleet, plus a `broken` list naming any manifest that will not load and its error type. One unreadable file lands in `broken`, never in a 500 — a fleet of twenty with one bad manifest lists nineteen |
| `GET /api/agent-manifests/{id}` | The parsed document, its raw YAML, its instruction file, and its verdict. A broken manifest answers 200 with the verdict, not 404 — "absent" and "unreadable" have different fixes |
| `POST /api/agent-manifests/validate` | Always 200; the verdict is the payload (`{ok, errors, warnings}`). Send `{manifest}` or `{yaml}` |
| `POST /api/agent-manifests` | Scaffold + validate + write + reconcile. `409` on a colliding id, `422` carrying the validator's own codes on refusal |
| `PATCH /api/agent-manifests/{id}` | Edits only the form-owned paths, bumps `version`, appends a changelog entry, snapshots the previous document, reconciles. Refused only on errors **this edit introduced** — see below |
| `POST /api/agent-manifests/{id}/enable` \| `/disable` | Sets `schedule.enabled`. Disabling drops the cron, heartbeat and worker jobs on the next reconcile without retiring the agent |
| `DELETE /api/agent-manifests/{id}` | Body `{"confirm": "<id>"}`. Moves the manifest to the instance's own `docs/agents/retired/` — nothing is unlinked, and the instruction file stays |
| `POST /api/agent-manifests/{id}/run` | Fires one run now, via the engine's trigger route |

Every write answers with a `reconcile` block. `applied: false` means the file is
on disk and the engine has not picked it up yet — the watchdog will, within five
minutes.

**What an edit is refused for.** Only the errors it INTRODUCED. Every edit is
validated twice — the document as it was, and as the edit leaves it — and the
difference is what decides the 422. "A save must not break a manifest" and "a
save is gated on the manifest being unbroken" are different promises, and the
second locks the operator out of the file exactly when they need it: an agent
with a bad cron, or one naming a tool a since-uninstalled plugin provided, could
not be repaired AND could not be `disable`d, and `disable` is the stop control.
A pre-existing fault comes back in `warnings` — and in `pre_existing`, so a UI
can say "saved, still broken for these reasons" without diffing two lists — so
"allowed through" does not read as "blessed". It is never a free pass for a
second fault, including one of the *same kind*: a check that finds three
unregistered tool names reports three findings, not one
(`CheckResult.faults`), so adding a fourth is refused while **removing** one is
a repair and is accepted, with the two that remain reported under
`pre_existing`.

**A save with `pre_existing` still reconciles.** The response carries
`reconcile.applied: true` and an `added`/`replaced` entry beside the carried
faults, and that is correct rather than a contradiction: the fault was already
live before the edit, and the edit did not change what it does to the agent. The
two classes differ —

| Fault class | Loads? | Schedules? | Example |
|-------------|--------|-----------|---------|
| `manifest_checks` FAIL (`check.*`) | yes | yes | an unregistered tool name, a missing instruction file — the agent runs, and the tool is simply unavailable to it |
| `bad_cron` / `not_loadable` / schema errors | no | no | an unparseable cron, a wrongly-typed block — `manifest_to_agent_config` or APScheduler refuses it, so reconcile has nothing to register |

So `reconcile.applied: true` next to a populated `pre_existing` means "the write
landed and the schedule is unchanged", not "the manifest is now clean". Read
`pre_existing` before telling an operator their agent is fixed.

**What a PATCH may change.** An edit sets only the paths the form owns
(`routers/agent_manifests.FORM_OWNED_PATHS`: name, description, department,
`model.primary`/`fallbacks`, the `schedule` and `delivery` blocks,
`tools_allowed`/`tools_denied`, `instruction_file`, `version`, `changelog`).
Everything else in the document survives untouched, including keys the platform
has never heard of — a hand-written `model.temperature` or a plugin's own
`v2.*` key is not collateral for renaming an agent. List-valued paths REPLACE
rather than union, so removing a fallback model or a tool actually removes it.

**Turning an agent off.** `schedule.enabled: false` is the switch:

```yaml
schedule:
  cron: "0 9 * * *"
  enabled: false      # default true; reconcile drops every job for this agent
```

It silences the agent's cron, heartbeat and worker together and keeps the
manifest, the instructions and the `agent_schedules` row, which is what lets the
fleet view show "off" instead of making a silenced agent look deleted.

---

## 5. Model Tiering Strategy

Models change frequently. Don't hardcode model names in templates — use **tier variables** from `_defaults.yaml` that resolve at install time.

| Tier | Use Case | Selection Criteria | Config Variable |
|------|----------|-------------------|-----------------|
| **T0: Router** | Classification, triage, simple extraction | Cheapest with reliable tool-calling | `{{ model_primary }}` |
| **T1: Worker** | Standard tool use, CRM writes, file ops | Good cost/quality balance, fast | `{{ model_primary }}` |
| **T2: Reasoning** | Analysis, composition, complex decisions | High quality, willing to pay more | `{{ model_quality }}` |
| **T3: Orchestrator** | Multi-agent coordination, synthesis, code generation | Best available for planning | `{{ model_quality }}` or override |

Current defaults (from `_defaults.yaml`):

```yaml
model_primary: "openrouter/moonshotai/kimi-k2.5"     # T0/T1
model_quality: "openrouter/anthropic/claude-sonnet-4.6"  # T2/T3
model_fallbacks:
  - "gemini/gemini-2.5-pro"
```

**Guidance:**
- Pick the cheapest tier that can do the job
- Track cost-per-run with `get_agent_stats()` — promote to a higher tier only when quality metrics (error rate, escalation rate) demand it
- Model names live in `_defaults.yaml` — update once there, all agents get the new model at next install/update
- Fallback chains are mandatory — models break, get deprecated, or change pricing

In manifest templates, use variables:

```yaml
model:
  primary: {{ model_primary }}
  fallbacks: {{ model_fallbacks }}
```

In concrete manifests (non-template), use the actual model string directly.

---

## 6. CRM-Centric Coordination

The CRM is how units talk to each other. No direct agent-to-agent calls outside of `spawn_agent`.

### Tasks as Inter-Unit Messages

Agent A creates a task for Agent B:
```
create_task(title="Analyze email from CEO", assignedToAgent="email-analyst", tags=["email", "analytical"])
```

Agent B picks it up:
```
list_my_tasks(assignedToAgent="email-analyst", status="TODO")
→ update_task(id, status="IN_PROGRESS")
→ ... work ...
→ resolve_task(id, resolution="Analysis complete: 3 action items identified")
```

**Tag vocabulary** for routing: `email`, `reply-needed`, `analytical`, `escalation`, `needs-owner`, `calendar`, `conflict`, `cancellation`, `vision`, `unknown-person`, `crm-hygiene`, `dedup`, `enrichment`, `nightwatch`, `self-improve`.

### Memory Blocks for Shared State

Persistent key-value blocks that multiple agents can read/write:

- `operational_findings` — cross-agent observations
- `contacts_summary` — CRM contact intelligence
- `performance_baselines` — agent performance metrics
- `nightwatch_log` — overnight improvement tracking
- `concierge_observations` — usage pattern analysis

### Status Files for Peer Awareness

Every agent writes a one-line status file at the end of each run:

```
Checked N URLs across M tasks. K broken. — 2026-03-04T14:00:00Z
```

The heartbeat checks for staleness. Peer agents read each other's status files via warmup context.

---

## 7. Tool Access Design

Each unit gets ONLY the tools it needs. The engine's `build_for_agent()` method strictly filters — if `tools_allowed` is set, ONLY those tools are available.

**CRITICAL:** If your agent does ANY file I/O, reads status files, or runs CLI commands, you MUST include `exec`, `read_file`, and `write_file`. Without these, agents silently fail — they hallucinate tool calls that don't exist.

### Tool Categories

| Category | Tools |
|----------|-------|
| **File I/O** | `exec`, `read_file`, `write_file`, `list_directory` |
| **Memory** | `search_memory`, `store_memory`, `get_entity`, `memory_block_read`, `memory_block_write`, `append_to_block`, `memory_block_list` |
| **CRM** | `create_task`, `update_task`, `get_task`, `list_tasks`, `list_my_tasks`, `resolve_task`, `create_note`, `list_notes`, `update_note`, `create_person`, `list_people`, `get_person`, `update_person`, `create_company`, `list_companies` |
| **Web** | `web_fetch`, `web_search` |
| **Communication** | `log_interaction`, `list_conversations`, `get_conversation`, `list_messages`, `create_message` |
| **Vision** | `look`, `who_is_here`, `enroll_face`, `set_vision_mode` |
| **Voice** | `make_call` |
| **Engine** | `list_agent_runs`, `get_agent_stats`, `get_fleet_health`, `detect_anomalies` |
| **Git** | `git_status`, `git_diff`, `git_branch`, `git_commit`, `git_push`, `create_pull_request` |
| **Sub-agents** | `spawn_agent`, `spawn_agents` |
| **Vault** | `vault_get`, `vault_set`, `vault_list` |

### Common Tool Profiles

**Read-only unit** (monitors, analyzers):
```yaml
tools_allowed: [read_file, search_memory, get_entity, memory_block_read, web_fetch, list_tasks, list_my_tasks]
```

**CRM worker** (task processors):
```yaml
tools_allowed: [exec, read_file, write_file, search_memory, store_memory, list_my_tasks, update_task, resolve_task, create_task]
```

**Action unit** (responders, callers):
```yaml
tools_allowed: [exec, read_file, write_file, web_fetch, create_message, log_interaction, list_my_tasks, update_task, resolve_task]
```

---

## 8. Template Packaging

When sharing agents as installable templates, each agent is a **5-file bundle**:

```
templates/agents/<department>/<agent-id>/
├── setup.yaml                    # Installation metadata + variable declarations
├── manifest.template.yaml        # Agent manifest with {{ variable }} placeholders
├── instructions.template.md      # Instruction file with {{ ai_name }} etc.
├── SKILL.md                      # Human-readable skill card (Agent Skills Standard)
└── programmatic.json             # Machine-readable metadata for web discovery
```

### setup.yaml — Installation Metadata

```yaml
agent_id: email-classifier
version: "2026-03-04"
instruction_file_path: brain/EMAIL_CLASSIFIER.md
variables:
  model_primary:
    type: string
    default: "openrouter/moonshotai/kimi-k2.5"
    description: "Primary LLM model"
  cron_expr:
    type: string
    default: "0 6-22/2 * * *"
    description: "Cron schedule"
    prompt: "How often should this agent run?"
```

### Variable Resolution

Variables resolve through a 5-layer priority chain (last wins):

1. `_defaults.yaml` — global fallbacks (`model_primary`, `model_quality`, `timezone`, etc.)
2. `setup.yaml` defaults — per-template values
3. `.robothor/config.yaml` — instance-wide defaults
4. `.robothor/overrides/<id>.yaml` — per-agent customization
5. CLI `--set key=value` — highest priority

**Install-time variables** use `{{ }}`: `{{ model_primary }}`, `{{ cron_expr }}`
**Runtime variables** use `${}`: `${ROBOTHOR_TELEGRAM_CHAT_ID}` — left unresolved for the engine

### SKILL.md — Agent Skills Standard

Human-readable card compatible with Claude Code, Codex, VS Code, and Copilot:

```markdown
---
name: Email Classifier
version: "2026-03-04"
description: Classifies incoming emails and routes to appropriate handlers
format: robothor-native/v1
department: email
---

# Email Classifier

Monitors the email inbox and classifies incoming messages...
```

### programmatic.json — Machine Discovery

```json
{
  "name": "Email Classifier",
  "id": "email-classifier",
  "version": "2026-03-04",
  "format": "robothor-native/v1",
  "department": "email",
  "description": "Classifies incoming emails and routes to handlers",
  "tags": ["email", "reply-needed", "analytical"]
}
```

### Installation

```bash
robothor agent install email-classifier              # from catalog
robothor agent install ./path/to/bundle/              # from local directory
robothor agent install standard --preset standard     # install a preset group
```

Preset mode installs every agent in the preset. The positional argument is
still required by the parser and is ignored when `--preset` is given.

---

## 8a. Sharing an Agent

A template bundle is what the hub publishes. An **agent bundle** is what you
hand to a person: the same files plus `bundle.yaml`, which pins every member by
SHA-256 and states what the agent needs on the far side.

### Export

```bash
genus agent export email-classifier                        # ./agent-email-classifier-<version>.tar.gz
genus agent export email-classifier --out ./shared/        # a directory instead
genus agent export email-classifier --include-adapters     # carry MCP adapter definitions too
```

`bundle.yaml`:

```yaml
kind: agent-bundle
schema: 1
id: email-classifier
name: Email Classifier
version: "2026-03-04"
exported_at: "2026-09-15T12:00:00+00:00"
platform_version: 1.89.0
requires:
  plugins: [genus-billing]        # from the manifest's requires: block
  adapters: [billing]             # adapters whose agents: list names this agent
  secrets: [BILLING_API_KEY]      # every ${NAME} the bundle references
  skills: [triage]                # from the manifest's requires: block
files:
  - path: setup.yaml
    sha256: 5f2b…
  - path: manifest.template.yaml
    sha256: a91c…
```

**What is included:** the manifest template, the instruction file, `setup.yaml`,
`SKILL.md`, `programmatic.json`, and every skill named in the manifest's
optional `requires.skills` (copied from `agents/skills/<name>/`).

**What is not:** memory, CRM rows, your instance config, your `.env`, and —
unless you pass `--include-adapters` — adapter definitions. An adapter names a
command the engine will run; carrying one is a decision worth typing.

**Two refusals, and they are hard failures — nothing is written:**

| Refused | Why |
|---------|-----|
| a credential **value** anywhere in the bundle | exit 2, naming `file:line`; the value itself is never printed. Replace it with `${NAME}` and list the name under `requires.secrets`. |
| `/home/<someone>/…`, `/Users/<someone>/…`, or this instance's workspace path | the same rule `scripts/check_instance_leak.py` enforces on a commit, applied where the file leaves. Use workspace-relative paths. |

What "a credential value" means, precisely — every member of the bundle is read
as text and checked for:

- **self-identifying token families**: `ghp_`/`gho_`/`ghs_`/`github_pat_`,
  `glpat-`, `AKIA…`/`ASIA…`, `AIza…`, `sk-`/`sk-or-`, `xox[abceprs]-`/`xapp-`,
  `npm_`, `shpat_`, and a JWT's three base64 segments
- **URL userinfo** — `https://user:password@host` — anywhere at all, including
  inside a `command:` array
- **PEM armour** — `-----BEGIN … PRIVATE KEY-----`
- a **credential word anywhere in a key name** (`api_key`, `access_key_id`,
  `client_secret`, `github_pat`) with a literal scalar value
- a credential **stated in prose** — "the billing password is hunter2hunter2" —
  where the noun is singular and determined ("*the* password is", not
  "Passwords are"), the value is twelve characters of letters and digits, and it
  is not the name of an algorithm. Prose about authentication is what operators
  write most, so this rule refuses only when the sentence carries the value.

`${NAME}` references are substituted out first, so an adapter that authenticates
correctly exports cleanly.

With `--include-adapters`: every value under `headers:` is replaced by a
`${NAME}` reference, and a value under `env:` or a top-level string is replaced
when its key names a credential, its value carries one of the shapes above, or
it reads as an issued identifier (long, unbroken, letters and digits). A plain
URL with no userinfo is never treated as one — an endpoint is the thing an
adapter must state in the clear, and versioned paths like `/v1/_mcp` would
otherwise become a secret the receiver has to invent. Every name introduced this
way is added to `requires.secrets`, so the far side supplies its own.
`command:` is **never** rewritten — an argv element cannot be parameterised
without breaking the command — so a credential there refuses the export instead.

**Reproducible.** The same agent exports to the same bytes: members are written
in sorted order with fixed mode, ownership and mtime, and the gzip header's
timestamp is zeroed. The only field that changes between two exports of an
unchanged agent is `exported_at` — pin it with `--exported-at`:

```bash
genus agent export email-classifier --exported-at 2026-09-15T00:00:00+00:00
```

### Declaring requirements

Skills are a workspace-wide library and plugins are a process-wide seam, so
neither can be derived from the agent alone. Say so in the manifest:

```yaml
requires:
  plugins: [genus-billing]
  skills: [triage]
```

Adapters and `${NAME}` references are derived automatically and merged with
anything you declare.

### Install

```bash
genus agent install ./agent-email-classifier-2026-03-04.tar.gz          # print the plan
genus agent install ./agent-email-classifier-2026-03-04.tar.gz --yes    # install it
genus agent install ./shared/email-classifier --yes                     # a bundle directory
genus agent install https://example.org/agent.tar.gz --sha256 <hex> --yes
```

Without `--yes` nothing is written — you get a plan:

```
Agent:    Email Classifier (email-classifier) v2026-03-04
Source:   ./agent-email-classifier-2026-03-04.tar.gz
SHA-256:  9c1f…

Files this would write:
  docs/agents/email-classifier.yaml
  brain/agents/email-classifier.md
  agents/skills/triage/

What this agent would be allowed to do:
  tools:        read_file, web_fetch (!), gws_gmail_send (!)
  delivery:     none
  schedule:     0 6-22/4 * * * (America/New_York)
  instructions: brain/agents/email-classifier.md (2104 bytes)
      # Email Classifier
      Triage the inbox and route each message.

Scan:     review
  - tools that act on the world: gws_gmail_send, web_fetch

Requirements:
        ok  secret BILLING_API_KEY
   MISSING  plugin genus-billing — not installed; 'genus plugin install genus-billing'
```

The capability block is the point of the preview. A bundle is a prompt plus a
tool grant plus a schedule. Every tool is listed; `(!)` marks the ones that
raise the scan verdict. The values shown are the ones that will be **written** —
`--set` overrides are applied before the plan is rendered. An **absent or empty
`tools_allowed`** reads as *every tool this fleet has* — which is what the engine
does with it — never as "none".

A tool is flagged when it can do something the receiving instance cannot take
back:

| Flagged | Not flagged on its own |
|---------|------------------------|
| execution — `exec`, `shell`, `bash`, `run_command` | workspace writes — `write_file`, `edit_file`, `append_file`, `create_file` |
| starting another agent — `spawn_agent`, `dispatch_agent` | reads — `read_file`, `search_files`, `list_directory`, `search_memory`, `get_*`, `list_*` |
| the network — `web_fetch`, `http_request`, `browser` | mailbox and calendar reads — `gws_gmail_get`, `gws_gmail_search`, `gws_calendar_list` |
| sending outside — `gws_gmail_send`, `telegram_send`, `slack_post_message`, `make_call` | `web_search` — a query returns results, it does not choose a destination |
| mutating a record — `create_person`, `update_task`, `resolve_task`, `gws_calendar_create` | instance-local memory — `store_memory`, `append_to_block` |
| destroying workspace state — `delete_file` | |

There is no per-agent host allowlist yet, so **every** `web_fetch` counts: a GET
whose URL carries the data is exfiltration with no write tool involved. When an
allowlist exists, this is where it plugs in.

| Flag | Effect |
|------|--------|
| `--yes` | actually write. Without it, plan only. |
| `--sha256 HEX` | pin the archive's bytes. **Required** for a URL — no signed index vouches for a file you name yourself. |
| `--id NEW_ID` | install under a different id. Renames the manifest id, `instruction_file` and the brain file together. |
| `--strict` | refuse unless every requirement is already satisfied. Without it, a missing one is listed and the install proceeds. |
| `--index URL` | resolve the name through that signed index instead of the hub. |
| `--accept-review` | install a bundle the scan marked `review`. Never one it `blocked`. |

**The scan.** Every bundle is scanned on install, the way a plugin wheel is —
on the staged copy, not on whatever a publisher's index claimed.

| Verdict | Means | What happens |
|---------|-------|--------------|
| `safe` | nothing to flag | installs |
| `review` | a capability grant a human should see: a tool that acts on the world, no `tools_allowed` list, or `can_spawn_agents` | refused until `--accept-review` |
| `blocked` | a credential literal or a foreign home path — something no `genus agent export` would have produced | refused, always; `--accept-review` does not cover it |

**Never overwrites anything.** Not just the manifest: **every** file the install
would write is checked, and one that already exists refuses the install and is
named in the plan with its owner. `--id` installs alongside. A skill the bundle
carries that you already have is **kept**, not replaced — your `triage` may be
three months of tuning.

**A bundle only ever writes its own files.** The instruction path is *derived*
from the agent's id, never taken from the manifest verbatim. Say a bundle
declares `instruction_file: brain/agents/main.md` (an instance-local path) for
an agent called `helpful-bot`. What it gets instead is
`brain/agents/helpful-bot.md` — instance-local, and named after the agent that
asked. The bundle chooses the directory; the filename is the platform's.
Underneath that, `installer.install` refuses outright to write an instruction
file another agent's manifest claims.

**Verified before anything is read.** `files[]` hashes must match *and* the
bundle must carry nothing `files[]` does not list; the archive is extracted by
the same bounded extractor the hub client uses (no traversal, no symlinks,
bounded members and size).

### From a signed catalogue

A signed plugin index (`docs/PLUGINS.md`) can publish agent bundles too — an
entry with `kind: agent-bundle` and a `bundle` artifact. Resolution for a bare
name is: local catalog, then a signed index **if you have configured one**, then
the hub. Only "no index publishes that name" falls through; a signature failure
stops there rather than quietly fetching an unsigned copy from the hub.

`genus plugin install` refuses an agent bundle and `genus agent install` refuses
a plugin wheel — each naming the verb that takes it.

### From the Helm

`POST /api/installed-agents/{id}/export` downloads the same bundle (operator
only, audited with the id alone; a credential literal is a 409 naming the file).
`GET /api/installed-agents/{id}/export/plan` returns what *would* be exported,
so a Share dialog can show it first. Adapters are never carried over HTTP, and
install-from-a-path or a URL is CLI-only.

---

## 9. Complete Example: Email Pipeline

Agents returning machine-consumed objects can declare `model.response_format:
json_object`. The runner requests JSON mode for both streaming and non-streaming
calls, including model fallbacks, and asks for one final object without Markdown
or trailing commentary. The default is `text`; concurrent agents keep their own
format. Choose primary and fallback models that support the provider's JSON mode.
Tool calls remain available. JSON mode controls syntax, not business correctness:
validate the result against the workflow's schema and evidence rules before
accepting it. A truncated or unsupported response still fails validation.

Native workflows can additionally apply a trusted `response_schema_scope` from
`robothor.engine.response_schema`. Its JSON Schema overrides the session's generic
JSON-object mode only while that scope is active and its readiness predicate is
true. This is an internal workflow contract, not a model-supplied schema or a tool
permission. Streaming and non-streaming request builders share it. Budgeted
OpenRouter requests require endpoint `structured_outputs` support in addition to
`response_format`; callers must still validate output and domain evidence locally.

For domain checks beyond JSON shape, a workflow can apply
`output_validation_scope` from `robothor.engine.output_validation`. Its trusted
synchronous validator receives the run identity and proposed final text, returning
`None` or a short, safe correction reason. Do not return raw untrusted page text or
sensitive values in feedback. Native completion can request at most two repairs;
these consume the existing run iterations, deadline and shared request budget.
The validator also checks finalization output before marking a run complete.
Validators must be deterministic and side-effect-free. The scope closes for
inherited tasks when its owner exits and does not affect other concurrent runs.

A workflow whose native tool already computes its final result can install a
trusted `workflow_completion_scope` from `robothor.engine.workflow_completion`.
After the complete tool turn, its owning tenant/agent can resolve to final text
or a failure. A success records a `workflow_completion` checkpoint with
`origin=trusted_workflow`; it does not fabricate an LLM call. Ordinary final
output validation remains active. This is an internal callback, not a tool
argument, and it cannot complete child runs or survive the scope's exit.

For a model whose backends have different reliability or tool support, set
`model.provider_order` to a mapping from its exact LiteLLM OpenRouter path to an
ordered, nonempty list of provider slugs. Base slugs include endpoint variants;
use an endpoint slug to pin a variant. These lists are also allowlists: unlisted
backends are refused. Models absent from the mapping retain their usual routing,
including fallback models. Existing engine compatibility requirements still apply.
See [OpenRouter provider selection](https://openrouter.ai/docs/guides/routing/provider-selection)
for provider slug syntax.

```yaml
model:
  primary: openrouter/example/model
  provider_order:
    openrouter/example/model: [preferred/fp8, backup]
```

Within a bounded operation, Genus chooses the first eligible listed provider,
then the least expensive endpoint within that preference. Tool/JSON support,
price constraints, failed-route exclusions and full-context spending admission
still apply. Each attempt pins one endpoint with SDK retries and unlisted
provider fallback disabled. Provider preferences do not increase the job budget
or guarantee a response within the agent deadline. They apply to the session's
main streaming/non-streaming calls; independently invoked auxiliary model calls
keep their own routing.

A 3-unit pipeline: **classifier** → **analyst** → **responder**, connected via CRM tasks and event hooks.

### Unit 1: Email Classifier

**Manifest** (`docs/agents/email-classifier.yaml` — an instance manifest, not shipped):

```yaml
id: email-classifier
name: Email Classifier
description: Triage incoming emails and route to appropriate handlers
version: "2026-03-04"
department: email

reports_to: main
escalates_to: main
creates_tasks_for: [email-analyst, email-responder]

model:
  primary: openrouter/moonshotai/kimi-k2.5     # T0: router — cheap, fast
  fallbacks:
    - openrouter/minimax/minimax-m2.5
    - gemini/gemini-2.5-pro
  reasoning_effort: medium                      # low | medium | high — thinking budget

schedule:
  cron: "0 6-22/2 * * *"       # safety net every 2h
  timezone: America/New_York
  max_iterations: 10
  session_target: isolated

delivery:
  mode: none

hooks:
  - stream: email
    event_type: email.new
    message: "New email received. Check triage inbox and classify."

tools_allowed:
  - exec
  - read_file
  - write_file
  - search_memory
  - get_entity
  - store_memory
  - list_conversations
  - create_person
  - list_people
  - get_person
  - list_tasks
  - create_task
  - list_my_tasks
  - update_task
  - resolve_task

task_protocol: true
status_file: brain/memory/email-classifier-status.md
tags_produced: [email, reply-needed, analytical, escalation, needs-owner]

warmup:
  memory_blocks: [operational_findings, contacts_summary]
  context_files:
    - brain/memory/email-classifier-status.md
    - brain/memory/triage-inbox.json

v2:
  error_feedback: true
```

**Instruction** (`brain/EMAIL_CLASSIFIER.md`, instance-local) — excerpt:

```markdown
# Email Classifier

You are **Email Classifier**, an autonomous agent in the {{ai_name}} system.

## Your Role

Read the email triage inbox and classify each message. Route to the right handler
via CRM tasks. You do NOT reply to emails — you create tasks for downstream units.

## Tasks

1. Read `brain/memory/triage-inbox.json` (instance state) for unprocessed emails.
2. For each email, classify: reply-needed, analytical, escalation, or noise.
3. Create a CRM task for the appropriate handler:
   - `reply-needed` → assignedToAgent="email-responder"
   - `analytical` → assignedToAgent="email-analyst"
   - `escalation` / `needs-owner` → assignedToAgent="main"
4. Write status file.
```

### Unit 2: Email Analyst

**Manifest** (`docs/agents/email-analyst.yaml` — an instance manifest, not shipped):

```yaml
id: email-analyst
name: Email Analyst
description: Deep analysis of complex emails requiring research or context
version: "2026-03-04"
department: email

reports_to: main
escalates_to: main
receives_tasks_from: [email-classifier]
creates_tasks_for: [email-responder]

model:
  primary: openrouter/moonshotai/kimi-k2.5     # T1: worker
  fallbacks:
    - openrouter/minimax/minimax-m2.5

schedule:
  cron: "30 8-20/6 * * *"      # safety net every 6h
  timezone: America/New_York
  max_iterations: 10
  session_target: isolated

delivery:
  mode: none

tools_allowed:
  - exec
  - read_file
  - write_file
  - search_memory
  - store_memory
  - web_fetch
  - web_search
  - list_my_tasks
  - update_task
  - resolve_task
  - create_task

task_protocol: true
notification_inbox: true
status_file: brain/memory/email-analyst-status.md
tags_consumed: [email, analytical]

v2:
  error_feedback: true
```

### Unit 3: Email Responder

**Manifest** (`docs/agents/email-responder.yaml` — an instance manifest, not shipped):

```yaml
id: email-responder
name: Email Responder
description: Draft and send email replies for tasks in queue
version: "2026-03-04"
department: email

reports_to: main
escalates_to: main
receives_tasks_from: [email-classifier, email-analyst]

model:
  primary: openrouter/anthropic/claude-sonnet-4.6  # T2: reasoning — quality-critical
  fallbacks:
    - openrouter/moonshotai/kimi-k2.5
    - gemini/gemini-2.5-pro

schedule:
  cron: "0 8-20/2 * * *"
  timezone: America/New_York
  max_iterations: 15
  session_target: isolated

delivery:
  mode: none

tools_allowed:
  - exec
  - read_file
  - write_file
  - search_memory
  - store_memory
  - get_entity
  - web_fetch
  - list_my_tasks
  - update_task
  - resolve_task
  - create_task
  - log_interaction
  - list_conversations
  - get_conversation
  - list_messages
  - create_message

task_protocol: true
review_workflow: true
notification_inbox: true
status_file: brain/memory/email-responder-status.md
tags_consumed: [email, reply-needed]

warmup:
  memory_blocks: [operational_findings]

v2:
  error_feedback: true
```

### The Workflow (Event-Driven)

No workflow YAML needed for event-driven pipelines. The connection is implicit:

1. `email_sync.py` (cron `*/10`) fetches new emails → publishes `email.new` to Redis
2. Hook fires `email-classifier` → classifier reads inbox, creates tasks with tags
3. `email-analyst` picks up `analytical` tasks on its next run (hook or cron safety net)
4. `email-responder` picks up `reply-needed` tasks on its next run
5. Each unit resolves its tasks and writes its status file

For sequential dependencies (analyst MUST run before responder), use a workflow YAML like the nightwatch example in Section 3.

---

## Validation Checklist

Before deploying any unit agent:

- [ ] `python scripts/validate_agents.py --agent <id>` passes
- [ ] `tools_allowed` includes `exec`, `read_file`, `write_file` if agent does file I/O
- [ ] `delivery.mode` is `none` for all unit agents
- [ ] Instruction file exists at the path specified in the manifest
- [ ] Status file path is under the instance's `brain/memory/`
- [ ] If `task_protocol: true`, instruction file mentions `list_my_tasks`, `IN_PROGRESS`, `resolve_task`
- [ ] Model tier matches the unit's complexity (don't use T2 for classification)
- [ ] Version date is today's date
- [ ] Department matches the agent's function

### Required-tool provider compatibility

For bounded OpenRouter requests, a named function can also use an endpoint that
explicitly supports `tool_choice=required` plus tools, even if it lacks named
function choice. Genus narrows that request's tool schemas to the single already
available named function and sends `required`. It never substitutes `auto`, adds
a tool, or grants permission. Missing or duplicate target schemas are refused.
Subsequent ordinary requests retain their normal tool list. Native dispatch and
domain result validation remain authoritative if a provider disobeys the request.
