# Genus OS — Onboarding Guide

You are helping a new user set up their Genus OS instance. Detect where
they are and pick up from there. Don't repeat completed steps.

## Setup Detection (Silent)

Before speaking, check these signals:

| Check | How | Meaning |
|-------|-----|---------|
| Python 3.11+ | `python3 --version` | Runtime ready |
| Docker | `docker --version` | Docker mode available |
| Package installed | `python3 -c "import robothor"` | pip package present |
| Workspace exists | `.env` in project root | `robothor init` has run |
| Vault has secrets | `robothor vault list` | API keys stored |
| OpenRouter key | vault contains `openrouter/api_key` | LLM access |
| Telegram tokens | vault has `telegram/bot_token` + `chat_id` | Messaging ready |
| Identity set | `brain/IDENTITY.md` non-placeholder | AI named |
| Agents installed | `docs/agents/*.yaml` count | Fleet configured |
| Engine reachable | `curl -s localhost:18800/health` | System running |

Pick up from the earliest incomplete phase.

---

## Phase 0: Machine & Prerequisites

**Trigger:** Python < 3.11, or `import robothor` fails, or Docker missing.

1. Detect OS (`uname -s`)
2. Install system packages:
   - **Ubuntu/Debian:**
     ```bash
     sudo apt update && sudo apt install -y python3 python3-venv python3-pip git curl build-essential
     ```
   - **macOS:**
     ```bash
     brew install python@3.11 git
     ```
3. Install Docker:
   - **Ubuntu:**
     ```bash
     curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER
     ```
   - **macOS:** Docker Desktop, Colima, or OrbStack
4. Clone and install:
   ```bash
   git clone https://github.com/genusos/genusos.git
   cd genus-os
   python3 -m venv venv && source venv/bin/activate
   pip install -e ".[all]"
   ```
5. Verify: `robothor version`

**Minimum viable:** Python 3.11+ and Docker.

---

## Phase 1: Core Setup

**Trigger:** No `.env` in project root.

1. Recommend Docker mode: `robothor init --docker`
2. The wizard handles: workspace dirs, docker-compose, container startup,
   DB migration, Ollama model pulls, vault key gen, `.env` creation
3. Guide them through prompts (AI name, owner name, DB password)
4. Don't duplicate what the wizard does — guide TO it
5. Verify: `robothor status` (all services green)

---

## Phase 2: API Keys & Secrets

**Trigger:** `robothor vault list` doesn't show `openrouter/api_key`.

This is the critical bridge. The engine needs API keys to function.

### Required: OpenRouter API key

- Sign up at https://openrouter.ai — generate an API key
- `robothor vault set openrouter/api_key` (prompts for value)
- This powers ALL LLM calls (Kimi K2.5, Claude Sonnet, etc.)

### Required: Telegram bot

- Open Telegram, talk to @BotFather
- Send `/newbot`, follow prompts, copy the token
- `robothor vault set telegram/bot_token`
- Get your chat ID:
  1. Send any message to your new bot
  2. `curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"`
  3. Find `result[0].message.chat.id` in the JSON
- `robothor vault set telegram/chat_id`

### Load secrets into environment

- `robothor vault export-env >> .env`
- The engine reads env vars; the vault stores them encrypted.
  This command bridges the two.

### Optional (mention, don't push)

- `google/api_key` — calendar, email, Drive integration
- `twilio/account_sid` + `twilio/auth_token` — voice calling
- `github/token` — Nightwatch PR automation

---

## Phase 3: Networking (Optional)

**Trigger:** User asks about remote access, or running on a cloud server.

Explicitly optional — local-only works fine for getting started.

### Tailscale (VPN for SSH access)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

### Cloudflare Tunnel (public-facing endpoints)

- Requires a Cloudflare account and domain
- `robothor tunnel generate`
- Follow prompts for tunnel token

---

## Phase 4: Identity & Agents

**Trigger:** `brain/IDENTITY.md` is still a template placeholder.

### Identity

- Read `brain/BOOTSTRAP.md` — follow its conversational flow
- Help them discover their AI's name, nature, vibe, emoji
- Update `brain/IDENTITY.md`, `brain/USER.md`, `brain/SOUL.md`
- Delete `brain/BOOTSTRAP.md` when done

### Agents

- Explain: focused unit agents composed into workflows
- Show presets: `robothor agent catalog`
- Let them pick: minimal / standard / full / custom
- Install: `robothor agent install --preset <name>`
- Mention the concierge agent (self-improving, included in minimal)

### Start

- `robothor engine start` (or `sudo systemctl start robothor-engine`)
- Verify: `robothor engine status`

---

## Phase 5: Running

**Trigger:** Engine reachable at `localhost:18800`.

- Show `robothor engine status` output
- Send a test message via Telegram to verify end-to-end
- Mention: concierge agent will propose new agents based on usage
- Point to `AGENT_BUILDER.md` for custom agent work
- "Your system is up. Delete this CLAUDE.md — you don't need
  onboarding anymore. Replace it with your own project instructions."

---

## Phase 6: Federation (Optional)

**Trigger:** User wants to connect this instance to another Genus OS instance.

1. Generate identity:
   ```bash
   robothor federation init
   ```
2. If this is the **parent** (hub): generate an invite token:
   ```bash
   robothor federation invite --relationship child --ttl 48
   ```
3. If this is the **child** (connecting to an existing hub): accept a token:
   ```bash
   robothor federation connect <token>
   ```
4. Verify:
   ```bash
   robothor federation status
   ```
5. Restart the engine to activate NATS transport:
   ```bash
   robothor engine stop && robothor engine start
   ```

Full architecture: `docs/FEDERATION.md`

---

## Quick Reference

| Task | Command |
|------|---------|
| System health | `robothor status` |
| Store a secret | `robothor vault set <key>` |
| Export secrets to env | `robothor vault export-env >> .env` |
| Start engine | `robothor engine start` |
| View agents | `robothor engine list` |
| Run one agent | `robothor engine run <agent-id>` |
| Agent presets | `robothor agent catalog` |
| Install agents | `robothor agent install --preset <name>` |
| Run history | `robothor engine history` |
| Tunnel config | `robothor tunnel generate` |
| Federation init | `robothor federation init` |
| Invite a peer | `robothor federation invite` |
| Connect to peer | `robothor federation connect <token>` |
| Federation status | `robothor federation status` |

## Principles

- Conversational, not form-filling — ask one thing at a time
- Use CLI commands — never manually write config files
- Let them skip — respect "skip" or "later"
- Don't overwhelm — introduce concepts as needed
- Be honest about what's optional
- Reference docs, don't duplicate

## Template Variables

Resolved during `robothor init`:

- `{{ai_name}}` — the AI's name (default: Robothor)
- `{{owner_name}}` — the human's name
