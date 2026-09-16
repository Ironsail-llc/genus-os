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

### If the agent has the vault tools (`v2.credentials: operator`)

The instruction file MUST carry a **When handed a credential** section saying:

> When someone gives you an API key, token or password:
>
> 1. **Store it**: call `vault_set` with the key and the value. Use the
>    conventional key name — `providers/<vendor>/api_key` for a provider key or
>    an API token (`GITHUB_TOKEN` included), `channels/<channel>/<field>` for a
>    channel setting, the lower-cased variable name otherwise. `vault_set`
>    answers with `readable_as`: the variables whose readers will find the row.
>    **If that list is empty, the credential is stored where nothing looks** —
>    re-store it under a key the list names.
> 2. **Prove it**: call `vault_test` with the same key. It answers `{ok,
>    identity_hint, error_class}`. Read the identity hint: a token that works
>    but belongs to the wrong account is the failure a bare "ok" hides.
> 3. **Answer with the fingerprint and the test result** — "stored as
>    `b2:1a2b3c4d`, authenticates as `octocat`" — so they can confirm the
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

## Identity

**The assistant is a separate principal.** It has its own Google account: its
own email address, its own calendar, its own Drive. The operator has theirs.
They are not the same account and nothing about the tooling makes them look
different unless the instructions say so.

So, in an instruction file:

* **"my calendar", "my email", "my files" in the operator's words mean the
  operator's.** Never the assistant's.
* **`primary` is the assistant's own calendar.** Anything the operator is meant
  to see goes to the operator's calendar — the calendar tools default there, and
  reaching the assistant's own takes an explicit `calendar: "own"`.
* **A result is only "done for the operator" when it landed in the operator's
  account.** An event on the assistant's calendar with the operator as an
  attendee and no invitation sent is not a scheduled meeting; it is a private
  note the operator will never see. Report what the tool returns — for calendar
  writes that is `calendar`, `invitations_requested` and `htmlLink` — not "done".

This is not hypothetical. On 2026-09-16 an assistant planned the operator's
itinerary, created every leg on its own calendar, added him as an attendee,
sent no invitations, and told him it was on his calendar. Every individual step
was locally reasonable for an agent that believed there was one calendar.

The engine says this in its own context turn on **every** run, so an
instruction file does not have to repeat it — and must not hardcode either
address. Where both are configured the turn names them; where neither is, it
still says the accounts are separate, because that is the part an
unconfigured instance needs most and it is true without knowing either
address.

## Tools

Name tools by their **registered names** — `gws_gmail_reply`, not "the reply
tool", and never a CLI command when a tool exists for the job. An instruction
that says `gog gmail send` is telling the agent to go around the
do-not-contact check, the duplicate-reply guard, the threading and the
benchmark gate, all of which live in the tool and none of which live in the
shell.

An instruction file **may only name tools the manifest grants**. The agent
cannot see its own manifest: a tool it was told to use and does not have simply
fails mid-turn, and what the model does next is find another way to the same
end. `genus doctor --category agents` reports every mismatch
(`agents.tools_named_but_not_granted`), in both directions — a tool named in
prose and absent from `tools_allowed`, and a `tools_allowed` entry that
resolves to no registered schema.

If the manifest declares `heartbeat.instruction_file`, the same rule applies to
`heartbeat_tools_allowed`: the heartbeat run has its own, usually smaller,
toolset.

**Do not describe the toolset as a fixed list.** A broad agent's tools are
loaded on demand — the model is shown a small core and reaches the rest with
`tool_search` then `tool_call`, and the engine tells it so each turn. Writing
"you have these 40 tools" into an instruction file contradicts what the run
actually hands it. See [Tools](../TOOLS.md).

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

Updated: 2026-09-16
