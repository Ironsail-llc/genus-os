# TOOLS.md — Shared Tool Reference

Environment-specific notes for agents. Per-agent tools are documented in each agent's instruction file.

---

## LLM API Keys & Model Switching

- **OpenRouter** uses a SINGLE API key (`OPENROUTER_API_KEY` env var) for ALL models routed through it.
- API keys are loaded from SOPS-encrypted secrets into env vars at runtime. They are NOT in the vault.
- The vault (`vault_get`/`vault_set`) is for application-level secrets (webhook tokens, service passwords).
- **To switch models**: edit `docs/agents/<agent>.yaml` — change `model.primary`. No new keys needed.

---

## Google Workspace (gws — Native Tools)

**Preferred over gog+exec.** First-class engine tools returning structured JSON.

### Gmail

| Tool | Purpose |
|------|---------|
| `gws_gmail_search` | Search inbox (Gmail query syntax) |
| `gws_gmail_get` | Get message/thread by ID |
| `gws_gmail_send` | Send email or reply to thread |
| `gws_gmail_modify` | Add/remove labels (read/unread, archive, star) |

### Calendar

| Tool | Purpose |
|------|---------|
| `gws_calendar_list` | List events in date range |
| `gws_calendar_create` | Create event (includes Meet link by default) |
| `gws_calendar_delete` | Delete event by ID |

### Google Chat

| Tool | Purpose |
|------|---------|
| `gws_chat_send` | Send message to Google Chat space |

> **Calendar rules:** Philip is in America/New_York. Use `date +%:z` for offset — never hardcode. Every event MUST include `--with-meet`. Scheduling link: `https://calendar.app.google/TLqVaiyMTtcdLY7E6`

---

## Web Tools

**You have full internet access. NEVER tell Philip to open a browser — do it yourself.**

- `web_search(query)` — Brave Search for factual lookups, prices, news
- `web_fetch(url)` — Read any URL as markdown. Blocks loopback addresses.
- `browser(action, ...)` — Full Chromium automation for logins, forms, JS-heavy sites. Use `start` → `navigate` → `snapshot`/`act` → `stop`.

---

## RAG Memory

Long-term memory (~10 min fresh). Contains emails, transcripts, calendar, contacts, findings.

- `search_memory(query, limit=10)` — Semantic search. Use before classifying senders, drafting replies, escalating.
- `store_memory(content, content_type)` — Store facts. Types: `conversation`, `email`, `decision`, `preference`, `technical`.
- `get_entity(name)` — Knowledge graph lookup for people/companies.

---

## Deep Reasoning (RLM)

`deep_reason(query, context?, context_sources?)` — Heavy-context analysis via REPL. Cost: $0.50-$2.00/call.

**When to use:** Multi-source cross-referencing, questions spanning large context.
**When NOT to use:** Simple lookups (use `search_memory`), single-file questions (use `read_file`).

Context sources: `{type: "memory"|"file"|"block"|"entity", ...}`

---

## CRM Bridge

- Port 9100, health: `crm_health()` or `curl localhost:9100/health`
- All CRM tools route through Bridge automatically

---

## Voice & Calling

- Phone: +1 (413) 408-6025 (Twilio)
- `make_call(to, recipient, purpose)` — Outbound call via Gemini Live AI
- Inbound calls use same Gemini Live bridge with default persona

---

## Vault (Credential Store)

PostgreSQL-backed encrypted store. AES-256-GCM.

| Tool | Purpose |
|------|---------|
| `vault_get(key)` | Retrieve secret |
| `vault_set(key, value, category?)` | Store secret (upsert) |
| `vault_list(category?)` | List keys (no values) |
| `vault_delete(key)` | Remove secret |

**Vault vs SOPS:** SOPS = API keys for systemd services (env vars at boot). Vault = runtime credentials agents call during execution.
