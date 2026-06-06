# Prompt-Injection Chokepoints

Genus OS agents ingest untrusted content — emails, web pages, CRM notes, recalled
memory, accreted skills, and tool outputs. Any of these can carry a prompt
injection that jailbreaks an unattended agent, exfiltrates data, or schedules
more work. This doc tracks the **chokepoints** where assembled/ingested content
is scanned, and the rollout state of each.

The scanner is `robothor/engine/cron_safety.py`
(`scan_assembled_cron_prompt` — pattern check, returns a finding or `None`).
Each chokepoint applies it under the `injection_scan_mode()` ladder
(`ROBOTHOR_INJECTION_SCAN_ENABLED` + `ROBOTHOR_INJECTION_SCAN_MODE`):
`off` → no scan; `observe`/`alert` → log + allow; `enforce` → block.

## Shipped

### 1. Assembled system-run prompt (PR-12)
- **Where:** `runner.py`, right after warmup-preamble assembly, for
  `CRON` / `HOOK` / `WORKFLOW` triggers.
- **Covers:** the system prompt + warmup preamble (recalled memory blocks,
  context files, peer context, breadcrumbs, skills folded into the prompt).
- **Action:** `observe` writes an `agent_guardrail_events` row
  (`guardrail_name="injection_scan"`, `action="observed"`); `enforce` raises
  `CronPromptInjectionBlockedError` (the run aborts; the watchdog reaps the
  partially-started run). Helper: `cron_safety.screen_cron_prompt()`.

## Planned (Hermes three-chokepoint model — design only)

Hermes scans at three additional points. These are the next chokepoints to wire,
each reusing `screen_cron_prompt()` / `scan_assembled_cron_prompt()`:

### 2. Tool OUTPUT
- **Where:** `tools/dispatch.py` post-execution, alongside the existing
  `no_sensitive_data` post-check, for tools that fetch external content
  (`web_fetch`, `gws_gmail_*`, CRM reads, MCP/adapter tools).
- **Why:** an email body or web page returned to the model is the most common
  injection vector. Scan the result before it re-enters the context.
- **Note:** must be allow-list-aware (don't block legitimate content that merely
  mentions "ignore previous instructions"); consider scanning only the portion
  that will be echoed back, and prefer `observe` long-term here.

### 3. Recalled MEMORY
- **Where:** `robothor/memory` recall path (`search_facts` / warmup block load),
  before a recalled fact/block is injected into a prompt.
- **Why:** a poisoned memory fact persists across runs — a single successful
  write can re-compromise every future run.
- **Pairs with:** the memory drift-detector (Rip 7) which already gates writes;
  this gates reads.

### 4. Stored SKILLS
- **Where:** skill-catalog assembly (`skills.py` / `build_skill_catalog`) and the
  curator's accretion gate (W2-24), before an agent-authored skill is loaded.
- **Why:** autonomous skill accretion (W2-22..24) means an agent can write a
  skill that later instructs itself; scan accreted skills before they're trusted.

## Enforcement-abort cleanup (follow-up)

The PR-12 enforce path raises during run setup, so the run row is left `running`
until the watchdog reaps it. A cleaner abort (mark the run `blocked` immediately)
is a small follow-up once a shared "abort-with-status" helper exists in the runner.
