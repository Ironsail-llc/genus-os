# Failure Analyzer Status

Last run: 2026-03-17 22:25 UTC (2026-03-17 6:25 PM EDT)
Failures analyzed: 1 (in 2h window: 1 timeout; 0 hard fails)
Tasks created: 0
Classifications: transient: 1, config: 0, code: 0, unknown: 0

## Run Summary

### Scope
- Window: 2026-03-17 20:25–22:25 UTC (4:25 PM–6:25 PM EDT)
- Trigger: failure-analyzer cron at 22:25 UTC (Tue 2026-03-17)
- Prior run: 2026-03-17 20:25 UTC

### 2-Hour Window Results

**1 timeout in-window. Classified: Transient/Config. No new tasks created.**

#### `1062189e` — calendar-monitor (21:00 UTC / 5:00 PM EDT)
- **Trigger:** hook `calendar:calendar.new`
- **Model:** z-ai/glm-5-20260211
- **Duration:** 480s (full timeout)
- **Tokens:** 23,381 in / 933 out
- **Step audit:**
  ```
  Step 1: llm_call       — 15,468ms  ✓
  Step 2: list_my_tasks  — 6ms       ✓
  Step 3: read_file      — 0ms       ✓
  Step 4: llm_call       — 29,849ms  ✓
  Step 5: update_task    — 9ms       ✓
  Step 6: list_tasks     — 1ms       ✓
  Step 7: error          — "Timed out after 480s"  ← third LLM call never returned
  ```
- **Classification:** TRANSIENT/CONFIG — z-ai/glm-5-20260211 second/third-call hang.
  Pattern: two successful LLM calls + tool calls complete normally, then the next LLM
  call blocks indefinitely. Identical signature to all prior calendar-monitor z-ai hangs.
- **Tracked under:** Task `08d64e1a` (z-ai fleet-wide hang), PRs #40/#41 pending merge.
- **Action:** None — no new task created. Fully covered by existing open task + PRs.

### All Failures Pre-Window
All other `failed` and `timeout` runs returned by list_agent_runs are either:
- Pre-window (started before 20:25 UTC) — skip
- >48h old (Mar 13 and earlier hard-fails) — stale, skip per rules

### 24h Fleet Stats (as of 22:25 UTC Mar 17)

| Agent             | 24h runs | completed | timeouts | timeout rate | driver                      |
|-------------------|----------|-----------|----------|--------------|------------------------------|
| main              | 20       | 19        | 1        | **5%** ⚠️   | z-ai hang (08d64e1a)        |
| calendar-monitor  | 40       | 37        | 3        | **7.5%** ⚠️ | z-ai hang (08d64e1a)        |
| improvement-analyst | 2      | 1         | 1        | **50%** 🚨  | context overflow (84ae40f2) |
| evening-winddown  | 1        | 0         | 1        | **100%** 🚨 | exec_allowlist (PR #42)     |

### Assessment
Fleet stable this window. All failure drivers remain tracked with open Nightwatch tasks
and pending PRs (#17–#42). PR merge remains the single bottleneck — 24+ PRs unmerged,
0 merged. No new tasks created this run.

**Persistent open failure drivers:**
- `08d64e1a` — z-ai/glm-5 second-call hang (main, calendar-monitor) — PRs #40, #41
- `84ae40f2` — improvement-analyst context overflow / 300s budget — open task
- PR #42     — evening-winddown exec_allowlist + write_file fix — pending merge
- `c93e6474` — main list_tasks_summary SQL schema bug (sla_deadline → sla_deadline_at)
