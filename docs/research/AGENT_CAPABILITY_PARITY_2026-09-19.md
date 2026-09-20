# Robothor capability parity and gap assessment

Research date: September 19, 2026 (America/New_York).

This extends [the runtime options brief](AGENT_RUNTIME_OPTIONS_2026-09-19.md). It is a source and documentation comparison, not a migration or benchmark result. No candidate was installed or exercised. No service configuration, goal, or external action was changed.

## Main finding

Robothor already has a substantial product layer that a generic agent framework will not replace automatically. Preserve its goal contracts, authority, memory semantics, and external-action receipts behind explicit interfaces. Compare upstream runtimes on how much generic execution machinery they can replace without weakening those contracts.

Pydantic AI and Deep Agents should be peer candidates in the first Python comparison. Pydantic offers a relatively incremental integration path; Deep Agents merits equal attention because persistent background work matters here. OpenCode remains the comparison for adopting a larger runtime and an attractive potential coding specialist. These are architectural judgments, not speed rankings.

## Evidence and limits

The current capability inventory uses the fixed deployed source artifact `6ffb95370d`, at `/home/philip/.local/share/robothor/combined-preview/6ffb95370d`. The research branch predates some of that artifact's goal and autonomy additions. Source presence does not establish tenant enablement, successful production execution, or throughput. Goal pursuit is opt-in and disabled by default; this review did not enable it or inspect private goal contents.

Local evidence paths below refer to that artifact. Upstream links describe documentation fetched during this research; prototype versions must be pinned and their actual behavior checked. “Integrate” means Robothor must supply the contract, even when a framework supplies useful primitives. “Not established” means the reviewed documentation did not demonstrate equivalent behavior, not that implementation is impossible.

## What our goals actually mean

| Capability | Current meaning | Local evidence |
|---|---|---|
| Working checklist | An ephemeral list inside a run; useful for organizing steps | `robothor/engine/todolist.py` |
| Short-term pursuit | An authorized objective that persists across runs until completion, a blocker, pause, cancellation, or budget exhaustion | `robothor/goals/model.py`, `store.py` |
| Long-term pursuit | Coordinates execution children, linked CRM tasks, scheduled reviews, and event watches | `robothor/goals/controller.py`, `events.py`, `docs/features/goal-pursuit.md` |
| Ongoing goal | Recurring assessment requiring fresh evidence each period, rather than permanent completion | `robothor/goals/model.py` |
| Performance standard | Manifest-defined quality/efficiency/correctness targets and breach handling | `robothor/engine/goals.py` |
| Legacy session objective | Older CRM-backed objective and session state, with coding-oriented completion conventions | `robothor/engine/session_goal.py` |

The newer pursuit system includes versioned updates, criterion evidence, optional human review, operator pause/resume/steer/revise controls, bounded repeated failures, durable event wakeups, timed fallback, leases, and aggregate token accounting. Unfinished children prevent finite parent completion. Waiting does not require continuous model calls.

Those distinctions matter: adopting an upstream “planning” tool does not automatically give us authorized objectives that survive restarts and wait days for a real-world event. Nor should changing the engine silently convert legacy objectives into new autonomous pursuits.

## Comparison by capability

