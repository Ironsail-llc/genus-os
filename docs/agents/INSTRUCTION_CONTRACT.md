# Agent Instruction File Contract

Every agent instruction file (.md) is loaded as the agent's system prompt.
It MUST follow this structure for the engine and coordination system to work.

## Required Sections

### Identity (first line)

```
# {Agent Name}
```

Followed by: `You are **{Agent Name}**, an autonomous agent.`

### Your Role

What this agent does. 2-3 sentences maximum. Be specific about scope and boundaries.

### Tasks

Numbered list of specific actions to take each run. Be explicit about:
- What inputs to read (files, task inbox, memory blocks)
- What processing to perform (classify, analyze, compose, resolve)
- What outputs to produce (status file, tasks, notifications, files)

### Output

How and where to write results. MUST specify:
- Status file path (from manifest's `status_file` field)
- Format: one-line summary + ISO 8601 timestamp
- Example: `All clear. No new items. — 2026-03-01T14:00:00Z`

## Required Behaviors (conditional)

### If `task_protocol: true` in manifest

The instruction file MUST tell the agent to:
1. Call `list_my_tasks` at the start of every run
2. Set each task to `IN_PROGRESS` before processing
3. Call `resolve_task` with a summary when done

### If `shared_working_state: true` in manifest

The instruction file MUST tell the agent to:
1. Call `append_to_block` with a one-line summary at the end of every run

### If `review_workflow: true` in manifest

The instruction file MUST tell the agent to:
1. Set tasks to `REVIEW` status (not `DONE`) when human approval is needed

### If the agent has the vault tools (`role: main` and the operator's delivery agents)

The instruction file MUST carry a **When handed a credential** section saying:

> When someone gives you an API key, token or password:
>
> 1. **Store it**: call `vault_set` with the key and the value. Use the
>    conventional key name — `providers/<vendor>/api_key` for a model provider,
>    `channels/<channel>/<field>` for a channel, the lower-cased variable name
>    otherwise.
> 2. **Prove it**: call `vault_test` with the same key. It answers `{ok,
>    identity_hint, error_class}`. Read the identity hint: a token that works
>    but belongs to the wrong account is the failure a bare "ok" hides.
> 3. **Answer with the fingerprint and the test result** — "stored as
>    `sha256:1a2b3c4d`, authenticates as `octocat`" — so they can confirm the
>    right credential landed.
>
> Never echo the value back, in any form. Never write it to a memory block, a
> note, a file, a CRM record or a task. `vault_get` does not return values and
> you do not need one: a tool that needs a credential holds it itself, and an
> agent whose shell commands need one is granted the NAME in its manifest's
> `secrets:` list.
>
> You cannot restart a service and you never need to — a `vault_set` takes
> effect immediately.

**Anti-pattern.** An instruction file that tells an agent to "read the token
with `vault_get` and use it" describes a tool that no longer exists, and
describes a credential in a context window, which is the thing this contract
exists to prevent.

## Optional Sections

- **Rules** — Guardrails and constraints (e.g., "never send emails without REVIEW")
- **Coordination** — How to create tasks for downstream agents
- **Escalation** — When to escalate vs handle autonomously
- **Context** — What warmup files and memory blocks to expect

## Anti-Patterns

- Do NOT reference specific Telegram chat IDs (use delivery config in manifest)
- Do NOT hardcode file paths to other agents' status files (use task_protocol)
- Do NOT assume specific agent names exist (use generic task routing)
- Do NOT reference `localhost` URLs (engine blocks loopback in web_fetch)

---

Updated: 2026-02-28
