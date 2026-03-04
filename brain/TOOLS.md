# TOOLS.md — Shared Tool Reference

Environment-specific notes for agents. Per-agent tools are documented in each agent's instruction file.

---

## Google Workspace (gog CLI)

- **Account:** robothor@ironsail.ai
- **Target calendar:** philip@ironsail.ai
- **Auth:** Environment variables (SOPS-encrypted, injected at runtime)

### Gmail

```bash
# Read a thread (use --full --json to get message IDs for threading)
gog gmail thread get <threadId> --account robothor@ironsail.ai --full --json

# Send a reply (use --reply-to-message-id for cross-account threading)
gog gmail send --reply-all --reply-to-message-id <lastMessageId> \
  --subject "Re: <original subject>" \
  --body-html "<html reply>" --account robothor@ironsail.ai --no-input
# Add --cc philip@ironsail.ai ONLY if Philip is not already in the thread

# Mark as read
gog gmail thread modify <threadId> --account robothor@ironsail.ai --remove UNREAD
```

### Calendar

```bash
# List events
gog calendar list philip@ironsail.ai --account robothor@ironsail.ai --json --from today --to tomorrow

# Create event — get current UTC offset first, then use it in timestamps
OFFSET=$(date +%:z)   # e.g. -05:00 (EST) or -04:00 (EDT)
gog calendar create philip@ironsail.ai --account robothor@ironsail.ai --json \
  --summary "Title" --from "2026-02-23T15:00:00${OFFSET}" --to "2026-02-23T16:00:00${OFFSET}" \
  --description "Notes" --location "Office" --attendees "person@example.com" \
  --reminder popup:30m --with-meet

# Delete event
gog calendar delete philip@ironsail.ai <eventId> --account robothor@ironsail.ai --force
```

Key flags: `--summary`, `--from`/`--to` (RFC3339 with timezone offset — **always derive dynamically via `date +%:z`**, never hardcode), `--all-day`, `--attendees` (comma-separated), `--with-meet`, `--rrule`, `--reminder`, `--json`

> **Philip is in America/New_York.** EST = `-05:00` (Nov–Mar), EDT = `-04:00` (Mar–Nov). Use `date +%:z` to get the current offset — do NOT hardcode it.

---

## Web & Browser Tools

**You have full internet access. NEVER tell Philip to open a browser or visit a URL — do it yourself.**

### web_search — Internet Search

```
web_search("best Italian restaurants Broad Channel NY")
web_search("NVIDIA Grace Blackwell specs")
web_search("weather NYC tomorrow")
```

Uses Brave Search. Returns structured results with titles, URLs, and snippets. Use for any factual lookup, price checks, business hours, news, research.

### web_fetch — Read Any URL

```
web_fetch("https://example.com/article")
```

Fetches a URL and returns its content as clean markdown text. Use to read articles, documentation, product pages, booking confirmations, or any page Philip shares a link to. Does NOT execute JavaScript — use `browser` for JS-heavy pages.

**Note:** Blocks loopback addresses (localhost/127.0.0.1) for security. Use registered tools for internal services.

### browser — Full Browser Automation

Headless Chromium via Playwright. Profile: `openclaw` (isolated, managed). Use for anything that requires interaction: logins, forms, multi-step flows, JavaScript-heavy sites, screenshots, purchases, bookings.

**Workflow:**
1. `browser(action="start", profile="robothor")` — launch the browser
2. `browser(action="navigate", targetUrl="https://site.com")` — go to URL
3. `browser(action="snapshot")` — read page content (ARIA tree with element refs)
4. `browser(action="act", request={kind: "click", ref: "e12"})` — click element by ref from snapshot
5. `browser(action="act", request={kind: "fill", ref: "e15", fields: [{ref: "e15", value: "robothor@ironsail.ai"}]})` — fill form
6. `browser(action="act", request={kind: "type", ref: "e15", text: "hello"})` — type text
7. `browser(action="screenshot")` — capture visual state
8. `browser(action="stop")` — close browser when done

**Actions:** status, start, stop, profiles, tabs, open, focus, close, snapshot, screenshot, navigate, console, pdf, upload, dialog, act

**Act kinds:** click, type, press, hover, drag, select, fill, resize, wait, evaluate (JS)

**When to use which:**
| Need | Tool |
|------|------|
| Quick factual lookup | `web_search` |
| Read a URL's content | `web_fetch` |
| Login, forms, JS-heavy sites | `browser` |
| Take a screenshot of a page | `browser` |
| Multi-step web workflow | `browser` |

---

## RAG Memory

Your long-term memory. Contains emails, meeting transcripts, calendar events, daily notes, contact profiles, and operational findings. Data is ~10 min fresh (continuous ingestion pipeline).

**Tools:**
- `search_memory(query, limit=10)` — semantic search across all facts. Use natural language: "Caroline cashflow report", "meeting with Craig last week", "vision alerts today"
- `store_memory(content, content_type)` — store new facts. Types: `conversation`, `email`, `decision`, `preference`, `technical`
- `get_entity(name)` — knowledge graph lookup. Returns relationships, linked entities, identifiers. Use for people and companies.

