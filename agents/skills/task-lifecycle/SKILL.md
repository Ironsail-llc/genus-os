---
name: task-lifecycle
description: "Create, advance, and resolve CRM tasks correctly \u2014 respecting status\
  \ transitions, handling retries, and avoiding common API pitfalls."
tags:
- crm
- tasks
- lifecycle
- workflow
- api-patterns
---

# Task Lifecycle — CRM Task Management

Correct patterns for creating, advancing, reviewing, and resolving tasks in the `crm_tasks` system.

---

## Status Transition Machine

```
TODO → IN_PROGRESS → REVIEW → DONE
                       ↘ IN_PROGRESS (via reject_task)
```

**Hard rules enforced by the API:**
- `resolve_task(id)` ONLY works from `IN_PROGRESS` or `REVIEW`. Calling it on a `TODO` task returns **422**: `"Cannot resolve from TODO — must be IN_PROGRESS or REVIEW first"`.
- `approve_task(id)` ONLY works from `REVIEW`.
- `reject_task(id)` reverts `REVIEW` → `IN_PROGRESS`.
- Invalid transitions return 422 with a descriptive error message.

---

## Correct Resolution Sequence

To resolve a task that is currently in `TODO` status:

```
1. update_task(id, status="IN_PROGRESS")
2. resolve_task(id, resolution="...")
```

**Pitfall:** Attempting `resolve_task` directly on a `TODO` task fails silently with 422. Do NOT retry the same call — advance the status first.

**Pitfall:** `update_task` IS available in the standard toolset. If you catch yourself writing "I don't have update_task," check the tool list. It's there.

---

## Early Task Validation (Step 0)

**Before operating on any task referenced in a conversation, summary, or prior context, verify it exists in the CRM.** References from prior sessions, summaries, or external sources may be stale, already resolved, or phantom.

**Validation sequence:**
```
1. get_task(id) — if you have a UUID
2. search_records(query="<title keywords>") — if you have a title fragment
3. list_tasks(tags=["<tag>"]) — if you have a tag
4. search_memory(query="<task description>") — as fallback
```

**If none return a match:** Log it and move on. Do NOT create a task to "investigate the missing task" — that's recursive waste. Treat it as already resolved or a stale reference.

**Pitfall:** Task references in conversation summaries, digests, and prior-session context are NOT authoritative. They may reference tasks that were resolved between sessions, auto-cleaned by maintenance scripts, or hallucinated by a prior agent. Always validate before acting.

---

## Creating Tasks

- `create_task(title, ...)` always starts in `TODO` by default.
- Creating one task does NOT affect the status of other tasks. There is no eventual-consistency side effect — each task is independent.
- Use `assignedToAgent` for routing to specific agents (e.g., `email-responder`, `agent-architect`).
- Use `tags` for categorization (see AGENTS.md for standard tag vocabulary).

---

## Common Pitfalls

### 1. Resolving TODO tasks directly
```
// WRONG — returns 422
resolve_task(id, resolution="done")

// RIGHT
update_task(id, status="IN_PROGRESS")
resolve_task(id, resolution="done")
```

### 2. Assuming create_task affects sibling tasks
Creating a new task (e.g., a follow-up) does NOT change the status of related tasks. Each task has an independent lifecycle.

### 3. Forgetting `update_task` exists
If your toolset includes `update_task`, use it. Don't conclude "I can't advance status" without checking. The tool accepts: `id`, `status`, `title`, `body`, `dueAt`, `personId`, `companyId`, `assignedToAgent`, `priority`, `tags`, `resolution`, `requiresHuman`.

### 4. Retrying the same failing call
If `resolve_task` returns a 422 due to status, the fix is NOT to retry. The fix is to advance the status first. Retrying wastes a tool call and teaches you nothing new.

### 5. Bulk resolution pattern
When resolving multiple tasks from `TODO`:
```
for task in tasks:
    update_task(task.id, status="IN_PROGRESS")
    resolve_task(task.id, resolution="...")
```
Each pair is two sequential calls. Batch the `update_task` calls if the API supports it, otherwise do them in sequence.

### 6. Acting on phantom task references
A task ID or title from a conversation summary, digest, or prior session context may not exist. **Always validate first** (see "Early Task Validation" above). If the task is gone, log it concisely and continue with remaining work — don't escalate or block on it.

### 7. Tool substitution when a tool is unavailable
If a tool call fails (e.g., `edit` tool errors), don't log it as a blocker. Diagnose the failure, identify the alternative tool (e.g., `write_file` for file edits), and complete the work with the substitute. Track the substitution but treat it as resolved, not as an error that needs escalation.

---

## Review Workflow

- `REVIEW` status means a task needs approval from a different agent/user than the assignee.
- `approve_task(id, resolution)` → `DONE`
- `reject_task(id, reason, changeRequests?)` → `IN_PROGRESS` (with optional subtasks for change requests)
- Only `main` and `helm-user` have approve/reject permissions.

---

## SLA Deadlines

Derived from priority at creation:
| Priority | SLA |
|----------|-----|
| urgent | 30 min |
| high | 2 hours |
| normal | 8 hours |
| low | 24 hours |

---

## Escalation Rules

Before escalating to the operator (creating a task with `tags=["needs-operator"]`), check:
- Tool errors → retry once, then log and continue
- Missing data → skip that item, continue with others
- Stale items (>48h) → skip entirely
- Already escalated similar → don't duplicate

the operator should only see things requiring human judgment.

---

## Handling Developer Verification Failures

When a developer verification or benchmark flags issues, evaluate whether you already addressed them:

1. **Read the flagged issues carefully** — they may describe problems you caught and adapted to during execution
2. **If you already diagnosed and resolved the issue** (e.g., tool error → substituted alternative), respond with a clear explanation of what happened and why it's handled
3. **Don't re-execute work that already succeeded** just because a verification script flagged an intermediate error
4. **If the verification reveals a genuine gap** (e.g., data loss, wrong result), address it — don't rationalize

The pattern: treat verification as a signal to evaluate, not an automatic retry trigger.

---

## References

- `AGENTS.md` — Full task coordination spec, tag vocabulary, routing patterns
- `brain/WORKER.md` — Worker agent task processing loop
