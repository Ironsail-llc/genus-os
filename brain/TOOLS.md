# TOOLS.md — Shared Tool Reference

Environment-specific notes for agents. Per-agent tools are documented in each agent's instruction file.

---

## LLM API Keys & Model Switching

- **OpenRouter** uses a SINGLE API key (`OPENROUTER_API_KEY` env var) for ALL models routed through it. Any `openrouter/*` model works with the same key — no per-model keys exist.
- API keys are loaded from SOPS-encrypted secrets (`/etc/robothor/secrets.enc.json`) into environment variables at runtime. They are NOT in the vault.
- The vault (`vault_get`/`vault_set`) is for application-level secrets (webhook tokens, service passwords). Do NOT look for LLM API keys there.
- **To switch models**: edit the YAML manifest in `docs/agents/<agent>.yaml` — change the `model.primary` field. No new keys needed for any OpenRouter model.
- After editing manifests, restart the engine: the scheduler reloads configs on startup.

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

Key flags: `--summary`, `--from`/`--to` (RFC3339 with timezone offset — **always derive dynamically via `date +%:z`**, never hardcode), `--all-day`, `--attendees` (comma-separated), **`--with-meet` (REQUIRED — always include on every event)**, `--rrule`, `--reminder`, `--json`

> **Philip is in America/New_York.** EST = `-05:00` (Nov–Mar), EDT = `-04:00` (Mar–Nov). Use `date +%:z` to get the current offset — do NOT hardcode it.

> **Every event MUST include `--with-meet`.** All meetings get a Google Meet link, no exceptions.

> **Scheduling link:** When someone needs to book time with Philip, share: `https://calendar.app.google/TLqVaiyMTtcdLY7E6`

---

## Google Workspace (gws — Native Tools)

**Preferred over gog+exec.** These are first-class engine tools that return structured JSON — no text parsing or regex needed.

### Gmail

| Tool | Purpose |
|------|---------|
| `gws_gmail_search` | Search inbox with Gmail query syntax (e.g. `is:unread from:alice newer_than:1d`) |
| `gws_gmail_get` | Get a message or thread by ID (returns headers, body, labels) |
| `gws_gmail_send` | Send a new email or reply to a thread |
| `gws_gmail_modify` | Add/remove labels (mark read/unread, archive, star) |

### Calendar

| Tool | Purpose |
|------|---------|
| `gws_calendar_list` | List events in a date range (returns structured event data) |
| `gws_calendar_create` | Create a calendar event with attendees, location, description |
| `gws_calendar_delete` | Delete a calendar event by ID |

### Google Chat

| Tool | Purpose |
|------|---------|
| `gws_chat_send` | Send a message to a Google Chat space |

> **Migration note:** `gog` CLI commands via `exec` still work as fallback. Use `gws_*` tools when available — they're faster, return structured data, and don't need shell parsing.

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

Chromium via Playwright on virtual display (:99). Use for anything that requires interaction: logins, forms, multi-step flows, JavaScript-heavy sites, screenshots, purchases, bookings.

**Workflow:**
1. `browser(action="start")` — launch the browser
2. `browser(action="navigate", url="https://site.com")` — go to URL
3. `browser(action="snapshot")` — read page content (ARIA accessibility tree with element refs)
4. `browser(action="act", request={kind: "click", selector: "button.submit"})` — click element by CSS selector
5. `browser(action="act", request={kind: "fill", selector: "#email", value: "robothor@ironsail.ai"})` — fill form field
6. `browser(action="act", request={kind: "type", text: "hello"})` — type text via keyboard
7. `browser(action="act", request={kind: "press", key: "Enter"})` — press key
8. `browser(action="screenshot")` — capture visual state
9. `browser(action="stop")` — close browser when done

**Actions:** status, start, stop, navigate, snapshot, screenshot, act, tabs, pdf, console, evaluate

**Act kinds:** click (by selector or x,y), fill, type, press, scroll, select

**When to use which:**
| Need | Tool |
|------|------|
| Quick factual lookup | `web_search` |
| Read a URL's content | `web_fetch` |
| Login, forms, JS-heavy sites | `browser` |
| Take a screenshot of a page | `browser` |
| Multi-step web workflow | `browser` |

---

## Desktop Control Tools (Computer Use)

Virtual display (Xvfb :99, 1280x1024) with mouse/keyboard control via xdotool. Use for interacting with any GUI application — LibreOffice, file managers, custom desktop apps, or anything not accessible via API.

**Core loop:** screenshot → decide → act → screenshot → verify

| Tool | Purpose |
|------|---------|
| `desktop_screenshot` | Capture display as base64 PNG |
| `desktop_describe(prompt?)` | Screenshot + VLM description of what's on screen |
| `desktop_click(x, y)` | Left click at coordinates |
| `desktop_double_click(x, y)` | Double click |
| `desktop_right_click(x, y)` | Right click / context menu |
| `desktop_type(text)` | Type text at cursor |
| `desktop_key(key)` | Press key combo (e.g. `ctrl+a`, `Return`, `alt+F4`) |
| `desktop_scroll(direction, clicks)` | Scroll up/down |
| `desktop_drag(start_x, start_y, end_x, end_y)` | Drag and drop |
| `desktop_mouse_move(x, y)` | Move cursor without clicking |
| `desktop_window_list` | List open windows (IDs, titles, positions) |
| `desktop_window_focus(window_id)` | Bring a window to front |
| `desktop_launch(app, args?)` | Launch an application |

**Coordinate system:** 1280x1024 pixels, origin (0,0) at top-left.

**Safety:** Terminal emulators are blocked (use `exec` tool). Dangerous key combos (Ctrl+Alt+Delete, TTY switches) are blocked. `file://` and `javascript:` URLs are blocked in browser.

**Monitoring:** VNC on port 5900 (localhost only, via `vnc.robothor.ai` with Cloudflare Access).

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

## Vault (Credential Store)

PostgreSQL-backed encrypted credential store. AES-256-GCM encryption with a 32-byte master key at `.vault-key`.

### Tools

| Tool | Purpose |
|------|---------|
| `vault_get(key)` | Retrieve and decrypt a secret by key |
| `vault_set(key, value, category?)` | Encrypt and store a secret (upsert) |
| `vault_list(category?)` | List all keys (no values exposed) |
| `vault_delete(key)` | Remove a secret |

### Key Naming Convention

Use descriptive, hierarchical keys:
- `google/robothor@ironsail.ai` — Google account password
- `aws/access-key` — AWS access key
- `db/staging/password` — database credential
- `api/twilio/auth-token` — API token

### When to Use Vault vs SOPS

| Use Case | System |
|----------|--------|
| API keys/tokens for systemd services | **SOPS** — decrypted at boot to tmpfs, available as env vars |
| Credentials agents need at runtime | **Vault** — agents call `vault_get` during execution |
| Passwords Philip gives you to save | **Vault** — use `vault_set` immediately |
| Infrastructure secrets (PG password, etc.) | **SOPS** — needed before any service starts |

---

## What Goes Here

Environment-specific notes: camera names, SSH hosts, preferred TTS voices, device nicknames. Keep it minimal — agents read this file every run.
