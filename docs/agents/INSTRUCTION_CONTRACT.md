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

## Optional Sections

- **Rules** — Guardrails and constraints (e.g., "never send emails without REVIEW")
- **Coordination** — How to create tasks for downstream agents
- **Escalation** — When to escalate vs handle autonomously
- **Context** — What warmup files and memory blocks to expect

## Anti-Patterns

- Do NOT reference specific Telegram chat IDs (use delivery config in manifest)
- Do NOT hardcode file paths to other agents' status files (use task_protocol)
- Do NOT assume specific agent names exist (use generic task routing)
- Do NOT include `HEARTBEAT_OK` in worker agents (supervisor-only pattern)
- Do NOT reference `localhost` URLs (engine blocks loopback in web_fetch)

---

Updated: 2026-02-28
