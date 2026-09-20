**Robothor agent runtime options — discussion brief**

Research date: September 19, 2026 (America/New_York). Documentation was fetched during this session. This is an architectural recommendation, not a migration decision or a performance result. No candidate runtime was installed, no benchmark against one was run, and no live configuration was changed for this research.

Follow-up: [capability parity and gap assessment](AGENT_CAPABILITY_PARITY_2026-09-19.md) inventories the newer deployed goal system. It promotes Deep Agents to a peer first-round Python candidate and identifies goal-budget and verification contracts that require explicit testing.

**Recommendation**

Adopt a maintained agent runtime through a small, explicit integration boundary, while keeping Robothor's product services independent of it. Start by comparing Pydantic AI with selected Harness capabilities against OpenCode and the improved current engine. Keep Deep Agents as the strongest alternative if its packaged workflow capabilities remove more custom work. This ordering is my assessment of fit, not an externally measured ranking.

The objective is to stop owning every generic agent mechanism. We should still own what an action means, who can authorize it, how its outcome is verified, and the experience the user sees. A framework can supply an agent loop; it cannot establish that our particular calendar invitation was correctly authorized and completed.

**What we have today**

The production entrypoint runs `robothor.engine.daemon`; it constructs our Python `AgentRunner`. The runner uses `LLMClient`, with LiteLLM handling much of the model-provider interaction. This is our own orchestration layer, not an OpenCode integration. Source references: [daemon](../../robothor/engine/daemon.py), [runner](../../robothor/engine/runner.py), [LLM client](../../robothor/engine/llm_client.py).

There are useful boundaries already: [tool admission](../../robothor/engine/tool_admission.py), [lifecycle hooks](../../robothor/engine/hook_registry.py), [tool registry](../../robothor/engine/tools/registry.py), and [durable calendar operations](../../robothor/engine/calendar_operations.py). However, admission is currently a runner mixin with session/config dependencies. Registering the raw tool handlers in another framework would not automatically preserve these controls.

The previous calendar work repaired a specific failure mechanism. It did not establish comparative performance against external runtimes, or production capacity under concurrent users. The existing [benchmark report](../runbooks/INTERACTIVE_REQUEST_PERFORMANCE.md) explicitly distinguishes model/Google fixtures from the separate live operation test.

**The options are different kinds of building blocks**

| Candidate | What upstream supplies | How our features attach | Main integration concern | Assessment for Robothor |
|---|---|---|---|---|
| Pydantic AI + selected Harness capabilities | Python agent loop, typed tools/results; optional planning, delegation and context capabilities | Tools, dependency injection and capability hooks | Core and Harness have different stability policies; existing state and authorization still need adapters | First Python prototype |
| OpenCode | Extensible agent runtime, sessions, events, model/tool customization and coding workflows | SDK/client plus plugins and controlled tool endpoints | JS runtime alongside our Python services; translate sessions, cancellation and permissions; choose a specific API generation | First comparison for adopting a more complete runtime |
| Deep Agents + LangGraph | Packaged context, filesystem and delegation behavior, with graph persistence underneath | Python tools, middleware, backends and checkpointers | More interacting layers; defaults and nested-agent policy propagation require inspection | Strong Python alternative if packaged behavior materially reduces our code |
| Pi | Small composable agent core, tool execution, streaming and steering | Explicit pre/post-tool and stop hooks; separate ecosystem packages | JS boundary; determine which additional packages are needed for our full lifecycle | Good alternative for a deliberately small core |
| OpenAI Agents SDK | Python/TypeScript agent loop, handoffs, streaming, state and integration surfaces | Application-owned tools, storage and approval logic | Mixed-provider feature parity must be demonstrated; some advanced features depend on the Responses path | Useful candidate, especially for an OpenAI-focused deployment |

These fit assessments are inferences from the following primary sources, not claims of measured speed or reliability.

**Pydantic AI: a promising match for our existing backend**

