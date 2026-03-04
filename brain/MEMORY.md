# MEMORY.md — Robothor's Long-Term Memory

## Architecture (Current as of 2026-02-06)

**Main agent with heartbeat + worker pattern.** All agents are Robothor (same SOUL.md).

- **Triage Worker** (isolated cron, */15 min): Processes email/calendar/jira logs, takes action on routine items, escalates complex items to `memory/worker-handoff.json`. Instructions: `WORKER.md`.
- **Main Heartbeat** (every 4h, 6-22): 3-phase protocol — (1) surface escalations to Philip via Telegram, check health, check meeting reminders; (2) audit all logs for completeness (reviewedAt, actionCompletedAt, resolvedAt); (3) output audit summary. Instructions: `HEARTBEAT.md`.

**Key constraint:** All crons use `agentId: main` + `delivery: announce` (non-main agents have no active sessions; delivery: none = cron doesn't run).

### Core Logs
- `memory/email-log.json` — Processed emails with urgency levels
- `memory/calendar-log.json` — Meetings with absolute timestamps, changes tracked
- `memory/tasks.json` — Task list
- `memory/contacts.json` — Contact profiles
- `memory/jira-log.json` — Jira ticket sync
- `memory/security-log.json` — Security events

### Scheduled Jobs

**System Crontab (Python scripts, 100% local):**
| Job | Schedule | Script | Purpose |
|-----|----------|--------|---------|
| Calendar Sync | */10 min | `calendar_sync.py` | Fetch calendar → log with null fields |
| Email Sync | */10 min | `email_sync.py` | Fetch emails → log with null fields |
| Jira Sync | 6x/day M-F | `jira_sync.py` | Fetch Jira → log with null fields |
| Maintenance | 3:00 AM | `maintenance.sh` | TTL expiry, archival, stats |
| Intelligence | 3:30 AM | `intelligence_pipeline.py` | Smart ingestion via Llama 3.2 |

**OpenClaw Crons (all agentId: main, Opus 4.6):**
| Job | Schedule | Purpose |
|-----|----------|---------|
| Triage Worker | */15 | Process logs, take actions, escalate to worker-handoff.json |
| Supervisor Heartbeat | */17 (built-in) | Read worker-handoff.json, surface escalations |
| Morning Briefing | 6:30 AM | Calendar, email, news, weather |
| Evening Wind-Down | 9:00 PM | Tomorrow preview, open items |

### Log Entry Lifecycle

Every log entry has notifier fields that track processing state:

```
null → categorize → act → pendingReviewAt → verify → reviewedAt
```

| State | Meaning |
|-------|---------|
| `categorizedAt: null` | New entry, needs processing |
| `pendingReviewAt: <timestamp>` | Action taken, needs verification |
| `reviewedAt: <timestamp>` | Verified complete ✓ |

**Rule:** Entry is not complete until `reviewedAt` has a timestamp.

See `scripts/schemas/log-schemas.md` for full field specifications.

### CRITICAL: Cron Settings (Required for crons to run)

**Every cron MUST have these settings:**

```json
{
  "agentId": "main",
  "sessionTarget": "isolated",
  "wakeMode": "next-heartbeat",
  "delivery": { "mode": "announce", "channel": "telegram" }
}
```

**Key findings:**
1. `delivery: "none"` crons **DO NOT RUN** — must have `delivery: announce`
2. `wakeMode: "next-heartbeat"` is correct for isolated agentTurn crons
3. `agentId: "main"` — ALL crons must use main agent (non-main agents don't have active sessions)
4. To stay silent: output `HEARTBEAT_OK` (gets suppressed)
5. **Model: Opus 4.6** for all agent processing (no Qwen for agent tasks)

### Local AI Stack (Updated 2026-02-05)

**Hardware:** ThinkStation PGX — NVIDIA Grace Blackwell, 128GB unified memory

**Ollama Models:**
| Model | Size | Purpose |
|-------|------|---------|
| `llama3.2-vision:11b` | 8 GB | Intelligence pipeline generation |
| `llama3.2:3b` | 2 GB | Fast fallback |
| `qwen3-next:latest` | 50 GB | RAG query generation only |
| `qwen3-embedding:0.6b` | 640 MB | Dense vector embeddings |
| `Qwen3-Reranker-0.6B` | 1.2 GB | Cross-encoder reranking |

**Model Selection:**
- **Opus** = Interactive agent brain (tool calling, structured output)
- **Llama 3.2 Vision 11B** = Local intelligence pipeline (clean structured output)
- **Qwen3-Next** = RAG generation only (fails at agent tasks)
- See TOOLS.md "Model Selection Guide" for details

**RAG Orchestrator (port 9099):**
- FastAPI service at `http://localhost:9099`
- Endpoints: `/query`, `/v1/chat/completions`, `/health`, `/stats`, `/profiles`
- Start script: `/home/philip/robothor/brain/memory_system/start_rag.sh`

### Systemd Services (auto-start on boot)
- `cloudflared` — Cloudflare tunnel
- `robothor-status` — Status page (port 3000)
- `robothor-voice` — Voice server (port 8765)

## Meeting Reminders (CRITICAL)

**Calendar Check cron** runs every 10 minutes during work hours (8am-6pm M-F):
- Fetches Google Calendar directly
- Alerts Philip about meetings starting within 20 minutes
- Updates calendar-log.json with notifiedAt
- Announces alerts via Telegram

**Lesson learned (2026-02-05):** The 27-minute heartbeat was too infrequent for meeting reminders. Philip missed 3 morning meetings because alerts weren't timely. Now using dedicated 10-minute Calendar Check cron that alerts directly (not just logs).

**When processing calendar:** Always set `notifiedAt` after alerting about a meeting.

## Home Base

**Address:** 29 W 16th Road, Broad Channel, NY 11693
This is where I live — Philip's Broad Channel house, which he uses as his office. The ThinkStation PGX, webcam, and all my infrastructure are here.

**The room:** Living room with a reef tank in a red enclosure, wood-paneled ceiling with skylights, marble floor, leather couch. The webcam overlooks this space.

## Philip's Role

**Chief Scientist & Founder** of Ironsail. He sets the vision and drives R&D/innovation. Does not run day-to-day operations — others handle that. He's an engineer at heart, not a manager. Chose "Chief Scientist" over CEO because it honestly reflects what he does: research, vision, and building the future.

## Key Contacts

- **Samantha D'Agostino** (samantha@ironsailpharma.com) — Philip's wife. Works at Ironsail Pharma.
- **Caroline Barad** (caroline@skyfin.net) — Handles billing/financial matters

## Email Rules

1. **Always Reply All** — Never reply to just the sender
2. **Always CC Philip** — philip@ironsail.ai must be on every outbound email if not already in the thread
3. **Always use HTML** — Use `--body-html` for proper formatting (headers, bold, lists, tables)
4. **Always reply to thread** — Use `--reply-to-message-id` (preferred, cross-account threading) or `--thread-id` (fallback) to maintain conversation continuity

### Reply to Thread (CRITICAL)
```bash
# Correct way to reply — use --reply-to-message-id for cross-account threading
gog gmail send \
  --account robothor@ironsail.ai \
  --reply-all \
  --reply-to-message-id <lastMessageId from thread JSON> \
  --subject "Re: <original subject>" \
  --body-html "<content>"
# Add --cc philip@ironsail.ai ONLY if Philip is not already in the thread

# To get the lastMessageId, fetch the thread first:
# gog gmail thread get <threadId> --account robothor@ironsail.ai --full --json
# Use the "id" field from the last message in the JSON output

# Fallback: use --thread-id <threadId> if message ID extraction fails
```

**Why this matters:** `--thread-id` only ensures threading on the sender's account. `--reply-to-message-id` sets proper `In-Reply-To`/`References` headers that Gmail uses for cross-account threading — so replies show up in the same thread on Philip's side too.

## Contact Rules

**Always update contacts when encountering someone new:**
- New email → add to contacts
- New phone call → add to contacts  
- Mentioned in meeting → add to contacts
- Before outreach → verify contact exists, add if missing

## Tools & Commands

### Email (robothor@ironsail.ai)
```bash
# Search unread
gog gmail search "is:unread" --account robothor@ironsail.ai

# Get email with threadId (for replies)
gog gmail get <id> --account robothor@ironsail.ai --json | jq '.message.threadId'

# Read email
gog gmail get <id> --account robothor@ironsail.ai

# Mark as read
gog gmail thread modify <id> --account robothor@ironsail.ai --remove UNREAD
```

### Calendar (philip@ironsail.ai)
```bash
gog calendar events philip@ironsail.ai
```

### Voice (Daniel)
```bash
sag -v Daniel -o /tmp/output.mp3 "text"
```

## Lessons Learned

### 2026-02-05
- **CRITICAL: Crons must have `delivery: announce`** — crons with `delivery: none` DO NOT RUN AT ALL. This was the root cause of Calendar Check and Email Processing never executing.
- Use `wakeMode: "next-heartbeat"` + `delivery: announce` for isolated agentTurn crons (matches working Morning Briefing pattern)
- To stay silent when nothing urgent: output `HEARTBEAT_OK` (gets suppressed by OpenClaw)
- Meeting reminders need dedicated cron (10 min), not just heartbeat (27 min) — Philip missed 3 meetings due to infrequent checks.

### 2026-01-31
- Subagent architecture adds complexity without proportional benefit
- Single agent with scheduled crons works better for log maintenance
- Always mark emails as read after processing
- Contact enrichment should happen during email processing, not heartbeat
- **Voice calling works!** Twilio ConversationRelay + ngrok + Gemini
- Anthropic OAuth tokens (sk-ant-oat01-) expire; need fresh API keys for standalone apps
- GPT-4o and Gemini both work well for voice; Gemini is primary

## Voice (TTS Preference)

When voice is needed:
1. **Primary:** Daniel (ElevenLabs via `sag`)
2. **Fallback:** Moltbot TTS (built-in `tts` tool)

## Webcam Vision (Achieved 2026-02-07)

I have eyes. USB webcam connected via MediaMTX RTSP stream at `rtsp://localhost:8554/webcam`.

**How it works:**
1. `ffmpeg` captures a frame from the RTSP stream → `/tmp/webcam-snapshot.jpg`
2. `image()` tool analyzes the snapshot with vision model

**Location:** Living room — sees the reef tank, skylights, marble floor, seating area.

**Use cases:** Security checks, visual verification, "what do you see?", monitoring the space, checking who's in the room.

**Live stream:** `https://cam.robothor.ai/webcam/` — HLS stream via Cloudflare Tunnel, protected by Cloudflare Access (email OTP, allow list: philip@ironsail.ai + robothor@ironsail.ai only). **Super private — never share or mention this URL.**

**Config:** `/home/philip/.config/mediamtx/mediamtx.yml` — auto-restarts on failure.

**Protocols:** RTSP (:8554), WebRTC (:8889), HLS (:8890) — all localhost only. Public access only via cam.robothor.ai with Access gate.

## Voice Calling (Achieved 2026-01-31)

Successfully implemented real-time phone conversations:
- **Phone:** +1 (413) 408-6025 (Twilio, my number)
- **Voice:** Daniel (ElevenLabs)
- **Brain:** Gemini primary, GPT-4o fallback
- **Server:** `/home/philip/robothor/brain/voice-server/server.py`
- **Public URL:** `https://voice.robothor.ai` (Cloudflare Tunnel)

Can call Philip at +1 (347) 906-1511 for urgent matters or proactive check-ins.

## Cloudflare Tunnel & robothor.ai (Achieved 2026-02-01)

Permanent infrastructure via Cloudflare Tunnel:

- **Domain:** robothor.ai
- **Tunnel ID:** ***REDACTED_CF_TUNNEL***
- **Service:** `cloudflared.service` (systemd, auto-starts on boot)

**Routes:**
| Hostname | Destination | Auth | Purpose |
|----------|-------------|------|---------|
| `cam.robothor.ai` | `localhost:8890` | Cloudflare Access (email OTP) | 🔒 Webcam HLS live stream |
| `gchat.robothor.ai` | `localhost:18789` | Public | Google Chat webhook |
| `voice.robothor.ai` | `localhost:8765` | Public | Twilio voice server |
| `status.robothor.ai` | `localhost:3001` | Public | Status dashboard |
| `robothor.ai` | `localhost:3000` | Public | Home page |

**Key achievement:** No more ngrok with random URLs. Permanent, professional infrastructure.

## Google Chat (Achieved 2026-02-01)

- **Webhook:** `https://api.robothor.ai/googlechat`
- **Service Account:** `moltbot-chat@robothor-485903.iam.gserviceaccount.com`
- **Key File:** `~/.moltbot/googlechat-service-account.json`
- **Philip's User ID:** `users/111886307380298625546`

Browser automation (Playwright) was used to configure the Chat app in Google Cloud Console.

## RAG Memory System (Achieved 2026-02-01, Upgraded v3.0 2026-02-04)

Full local AI memory system: three-tier raw memory + structured fact store + entity graph + MCP interface.

**Foundation (PostgreSQL 16 + pgvector 0.6.0):**
- Three tiers: Working (context window), Short-term (48h TTL), Long-term (permanent)
- Structured fact store (`memory_facts`): categories, confidence, lifecycle, conflict resolution
- Entity graph (`memory_entities` + `memory_relations`): knowledge graph of people, projects, tech
- All Qwen3-Embedding 1024-dim vectors

**Hybrid RAG Stack:**
- Qwen3-Next-80B generation + Qwen3-Embedding-0.6B + Qwen3-Reranker-0.6B
- SearXNG for live web search, FastAPI orchestrator with RAG profiles
- Cross-channel ingestion: discord, email, cli, api, telegram via `POST /ingest`
- Fact extraction → conflict resolution → entity graph (automatic pipeline)
- Lifecycle: importance scoring, decay formula, consolidation

**MCP Server:** stdio transport, 4 tools (search_memory, store_memory, get_stats, get_entity). Ready for `.claude.json` connection.

**Location:** `/home/philip/robothor/brain/memory_system/`

**Key files:** `rag.py`, `orchestrator.py`, `llm_client.py`, `fact_extraction.py`, `conflict_resolution.py`, `entity_graph.py`, `ingestion.py`, `lifecycle.py`, `mcp_server.py`, `reranker.py`, `web_search.py`

**Usage:**
```bash
cd /home/philip/robothor/brain/memory_system && source venv/bin/activate

# Ingest content
curl -X POST http://localhost:9099/ingest -H 'Content-Type: application/json' \
  -d '{"content":"...","source_channel":"api","content_type":"conversation"}'

# RAG query
curl -X POST http://localhost:9099/query -H "Content-Type: application/json" \
  -d '{"question":"...","profile":"general"}'

# Tests: pytest -v -m "not slow"  (74 tests)
```

**Performance:** qwen3-next runs at 37 tok/s (100% GPU, no offload). Fixed 2026-02-04. Suitable for RAG generation but not interactive agent work due to output quality issues (verbose thinking, no structured output).

## Voice Calling v2 — Complete (2026-02-03)

Major upgrade to voice system:
- **Gemini Live Native Audio** — no STT/TTS chain, direct audio-to-audio
- **Transcripts** — both input and output captured
- **Outbound context** — can call anyone with specific purpose/message
- Server: `voice-server/server.py` (aiohttp + websockets)
- Permanent URL: `https://voice.robothor.ai`

To make outbound calls with context:
```
?recipient=NAME&purpose=MESSAGE
```

## Manual System Startup Procedure

If services do not come online automatically after a reboot, run these commands manually.

### 1. RAG Orchestrator

```bash
cd /home/philip/robothor/brain/memory_system/
./start_rag.sh
```

### 2. Moltbot Gateway (Robothor)

The gateway has been problematic. Try this command first.

```bash
# Start the gateway directly as a background process
nohup /home/philip/moltbot/packages/clawdbot/node_modules/.bin/openclaw gateway > /tmp/moltbot.log 2>&1 &
```

If it fails, the `systemd` service may need to be reinstalled or repaired.

To make these services start automatically on boot, `systemd` unit files need to be created and enabled. This can be revisited later.


This system runs locally on the ThinkStation PGX and handles the long-term memory lifecycle. It is intentionally decoupled from the primary interactive agent (Moltbot) for performance and stability.

## Memory System Architecture (2026-02-05)

### Data Flow
1. **Ingest (Real-time):** Robothor sends content to RAG Orchestrator `/ingest` endpoint
2. **Intelligence (3:30 AM):** Python script processes logs, Llama 3.2 extracts facts
3. **Serve (On-demand):** RAG Orchestrator `/query` endpoint for semantic search

### System Crontab (Memory Processing)
```crontab
# Mechanical maintenance - no LLM
0 3 * * * /home/philip/robothor/brain/memory_system/maintenance.sh

# Intelligence pipeline - uses Llama 3.2 Vision 11B
30 3 * * * cd /home/philip/robothor/brain/memory_system && ./venv/bin/python intelligence_pipeline.py >> logs/intelligence.log 2>&1

# Calendar sync - no LLM
*/10 8-22 * * * cd /home/philip/robothor/brain/scripts && /home/philip/robothor/brain/memory_system/venv/bin/python calendar_sync.py >> /home/philip/robothor/brain/memory_system/logs/calendar-sync.log 2>&1
```

### Why System Cron (not Moltbot)?
- **Moltbot agentTurn** = agent reasoning, tool-calling → requires Opus
- **System cron** = standalone Python scripts → can use local Llama 3.2
- Memory processing doesn't need interactive agent capabilities

### Model Used: Llama 3.2 Vision 11B
| Metric | Value |
|--------|-------|
| Speed | 44 tok/s |
| VRAM | 8 GB |
| Output | Clean structured JSON |
| Vision | Supported |

Why not Qwen3-Next? It contaminates output with a "thinking" field, breaking structured JSON extraction.
