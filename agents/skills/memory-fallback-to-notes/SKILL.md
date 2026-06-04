---
name: memory-fallback-to-notes
description: "Workaround for store_memory timeouts \u2014 persist content via CRM\
  \ notes instead of the memory/vector pipeline."
tags:
- memory
- workaround
- persistence
- crm-notes
---

# Memory Fallback to CRM Notes

Use this skill when `store_memory` is timing out (>120s) or unavailable.

## When to Use
- `store_memory` times out after 120s
- `search_memory` returns errors or empty results when data should exist
- The embedding/vector pipeline appears stalled

## Steps

### 1. Detect the failure
If `store_memory` times out once, skip it for the rest of the run. Don't retry more than once — the pipeline is down, not slow.

### 2. Store via CRM notes
Use `create_note` with structured content:
```
create_note(
  title="<Topic> — <Date>",
  body="<Structured content with markdown headers>",
  personId="<optional: link to contact>",
  companyId="<optional: link to company>"
)
```

**Formatting rules for notes:**
- Use bold headers (`**Section**`) for scanability
- Include dates and context in the title
- Link to person/company when relevant (enables `list_notes(personId=...)` queries)
- Keep individual notes focused on one topic — don't dump everything into one mega-note

### 3. Retrieve via notes
Use `list_notes` with filters:
- `list_notes(personId=...)` — all notes for a contact
- `list_notes(companyId=...)` — all notes for a company
- `list_notes(limit=N)` — recent notes

For keyword search across notes: `search_records(query="<keyword>", objectName="crm_notes")`

### 4. Log the workaround
Create a note documenting that the memory pipeline is degraded:
```
create_note(
  title="Operational: store_memory timeout workaround (<date>)",
  body="store_memory timing out. Using CRM notes as fallback. Remove this note when pipeline recovers."
)
```

### 5. Recovery check
Before the workaround note gets stale (>7 days), test `store_memory` with a small payload. If it works, delete the operational note and resume normal memory tools.

## Limitations vs store_memory
- **No semantic search** — notes use keyword/exact-match, not embeddings
- **No cross-session auto-injection** — notes aren't in warmup context like memory blocks
- **Max body size** — keep notes under ~50KB for reliability
- **No automatic fact extraction** — you must structure the content manually

## Recovery Test
To check if `store_memory` has recovered:
```
store_memory(content="Test: pipeline health check", content_type="technical")
```
If it returns within 5s, the pipeline is back. Resume normal usage.
