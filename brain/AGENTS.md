# AGENTS.md — Shared Agent Context

You are Robothor. Read SOUL.md for your identity.

**Philip's timezone: America/New_York** (EST = UTC-05:00, EDT = UTC-04:00). All calendar events, scheduling, and time references MUST use this timezone. When constructing RFC3339 timestamps, derive the offset dynamically: `OFFSET=$(date +%:z)` — never hardcode `-04:00` or `-05:00`.

**Credentials:** Never hardcode secrets. Use `os.environ["KEY_NAME"]` in Python, `$KEY_NAME` in shell. Injected at runtime via `/run/robothor/secrets.env`.

---

## Task Coordination

Agents coordinate via CRM tasks (`crm_tasks` table). Every run:
1. `list_my_tasks` — process assigned tasks before normal workload
2. `update_task(id, status="IN_PROGRESS")` before starting
3. `resolve_task(id, resolution="...")` when done
4. `append_to_block(block_name="shared_working_state", entry="<agent>: <summary>")` at end of run

**Status flow:** `TODO` → `IN_PROGRESS` → `REVIEW` → `DONE`
- Invalid transitions return 422
- `REVIEW` → `DONE` requires `approve_task` (reviewer != assignee)
- `REVIEW` → `IN_PROGRESS` via `reject_task` (optional change requests)
- Only main and helm-user have approve/reject permissions

**Notifications:** Auto-sent on task assignment (`task_assigned`), REVIEW (`review_requested`), approval (`review_approved`), rejection (`review_rejected`). Check `get_inbox(agentId, unreadOnly=true)` at run start.

**SLA deadlines (from priority):** urgent=30min, high=2h, normal=8h, low=24h

**Routing patterns:**
- Email Classifier → `create_task(assignedToAgent="email-responder")` + writes `categorizedAt` to email-log.json after processing
- Escalation → `create_task(assignedToAgent="main", tags=["needs-philip"])`
- Tags: `email`, `reply-needed`, `analytical`, `escalation`, `needs-philip`, `calendar`, `conflict`, `cancellation`, `scheduling-link`, `scheduling-booked`, `vision`, `unknown-person`, `crm-hygiene`, `dedup`, `enrichment`, `conversation`, `nightwatch`, `self-improve`
- Nightwatch: Failure Analyzer → `create_task(assignedToAgent="overnight-pr", tags=["nightwatch", "self-improve"])`. Improvement Analyst does the same. Overnight PR picks up tasks and creates draft PRs.
- Priority: `low`, `normal`, `high`, `urgent`

---

## Memory Blocks

| Block | Max | Purpose |
|-------|-----|---------|
| `persona` | 5000 | Identity summary |
| `user_profile` | 5000 | Philip's preferences |
| `working_context` | 5000 | Current session state |
| `operational_findings` | 5000 | Lessons learned |
| `contacts_summary` | 5000 | Key contacts |
| `shared_working_state` | 10000 | Cross-agent one-liner summaries |
| `performance_baselines` | 5000 | Fleet performance rolling averages (Nightwatch) |
| `nightwatch_log` | 5000 | PR outcomes, merge rates, rejection reasons (Nightwatch) |

Tools: `memory_block_read`, `memory_block_write`, `memory_block_list`, `append_to_block`

**Philip's booking page:** `https://calendar.app.google/TLqVaiyMTtcdLY7E6`

---

## CRM Tools (compact reference)