**When to use:**
- Before classifying unknown senders (cheaper than spawning a sub-agent)
- Before drafting replies (find prior context on the topic)
- Before escalating (RAG may have the answer)
- After significant actions (store what you did for future reference)

---

## Deep Reasoning (RLM)

### deep_reason — Heavy-Context Analysis

```
deep_reason(query="Summarize all email threads with Craig from the past month and identify action items")
deep_reason(query="What are the recurring themes in vision alerts?", context_sources=[{"type": "memory", "query": "vision alert", "limit": 50}])
deep_reason(query="Analyze this document", context_sources=[{"type": "file", "path": "brain/memory/triage-inbox.json"}])
```

Runs a Recursive Language Model (RLM) session that writes Python code in a REPL to navigate, chunk, and recursively query large context. Dramatically outperforms vanilla LLM calls on heavy-context tasks.

- `query`: The reasoning question to answer (required)
- `context`: Optional raw text to include directly
- `context_sources`: Optional list of sources to pre-load:
  - `{"type": "memory", "query": "...", "limit": 10}` — semantic memory search
  - `{"type": "file", "path": "..."}` — read a file (50KB limit)
  - `{"type": "block", "block_name": "..."}` — read a memory block
  - `{"type": "entity", "name": "..."}` — knowledge graph lookup

**Models:** Sonnet 4.6 (root — writes REPL code) + Haiku 4.5 (sub — bulk queries). Configurable via `ROBOTHOR_RLM_ROOT_MODEL` / `ROBOTHOR_RLM_SUB_MODEL` env vars.

**Cost:** $0.50-$2.00 per call (budget capped at $2.00, timeout at 240s). Inside the REPL, the LLM can also call `search_memory`, `get_entity`, `read_file`, and `memory_block_read`.

**When to use:**
- Multi-source analysis requiring cross-referencing (email threads + calendar + contacts)
- Questions spanning more context than fits in a single prompt
- Complex reasoning that benefits from programmatic navigation

**When NOT to use:**
- Simple factual lookups — use `search_memory` directly
- Single-file questions — use `read_file` directly
- Anything answerable from the warmup preamble

Returns: `response`, `execution_time_s`, `cost_usd`, `context_chars`, `trajectory_file`

Trajectory logs saved to `brain/memory/rlm-traces/` (gitignored).

### /deep — Interactive RLM Command

```
/deep What calendar conflicts do I have this week?
/deep Summarize all email threads with Craig and identify action items
/deep Analyze my recent interactions with John across all channels
```

Interactive command that calls `deep_reason` directly — bypasses the LLM loop. Available from every surface:

- **Telegram:** `/deep <question>`
- **Helm:** Brain toggle button (purple) or `Ctrl+Shift+D` then send
- **TUI:** `/deep <question>`
- **CLI:** `robothor engine run main --deep -m "question"`

One-shot flow: submit query → RLM runs (progress updates every 5s) → result returned with cost/time. No approve/reject cycle.

**Mutual exclusivity:** `/deep` and `/plan` cannot be active simultaneously as modes, but plan mode CAN invoke `deep_reason` as a tool during exploration (it's in `READONLY_TOOLS`).

**Cost tracking:** `GET /costs/deep?hours=24` returns total calls, total cost, avg cost, avg duration.

---

## Model Selection (summary)

- **Interactive/agent tasks:** Opus (primary) → Gemini Pro (fallback)
- **Massive context (>200K):** Gemini 2.5 Pro
- **RAG/batch/local:** Qwen3-Next 80B (zero API cost)
- **Embeddings/reranking:** Qwen3-Embedding + Qwen3-Reranker (always on)

Agents don't choose models — model is set per-agent in jobs.json.

---

## CRM Bridge

- **Port:** 9100
- **Health:** `crm_health()` tool or `curl localhost:9100/health`
- All CRM tools route through Bridge automatically

---

## Voice & Calling

**Phone:** +1 (413) 408-6025 (Twilio)
**Service:** robothor-voice.service (port 8765, voice.robothor.ai)
**AI Model:** Gemini 2.5 Flash Native Audio (real-time audio-to-audio via Vertex AI)

### make_call — Outbound Phone Calls

```
make_call(to="+13475551234", recipient="John Smith", purpose="Confirm tomorrow's 3pm meeting")
```

Initiates an outbound call. Gemini Live handles the real-time AI conversation with context about who is being called and why.

- `to`: Phone number in E.164 format (+1XXXXXXXXXX)
- `recipient`: Name of person being called (used in greeting)
- `purpose`: Why Robothor is calling (injected into system prompt)

Returns: `call_sid`, `status` (initiated/ringing/in-progress/completed)

**Inbound calls** arrive via Twilio ConversationRelay webhook → same Gemini Live bridge, using Robothor's default persona.

---

## What Goes Here

Environment-specific notes: camera names, SSH hosts, preferred TTS voices, device nicknames. Keep it minimal — agents read this file every run.