| Need | Robothor evidence | Pydantic AI / Harness | Deep Agents / LangGraph | OpenCode |
|---|---|---|---|---|
| Model loop and tool execution | Custom `AgentRunner`, LiteLLM client | Upstream core; adapt admission | Upstream agent stack; adapt middleware | Upstream runtime; bridge Python services |
| Working plans | Per-run checklist | Planning capability, optional persistent stores [1] | Packaged planning [6] | Agent execution controls; exact checklist parity needs testing [9] |
| Authorized short/long goals | Dedicated pursuit domain | Integrate our goal service | Integrate our goal service | Integrate our goal service |
| Ongoing assessments, criterion history | Goal model/store | Integrate | Integrate | Integrate |
| Waiting for business events | Durable inbox, review times, controller | Keep scheduler; optional durable backend [3] | Keep business wake rules; graph persistence useful [7] | Keep external controller; goal parity not established |
| Delegation | Runner spawn context and controls | Subagents; budget behavior differs [2] | Async tasks support independent threads and steering [5] | Agent/subagent modes [9] |
| Pause and stop authority | Lease checks, session steering, tool admission | Map every child and tool path | Map every thread and tool path | Map sessions, tools, and process boundary |
| Goal-family budgets | Coordinator, spawn runs and execution-child accounting | Explicit bridge; child limits are not automatically equivalent [2] | Explicit bridge; aggregate parity not established | Explicit bridge; step limit is not goal-family spend [9] |
| Restart recovery | Goal leases/inbox plus conversation checkpoints | Durable integrations available; choose/configure [3] | Persistent graph state with configured checkpointer [7] | Session persistence alone is insufficient proof of safe action replay |
| Business memory | Retrieval, entities, conflicts and lifecycle | Memory capability supplements our service [4] | Filesystem-backed memory supplements our service [8] | Expose our service through controlled tools/plugins [10] |
| Standard extensions | Existing tools/skills/plugin mechanisms | Tools and capabilities | Tools, middleware, skills [6] | Plugins and tools [10] |
| External writes and reconciliation | Calendar operations plus effects ledger used by sales workflows | Keep Robothor action boundary | Keep Robothor action boundary | Keep Robothor action boundary |
| Tenant authority and autonomy resources | Goal authorization; autonomy broker/preflight/budgets | Keep domain services | Keep domain services | Keep domain services |
| Progress and verification | Telemetry, verifier, completion contracts | Adapt events and validators | Adapt streaming/state and validators | Adapt runtime events and validators |

A lower-level choice remains possible: Pi supplies a composable agent core, while OpenAI Agents SDK supplies orchestration and handoff primitives. Neither reviewed overview establishes an equivalent Robothor goal domain. They belong in a second round if the first candidates require excessive adaptation. [Pi core](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md), [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk).

## Concrete differences worth testing

**Persistent plans are useful but narrower than goals.** Pydantic's planning capability can use SQLite or PostgreSQL storage and represent task dependencies. Its default plan is isolated per run. We can adopt the planning primitive while retaining authorization, completion evidence, wakeups and lifecycle in our goal service. [1]

**Nested budgets can change behavior during a migration.** Pydantic documents that children with their own usage limits use separate accounting, with their tokens no longer included in parent usage even when usage forwarding is enabled. Robothor must account for the complete authorized goal tree independently or configure and verify equivalent accounting. A parent-only counter would be an unsafe assumption. [2]

**Background tasks have their own operational cost.** Deep Agents provides launch/check/update/cancel controls for independent persistent threads through an Agent Protocol server. Updating a task interrupts its run and starts another on the same thread. Task metadata survives message compaction. This is promising for keeping chat responsive, but worker sizing, cancellation and our authorization still require integration. [5]

**Memory is not one interchangeable feature.** Deep Agents distinguishes shared agent memory from user-scoped memory. Robothor's conflict handling and memory lifecycle are additional product semantics. We should expose retrieval and updates through a tenant-aware service, rather than replace the existing store merely because another system offers memory files. [8]

## Gaps and improvements

### Confirmed architectural work

1. **Make goal execution independent of `AgentRunner`.** The current controller calls the concrete runner. Goal binding uses a Python `ContextVar`; runner setup, dispatch, loop guards, completion checks and verifier consume it. A new framework must receive trusted tenant/goal/attempt identity, remaining budget, control updates and usage reporting explicitly. A JavaScript process will not inherit the Python binding.
2. **Clarify goal vocabulary and ownership.** The code has several different meanings of “goal.” Keep those distinctions internally, expose understandable product names, and provide explicit adoption of legacy objectives. Do not collapse persistent pursuits into per-run checklists.
3. **Strengthen criterion-specific verification.** The generic goal model checks recorded evidence and its latest `satisfied` assessment. It does not universally validate the external truth of an arbitrary evidence reference. Add validators where outcomes are mechanically observable—for example, a provider receipt or an actual stored document. Keep human review for outcomes that need judgment. Existing domain verifiers should be reused.

### Coverage questions, not established missing features

- **Every mutating connector:** calendar and sales have durable-operation machinery, but this review did not prove all mutations use an equivalent claim/receipt/reconciliation path. Inventory coverage before adding more execution routes.
- **Distributed goal capacity:** the controller uses a durable coordinator lease per tenant. This supports serialization and coordination, but does not establish capacity under many ready goals and interactive requests. Measure fairness, backlog and cancellation latency before changing concurrency.
- **Complete user-visible status:** telemetry and control mechanisms exist. Verify that the product consistently shows current activity, elapsed time, waiting reason, remaining budget and effective stop status across parent and child work. Source modules alone do not prove the experience is good.
- **Upgrade compatibility:** local tests exist, but no alternate runtime adapter has yet demonstrated preservation of goal contracts across framework upgrades. Establish that suite before selecting a dependency.