The core documents a typed extensible loop and a capability mechanism for tools, model settings, instructions and lifecycle interception. This provides plausible homes for our authorization bridge, memory retrieval and cost accounting without editing framework internals. [Overview](https://pydantic.dev/docs/ai/overview/), [capabilities](https://pydantic.dev/docs/ai/capabilities/overview/).

The separate Harness library provides composable planning, delegation, memory, context and execution features. We could adopt individual capabilities without enabling its complete coding-agent stack for a simple administrative assistant. Its API is explicitly versioned separately and can change across 0.x minor releases. [Harness and version policy](https://pydantic.dev/docs/ai/harness/).

The core's policy says intentional breaking changes are reserved for major releases, with exceptions including new event variants and certain instrumentation changes. This is helpful for maintainability but still requires tolerant event handling and upgrade tests. [Core version policy](https://pydantic.dev/docs/ai/project/version-policy/).

My assessment: probably the least disruptive first experiment because our services and tool implementations are already Python. That does not establish that it will produce fewer model calls or better decisions.

**OpenCode: viable as an embedded product component**

The V2 documentation describes `@opencode/sdk` as an embedded host and a separate network client. The embedded JavaScript path avoids a client/server network hop inside that application; it does not eliminate the boundary to our Python services. Plugins can customize agents, models and tools. [SDK](https://opencode.ai/v2/docs/build/sdk/), [plugins](https://opencode.ai/v2/docs/build/plugins/).

For us, a separate runtime process with a narrow authenticated tool interface is a plausible design. It can retain a coding specialist's upstream workflows while our backend owns calendar/CRM operations. We must examine built-in tools, filesystem scope, child sessions and every route by which a tool can execute; a plugin hook alone is not proof that our application authorization remains intact.

The published V1-to-V2 migration guide says plugin implementation code must be ported. We must choose one pinned release and API generation, and verify documentation examples against that release. [Migration guide](https://opencode.ai/v2/docs/build/plugins/migrate-v1/).

My assessment: a serious candidate, particularly for coding work. Its documented extension surface makes a permanent fork unnecessary in principle; whether our requirements fit that surface is what the prototype must establish.

**Deep Agents: evaluate the packaged harness, not only LangGraph**

Deep Agents documents context summarization, filesystem tools, delegation, memory and optional planning/skills. It is a closer comparison to a complete assistant harness than the lower-level graph runtime alone. [Overview](https://docs.langchain.com/oss/python/deepagents/overview).

It supports custom middleware and replacement of selected defaults. Its documentation also describes differences in middleware inheritance for subagents, which matters for our policy checks. [Customization](https://docs.langchain.com/oss/python/deepagents/customization).

LangGraph supplies persistent checkpointers and stores, with PostgreSQL options. Persistence still needs configuration and retention management. It does not substitute for an application operation ledger around external side effects. [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence).

A concrete integration limit: the Deep Agents filesystem-permission feature documents coverage for built-in filesystem tools, excluding custom/MCP filesystem tools and sandbox execution. Our business authorization must therefore remain independently enforced. [Permissions](https://docs.langchain.com/oss/python/deepagents/permissions).

My assessment: potentially the largest reduction in generic feature maintenance, at the cost of understanding more framework behavior and defaults.

**Pi and OpenAI Agents SDK**

Pi's current primary README documents a small agent core with streaming, configurable parallel/sequential tools, preflight hooks, abort/steering, and explicit stop-after-turn control. Those are directly relevant to preventing unnecessary continuation. The old `badlogic/pi-mono` URL now redirects to `earendil-works/pi`; use the current package documentation rather than old snippets. This core is not by itself evidence of a complete Robothor replacement. [Pi agent core](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md).

OpenAI's SDK documentation explicitly places deployment, tool implementations, storage and approvals in the application while the SDK runs the loop. It is distinct from the hosted Agents API. Non-OpenAI provider adapters are supported as an integration direction, but some advanced features require the Responses path. We would test our actual provider mix rather than assume identical behavior. [SDK](https://developers.openai.com/api/docs/guides/agents/sdk), [models and providers](https://developers.openai.com/api/docs/guides/agents/models).

**How custom and standard features coexist**

Recommended ownership boundary:

| Keep under Robothor's control | Prefer a maintained upstream implementation |
|---|---|
| Tenant/user identity and authorization | Generic model/tool iteration |
| Calendar, CRM and other domain services | Provider-specific message and streaming handling |
| Confirmation binding and operation receipts | Generic context-management strategies |
| Durable action records, duplicate suppression and reconciliation | Planning/delegation primitives where useful |
| Domain memory, provenance and retention rules | Supported extension/plugin loading |
| Channel delivery, user-visible outcomes and service-level targets | Runtime events and tracing integrations |

The runtime proposes a tool call. A Robothor-owned execution service validates its trusted run identity, checks policy, executes the operation, and records the result. All runtimes use that same service. Credentials and tenant identity must come from trusted server context, not model-written arguments.

Keep confirmed deterministic operations on their direct path. Changing the general runtime should not add another reasoning loop to a fully specified, authorized action. A coding request and an attendee addition need not load the same tools or planning machinery.

Use MCP where it makes integrations reusable across runtimes; it defines a host/client/server boundary for tools and context. It is not a replacement for an agent loop or the business operation's guarantees. In-process Python calls can remain more economical for internal tools. [MCP architecture](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture).

Use the Agent Skills format for portable instructions and resources where supported. A skill package is not the same as an executable framework plugin, and sharing a SKILL.md does not make permission semantics portable. [Agent Skills](https://agentskills.io/home).

Do not build a universal abstraction for every feature in every framework. Define only the boundary our product needs: start a run, emit progress, request a controlled tool, stop/steer, record usage, return a terminal outcome, and optionally save/resume a versioned checkpoint. Expose runtime-specific capabilities explicitly when needed.

**What staying current should mean operationally**

1. Pin a tested runtime version and dependency lock in a reproducible release. Do not let an active agent upgrade its own runtime during a user request.
2. Keep our integration in a small adapter and extensions. Record every required internal patch; an accumulating fork is a reason to reject or revisit the candidate.
3. Review upstream updates on a regular schedule, with expedited security fixes. Upgrade one runtime/provider combination in staging before broad rollout.
4. Replay a stable set of product tasks and failure injections for each update. Compare outcomes, latency, cost and policy behavior, not just whether imports succeed.
5. Record runtime version, model, adapter version, prompts and tool versions on each run. Keep checkpoints owned by that runtime/version until compatibility is demonstrated; do not blindly resume old state under a different harness.
6. Roll back new admissions while retaining operation records. Drain or finish old runs on their original version when state cannot migrate safely.

This is less work than owning the entire engine, but it is not zero maintenance. Upstream releases supply implementations and improvements; our contract tests establish whether those improvements work in Robothor.

The reviewed OpenCode, Pydantic AI core, Deep Agents and Pi repositories carry MIT licenses. That identifies their source-license posture; it does not establish the terms or cost of every optional hosted service, model provider or plugin. [OpenCode license](https://github.com/anomalyco/opencode/blob/dev/LICENSE), [Pydantic AI license](https://github.com/pydantic/pydantic-ai/blob/main/LICENSE), [Deep Agents license](https://github.com/langchain-ai/deepagents/blob/main/LICENSE), [Pi license](https://github.com/earendil-works/pi/blob/main/LICENSE).

**A bounded experiment before choosing**

Propose one integration experiment, not a full rewrite: our current engine as the control, a minimal Pydantic AI configuration, and OpenCode through its supported interface. Add Deep Agents if the Pydantic prototype still leaves too much generic harness code for us to own. Pi and OpenAI Agents SDK remain alternatives rather than five simultaneous integration projects.

First implement fake tools through the same authorization/operation boundary. Then exercise these representative cases:

| Case | Evidence required |
|---|---|
| Conversational meeting change | Correct target resolution, draft before confirmation, unchanged original fields, one authorized write, clear final receipt |
| Cancellation and changed instructions | No new write after cancellation is observed; in-flight outcomes explicitly reconciled |
| Crash or timeout around a write | Restart does not blindly repeat the external action |
| Research and CRM update | Useful evidence-based output; bounded exploration; only permitted writes |
| Coding task in a disposable checkout | Correct tested patch, isolated filesystem, visible progress |
| Long conversation and provider fallback | Original request survives compaction; fallback remains usable; no repeated no-progress loop |
| Concurrent isolated users | No identity/state crossover; interactive latency measured with background work present |
| Runtime upgrade | Our extension remains small; old state handled explicitly; critical behavior survives the version change |

Measure complete request latency separately from confirmation execution, first visible progress, p50/p95, model/tool call counts, token cost, duplicate writes, cancellation delay and task success. Start with at least 30 repetitions per case for a directional comparison; expand the sample before claiming tail-latency capacity. Evaluate representative concurrency levels separately from single-user performance.

Use the same model and tool implementations for a controlled comparison. Also report a second, separately labeled product comparison with each runtime's sensible defaults. Otherwise prompt, model and framework changes get mistaken for each other. No production meeting writes are necessary for this experiment.

Track maintenance as an outcome: custom code retired, adapter complexity, undocumented APIs used, time to adopt a second upstream release, and regressions introduced. The earlier idea of requiring a 20% speed win is too narrow for this broader decision. A runtime could be worthwhile with similar latency if it substantially reduces ownership and improves capability. Set acceptable performance/quality budgets before running the comparison; never trade away authorization or duplicate-write correctness for speed.

**What we should discuss**

My preferred direction is a maintained Python core for everyday Robothor work, with an optional coding runtime where it earns its place. The immediate decision is whether to investigate that split or insist on one runtime for all tasks. We do not need to choose a permanent framework now.

The experiment should end with a recommendation supported by traces and upgrade experience: adopt one candidate, narrow its role to a specialist, or retain the current runtime while fixing the specific integration blockers. Research establishes that viable extension-based designs exist. It does not yet establish which one is fastest, most reliable, or cheapest for our actual workload.