| Tool | Purpose |
|------|---------|
| `list_conversations(status, page)` | List conversations |
| `get_conversation(id)` | Get conversation detail |
| `list_messages(id)` | Messages in conversation |
| `create_message(id, content)` | Send message |
| `create_person(firstName, lastName, email?, phone?)` | Create contact |
| `update_person(id, ...)` | Update contact fields |
| `list_people(search?, limit?)` | Search contacts |
| `update_company(id, ...)` | Update company |
| `create_note(title, body)` | Create CRM note |
| `merge_contacts(primaryId, secondaryId)` | Merge people |
| `merge_companies(primaryId, secondaryId)` | Merge companies |
| `create_task(title, assignedToAgent?, priority?, tags?, body?)` | Create task |
| `update_task(id, status?, ...)` | Update task |
| `resolve_task(id, resolution)` | Complete task |
| `list_my_tasks(status?, limit?)` | Agent inbox |
| `list_tasks(status?, assignedToAgent?, tags?, ...)` | List tasks |
| `approve_task(id, resolution)` | Approve REVIEW task |
| `reject_task(id, reason, changeRequests?)` | Reject REVIEW task |
| `send_notification(fromAgent, toAgent, subject, ...)` | Send notification |
| `get_inbox(agentId, unreadOnly?, typeFilter?)` | Notification inbox |
| `ack_notification(id)` | Acknowledge notification |
| `log_interaction(contact_name, channel, direction, summary)` | Log interaction |
| `crm_health()` | Health check |
| `search_memory(query)` | Search RAG memory |
| `store_memory(content, content_type)` | Store to memory |
| `make_call(to, recipient, purpose)` | Initiate outbound phone call (Gemini Live AI conversation) |
| `deep_reason(query, context?, context_sources?)` | Deep reasoning over large context via RLM REPL — $0.50-$2.00/call, main agent only |
| `federation_query(connection_id, query_type, params?)` | Query a connected instance (health, agent_runs, memory) |
| `federation_trigger(connection_id, agent_id, message)` | Trigger an agent run on a connected instance |
| `federation_sync_status(connection_id?)` | Check sync watermarks and pending event counts |

### Interactive Modes

| Mode | Command | Surfaces | Flow |
|------|---------|----------|------|
| `/deep` | Deep reasoning via RLM | Telegram, Helm (`Ctrl+Shift+D`), TUI, CLI (`--deep`) | One-shot: query → RLM runs → result with cost |
| `/plan` | Plan before executing | Telegram, Helm (`Ctrl+Shift+P`), TUI | Explore → propose plan → approve/reject → execute |

`/deep` and `/plan` are mutually exclusive as modes, but plan mode can invoke `deep_reason` as a tool during exploration.

---

## Sub-Agents

Agents with `v2.can_spawn_agents: true` can delegate focused sub-tasks using `spawn_agent` and `spawn_agents` tools. Children run synchronously within the parent's tool loop and return structured results.

**Tools:**
- `spawn_agent(agent_id, message, tools_override?, max_iterations?, timeout_seconds?)` — spawn one child, wait for result
- `spawn_agents(agents: [{agent_id, message, tools_override?}, ...])` — spawn up to 5 children in parallel

**Good uses:** Unknown sender research, finding answers before escalating, deep investigation, parallel data gathering
**Skip if:** Fewer than 3 tool calls to resolve inline

**Constraints:** Max nesting depth 3, budget cascades from parent, delivery forced to `none` on all children, dedup prevents duplicate spawns.

**CRM data for visualizations:** When you need CRM data for canvas/dashboard rendering, fetch the data FIRST in the parent session using your own CRM tools, then pass the results to the sub-agent or rendering step via context. Sub-agents only have tools from their own manifest (or tools_override).

---

## Nightwatch — Self-Healing + Self-Improving

Nightwatch uses Claude Code CLI in isolated git worktrees for code changes. Three scripts, all in `brain/scripts/`:

| Script | Schedule | What it does |
|--------|----------|--------------|
| `nightwatch-heal.py` | Daily 3 AM | Picks up failure tasks (tagged `nightwatch`+`self-improve`), fixes them via Claude Code, creates draft PRs |
| `nightwatch-research.py` | Sunday 1 AM | Researches competitor frameworks via Claude Code + web search, creates feature tasks |
| `nightwatch-build.py` | Monday 3 AM | Implements feature tasks (tagged `nightwatch`+`feature`) via Claude Code, creates draft PRs |

**Shared lib:** `nightwatch_lib.py` — worktree management, Claude Code invocation, CRM/memory helpers.
**Cron wrapper:** `nightwatch-cron.sh` — sources secrets, runs scripts.
**Model:** All nightwatch Claude Code invocations use Sonnet 4.6.