Potential upstream additions include durable execution backends, richer reusable planning, and standard extension surfaces. Adopt them only where they replace measured maintenance work or enable a needed feature. Adding another critic, planner or subagent to every simple action could increase latency. We already have planning, delegation and verification mechanisms.

## What to do when a feature is missing

| Situation | Response |
|---|---|
| Robothor-specific behavior, such as authorized recurring goals | Keep it in our product service and expose a narrow integration |
| Generic capability that upstream supports | Adopt its public interface after parity and performance tests |
| Generic capability absent upstream but supported by extension hooks | Implement a small extension; consider contributing reusable parts upstream |
| Required behavior needs repeated edits to framework internals | Treat this as a candidate failure or redesign the boundary; avoid a permanent fork |
| Attractive feature with no demonstrated user benefit | Keep it on the roadmap until an evaluation or use case justifies its cost |

Standards such as MCP for tools and Agent Skills for portable instructions can reduce bespoke integration. They do not standardize our goal lifecycle or prove authorization and retry safety. [MCP architecture](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture), [Agent Skills](https://agentskills.io/home).

## A comparison that would support a decision

Use the improved current engine as the baseline. Build thin Pydantic and Deep Agents adapters first, then an OpenCode comparison if the Python candidates do not clearly satisfy the requirements or the coding-specialist case warrants it. Reuse identical models, tool schemas, authority, memory service and fixtures; separately measure useful framework defaults.

Required behavior tests:

- Simple authorized action completes with bounded work and an observed outcome; ambiguous writes reconcile without duplication.
- Short pursuit resumes after restart without becoming a new objective.
- Long pursuit waits without polling the model, wakes once for a duplicated event, and respects timed fallback.
- Finite parent cannot complete with unfinished execution children; ongoing goals need fresh period evidence.
- Pause/cancel propagates to delegated work and prevents new writes after the control takes effect; already-dispatched effects are reconciled.
- Whole-family budgets account for children; exhaustion requires authorized renewal. Document in-flight overshoot rather than promise impossible instantaneous limits.
- Stale leases and stale updates cannot regain authority. Tenant A cannot read or control tenant B's work.
- Framework upgrade can read or explicitly migrate checkpoints; old events and unknown event variants do not silently corrupt state.

Measure end-to-end p50/p95, time to first useful action, model calls, cost, queue delay, recovery success, cancellation latency, and maintenance burden. Include one-turn tasks, multi-day simulated goals and concurrent tenants. Use simulated time and test tools for the first comparison; no real meeting changes are needed.

The selection gate is demonstrated correctness of the product contracts plus an improvement in speed, maintainability, or useful capability. A longer advertised feature list is not sufficient. Candidate performance and scalability remain unmeasured.

## Primary documentation

1. [Pydantic planning](https://pydantic.dev/docs/ai/harness/planning/).
2. [Pydantic subagents and usage limits](https://pydantic.dev/docs/ai/harness/subagents/).
3. [Pydantic durable execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/).
4. [Pydantic memory](https://pydantic.dev/docs/ai/harness/memory/).
5. [Deep Agents asynchronous subagents](https://docs.langchain.com/oss/python/deepagents/async-subagents).
6. [Deep Agents overview](https://docs.langchain.com/oss/python/deepagents/overview).
7. [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence).
8. [Deep Agents memory](https://docs.langchain.com/oss/python/deepagents/memory).
9. [OpenCode agents](https://opencode.ai/v2/docs/agents).
10. [OpenCode plugins](https://opencode.ai/v2/docs/build/plugins/).

Additional local evidence: `robothor/goals/{model,store,controller,runtime,events,tools}.py`; `robothor/engine/{runner,loop_guards,verifier,completion_contract,session_goal,checkpoint,telemetry}.py`; `robothor/engine/tools/dispatch.py`; `robothor/operations/effects.py`; `robothor/autonomy/`; `robothor/memory/{lifecycle,conflicts}.py`. Goal integration tests are present under `robothor/goals/tests/`; their presence is not a claim that they were executed during this research.
