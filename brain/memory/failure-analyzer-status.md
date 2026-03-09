# Failure Analyzer Status

Last run: 2026-03-09 18:00 UTC (2026-03-09 2:00 PM EDT)
Failures analyzed: 17 (in 16:00–18:00 UTC window + carry-forward from 12:00 EDT cron tick)
Tasks created: 0 (no new tasks — all failures map to existing open tasks)
Classifications: transient: 0, config: 5 (tilde path, updated d8a53d2b), infrastructure: 12 (zombie wave + model failures, updated 60d7b991 + 8162ebd2), code: 0, unknown: 0

## Run Summary

### Scope
- Window: 2026-03-09 16:00 UTC – 18:00 UTC (12:00 PM – 2:00 PM EDT)
- Failed runs in window: 0 new "models failed" runs
- Timeout runs in window: 6 calendar-monitor + 1 email-classifier (d5ea55dd) + 7-agent zombie wave (12:00 EDT cron tick)

### New Failures Analyzed

#### 1. calendar-monitor — 4 new timeouts (89c292ce, cf4445f7, 571e76d3, 4d63c1a8)
- **Classification:** CONFIG (tilde path bug)
- **Root cause:** Same `~/robothor/brain/memory/triage-inbox.json` path bug (task d8a53d2b)
- **Run 89c292ce step trail:** 4 consecutive tilde path failures at steps 3/5/7/9 → wasted 243s of 480s budget → timeout
- **Mapped to:** Task d8a53d2b (updated with new evidence)
- **calendar-monitor 24h stats:** 43 runs, 31 completed, 12 timeouts = **27.9% timeout rate** (up from 16.7% 2h ago)

#### 2. email-classifier — 1 new timeout (d5ea55dd)
- **Classification:** CONFIG (tilde path bug)
- **Run trail:** Step 5 = tilde path error → extra LLM round-trip (126s) → timeout at step 8 / 779s total
- **Mapped to:** Task d8a53d2b (updated)

#### 3. 12:00 EDT mass zombie wave (7 agents, simultaneous)
- **Agents:** main (09a9e887), email-responder (b75b0e3f), email-classifier (ec201e9d), vision-monitor (70612d02), chat-responder (c416b005), calendar-monitor (8f437a37), conversation-inbox (8372cf07)
- **Classification:** CODE / INFRASTRUCTURE (zombie pattern)
- **All share:** 0 steps, 0 tokens, null model, null completed_at — identical zombie signature
- **New evidence:** First time ALL 7 cron agents at a single tick zombied simultaneously. Strongly points to shared resource pool exhaustion at cron tick boundary.
- **Mapped to:** Task 60d7b991 (updated with mass event evidence + cron stagger proposal)

#### 4. email-analyst — 1 timeout (7e0cabe3, 03:55 EDT)
- **Classification:** INFRASTRUCTURE (zombie — 0 steps, hook trigger)
- **Mapped to:** Existing tasks c51a8374 / 740a4a86 (no update needed, pattern already documented)

#### 5. Model failures (8162ebd2)
- **No new "models_attempted: []" failures in this window** — current 2h window is clean
- Updated task 8162ebd2 with clean status confirmation

### Open Nightwatch Tasks (13 total)
| ID | Title | Priority | Age |
|----|-------|----------|-----|
| d8a53d2b | calendar-monitor: tilde path bug causing 27.9% timeout rate | HIGH | 4 days |
| 60d7b991 | main: zombie runs — 7-agent mass event at 12:00 EDT | HIGH | 3 days |
| 8162ebd2 | email-classifier: "All models failed" fleet-wide | HIGH | 1 day |
| c51a8374 | email-analyst: zombie pattern cron + hook | HIGH | 3 days |
| 5891440c | email-responder: zombie runs on hook trigger | HIGH | 3 days |
| 43e41fed | calendar-monitor: leading-space tool name bug | HIGH | 3 days |
| 740a4a86 | email-analyst: hook zombie (parent of c51a8374) | HIGH | 3 days |
| 1f78c6e4 | main:heartbeat 0 runs for 2 days | HIGH | 3 days |
| d66659bc | chat-responder: LLM hang after list_my_tasks | NORMAL | 2 days |
| bd52e6c9 | improvement-analyst: missing memory blocks | NORMAL | 4 days |
| ce4671ce | email-analyst: exec call hangs | NORMAL | 4 days |
| 0f2cddb3 | Review Nightwatch PRs 2026-03-09 (6 PRs pending) | HIGH | 11h |
| bde627f5 | Review Nightwatch PRs 2026-03-07 (3 PRs pending) | HIGH | 2 days |

### Critical Observations
1. **Tilde path bug (d8a53d2b) is now 4 days old with 27.9% calendar-monitor timeout rate** — this is the single highest-ROI fix. overnight-pr should prioritize this above all other tasks.
2. **Mass zombie wave at 12:00 EDT** — 7 agents simultaneously zombied at a single cron tick. This is the most severe zombie event recorded. Strongly suggests shared resource pool (DB connections / process slots) is exhausted at high-concurrency cron ticks. Cron staggering + zombie cleanup are the key fixes.
3. **6 Nightwatch PRs pending review** — zero merges means the overnight-pr loop cannot unlock code-level fixes. Philip review unblocks the entire backlog.
4. **No new failure modes discovered this window.**