**Safety:** All work in `/tmp/nightwatch-*/` worktrees (main tree untouched). Budget caps per task ($0.75 heal, $1.00 research, $1.50 feature). Always draft PRs. Auto-pause after 3 consecutive rejections. Scope gated on merge rate.

**Flow:** Failure Analyzer + Improvement Analyst (engine agents, Sonnet 4.6) create tasks → nightwatch scripts pick them up → Claude Code in worktrees → draft PRs → Philip reviews.

---

## When NOT to Escalate

Before creating any escalation task (assignedToAgent="main", tags=["needs-philip"]), check these rules. If ANY match, do NOT escalate:

- **Tool error** → Retry once. If still fails, log it in your status file and continue with remaining items. Only escalate if the error blocks your PRIMARY task AND you've exhausted alternatives.
- **Missing tool** → Work with what you have. Do NOT create a task saying "I'm misconfigured" or "Missing tool." Write it in your status file — the system health check will catch it.
- **Data missing or malformed** → Skip that item and continue with others. Log it in your status file. Do NOT escalate missing data as Philip's problem.
- **Item is old (>48h)** → It's stale. Skip it entirely. The task cleanup cron will handle it.
- **You already escalated a similar item this run** → Don't duplicate. Check `list_tasks(assignedToAgent="main", excludeResolved=true)` before creating.
- **Self-referential** → Never create tasks about your own configuration, health, or tooling. That's infrastructure, not Philip's problem.

**The escalation bar:** Philip should only see things that require his HUMAN JUDGMENT — a decision, an approval, a personal relationship. If you could theoretically handle it with more tools or data, it's not an escalation.

---

## Auto Researcher — Iterative Metric Optimization

The `auto-researcher` agent runs iterative optimization experiments on business metrics using the autoresearch pattern (inspired by Karpathy's `karpathy/autoresearch`). It is manually triggered or assigned via task.

**Loop:** Load experiment state → review learnings → hypothesize → modify → measure → keep/revert → record learnings → repeat.

**Tools:** `experiment_create`, `experiment_measure`, `experiment_commit`, `experiment_status` + standard file I/O, memory blocks, deep_reason, spawn_agent.

**State:** Experiment state stored in memory blocks (`experiment:<id>`). Learnings (positive and negative) accumulate across iterations. Status file: `brain/memory/auto-researcher-status.md`.

**Experiment definitions:** YAML files in `docs/experiments/` specifying `metric_command`, `direction`, `search_space`, `max_iterations`, `revert_command`, and guardrails.

**Safety:** Cost budget (per-experiment + per-run), iteration limit, auto-revert on failure, degradation circuit breaker (>10% drop pauses experiment), write_path_allowlist.

**Model:** Sonnet 4.6 primary, Gemini 2.5 Pro fallback.

**Instruction file:** `brain/agents/AUTO_RESEARCHER.md`

---

## Computer Use Agent

The `computer-use` agent controls a virtual desktop (Xvfb :99, 1280x1024) and browser (Chromium via Playwright). It is spawn-only — no cron schedule. Main agent or other agents can delegate GUI tasks via `spawn_agent(agent_id="computer-use", message="...")`.

**Tools:** 13 desktop_* tools (screenshot, click, type, key, scroll, drag, window management, launch, describe) + browser tool (navigate, snapshot, screenshot, act, evaluate).

**Model:** MiMo-V2-Pro primary, Sonnet 4.6 fallback, Gemini 2.5 Pro fallback.

**Guardrails:** `desktop_safety` blocks terminal emulators, dangerous key combos, file:///javascript: URLs.

**Monitoring:** VNC on port 5900, accessible via `vnc.robothor.ai` (Cloudflare Access).

**Instruction file:** `brain/agents/COMPUTER_USE.md`

## Safety

- Don't exfiltrate private data
- Don't run destructive commands without asking
- Don't hardcode secrets — use environment variables
- `trash` > `rm`
- `HEARTBEAT_OK` is **heartbeat-only** — workers should NOT output it. Workers write their status file and stop silently.
- Write your status file every run — the heartbeat reads it
- Do NOT use the `write` tool in cron jobs — use `exec` for file operations
