---
name: memory-fallback-to-notes
description: "Fallback persistence paths when primary tools (store_memory, create_note,\
  \ write_file) are unavailable \u2014 memory blocks, task resolution fields, CRM\
  \ notes, and reporting, in priority order."
tags:
- memory
- workaround
- persistence
- crm-notes
---

# Memory & Persistence Fallback

Use this skill when **any primary persistence tool** is unavailable or failing: `store_memory`, `create_note`, `write_file`, `append_to_block`, or similar.

## When to Use
- `store_memory` times out after 120s
- `create_note`, `write_file`, or `append_to_block` are not in your tool set for this run
- `search_memory` returns errors or empty results when data should exist
- Any persistence tool returns errors on first attempt

## Critical Rule: Detect Once, Don't Retry

**If a persistence tool is missing or fails once, DO NOT retry it in the same run.** Mark it unavailable and move to the next fallback path. Retrying a broken persistence path is the most common source of wasted iterations.

Detection checklist (run at start of every worker run):
1. Do I have `create_note`? If no → skip CRM note creation for this run.
2. Do I have `write_file`? If no → skip status file updates.
3. Do I have `append_to_block` / `memory_block_write`? If no → skip memory block updates.
4. Do I have `store_memory`? If no → use the fallback paths below.

**Hard stop:** If you have already attempted a persistence tool and it failed, do NOT attempt it again. Move to the next fallback. If you've exhausted all fallbacks, report the outcome to the user/task resolution and stop.

## Steps

### 1. Detect the failure
If a persistence tool fails once or is absent from your tool set, skip it for the rest of the run. Don't retry more than once — the tool is unavailable, not slow.

### 2. Choose a fallback path (priority order)

**Path A: Task resolution fields (preferred for task outcomes)**
When resolving a CRM task, put the findings directly in the `resolution` parameter of `resolve_task()`. This durably captures the outcome alongside the task record — no separate note needed.

```
resolve_task(id="<uuid>", resolution="<detailed findings and outcome>")
```

Best for: task-specific results that belong with the task record.

**Path B: Memory blocks (preferred for run-level context)**
Use `memory_block_write` / `append_to_block` for session summaries, working state, and operational context. These are already in warmup context on the next session — no extra retrieval needed.

```
memory_block_write(block_name="working_context", content="<updated summary>")
append_to_block(block_name="shared_working_state", entry="<agent>: <one-liner summary>")
```

Best for: run summaries, status updates, cross-agent coordination state, operational findings.

**Path C: CRM notes (for durable facts and contact-linked info)**
Use `create_note` for facts that need to survive long-term and be linked to people/companies:
```
create_note(
  title="<Topic> — <Date>",
  body="<Structured content with markdown headers>",
  personId="<optional: link to contact>",
  companyId="<optional: link to company>"
)
```

Best for: contact-specific findings, company research, decisions that don't fit a task or memory block.

**Path D: Status file via exec (for worker status)**
When `write_file` is unavailable, try writing status via `exec`:
```
exec(command="cat > /tmp/agent_status.md << 'EOF'\n# Status\n...\nEOF")
```

Best for: worker status files when `write_file` is not in tool set.

**Path E: Report and stop (last resort)**
If ALL persistence paths are unavailable, report the outcome in your final message and stop. Do not loop.

**Formatting rules for notes:**
- Use bold headers (`**Section**`) for scanability
- Include dates and context in the title
- Link to person/company when relevant (enables `list_notes(personId=...)` queries)
- Keep individual notes focused on one topic — don't dump everything into one mega-note

### 3. For enriched contacts — persist what you HAVE, not what you're missing

When enriching a contact and not all fields are found:
1. **Create the note with available data.** Even a partial bio is valuable — it captures RAG-sourced context that took effort to gather.
2. **Document missing fields in the note body** — e.g., "## Gaps\n- Email: not found (Apollo 403, web search empty)\n- Phone: not found"
3. **Mark the task with what was accomplished**, not what failed. Resolution should say "Bio note created; email/phone/LinkedIn unfilled" not "Enrichment failed."

### 4. Retrieve from fallbacks
- **Task resolution:** `get_task(id="<uuid>")` — the `resolution` field persists
- **Memory blocks:** `memory_block_read(block_name="working_context")` — always in warmup, instant
- **CRM notes:** `list_notes(personId=...)`, `list_notes(companyId=...)`, or `search_records(query="<keyword>", objectName="crm_notes")`

### 5. Log the workaround (only on first detection per session)
If persistence tools are degraded, note it:
```
append_to_block(block_name="operational_findings", entry="create_note unavailable this run — using task resolution fallback")
```

### 6. Recovery check
Before any workaround gets stale (>7 days), test the primary tools. If they work, resume normal usage.

## Anti-Loop Discipline

**Signs you are looping on persistence:**
- You've attempted the same tool 2+ times with the same error or absence
- You've written status files that say the same thing 2+ times
- You're at iteration 5+ and haven't advanced past "try to save findings"

**If you see these signs:** STOP. Pick the highest-priority fallback that IS available, persist what you have, and finish the run. A partial save is infinitely better than a loop that burns budget and produces nothing.

## Limitations vs Primary Tools
- **No semantic search** — blocks and notes use exact-match, not embeddings
- **No automatic fact extraction** — you must structure the content manually
- **Memory blocks have size limits** — each block has a max_chars cap; use append_to_block for incremental updates
- **CRM notes have no cross-session auto-injection** — require explicit retrieval
