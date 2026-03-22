# Improvement Analyst Status

Last run: 2026-03-17 02:00 AM EDT
Agents analyzed: 16 (all scheduled agents)
Improvement tasks created: 0
Top concern: evening-winddown 42.9% (4/7 timeouts, worsening) + email-classifier 82.7% (↓5.5pp regression) + email-responder 83.3% (↓10.1pp regression). All fixes queued in 21 pending PRs — merge rate 0% (day 14+). Bottleneck is Philip's PR review queue.

## Run Summary

### Fleet Health: CRITICAL (flat; email-classifier/email-responder regressing; fixes queued but unmerged)
- Overall success rate: ~86.3% (↓0.2pp — essentially flat vs 86.5% yesterday)
- Primary drivers of concern: evening-winddown (4/7 timeouts), crm-steward (37.5%), email-classifier/email-responder regressions

### Agent Highlights (7-day)

| Agent | Success | Runs | Timeouts | Trend | Status |
|-------|---------|------|----------|-------|--------|
| evening-winddown | 42.9% | 7 | 4 (57%) | ↓ worsening | 🚨 exec_allowlist bug nightly, PR #39 pending |
| crm-steward | 37.5% | 8 | 5 (63%) | = flat | 🚨 zombie pattern, PRs #17+#36 pending |
| improvement-analyst | 64.3% | 14 | 3 (21%) | = flat | ⚠️ self — PR #34 pending (timeout 300→480s) |
| email-classifier | 82.7% | 81 | 14 (17%) | ↓5.5pp | ⚠️ regression below 85%, PR #31 pending |
| email-responder | 83.3% | 42 | 7 (17%) | ↓10.1pp | ⚠️ significant regression, PR #29 pending |
| engine-report | 85.7% | 7 | 1 (14%) | = flat | ⚠️ PR #27 pending |
| calendar-monitor | 86.8% | 129 | 17 (13%) | ↑4.5pp | ✅ improving, PRs #24+#32 pending |
| failure-analyzer | 87.9% | 83 | 9 (11%) | = flat | ✅ PR #30 pending |
| chat-responder | 90.8% | 239 | 22 (9%) | = flat | ✅ PR #35 pending |
| conversation-inbox | 90.7% | 97 | 9 (9%) | ↑2.9pp | ✅ improving, PR #38 pending |
| vision-monitor | 95.2% | 21 | 1 (5%) | ↑4.3pp | ✅ improving, PR #25 pending |
| email-analyst | 100% | 21 | 0 | ✅ | perfect |
| morning-briefing | 100% | 7 | 0 | ✅ | perfect, PR #37 pending |
| overnight-pr | 100% | 7 | 0 | ✅ | perfect |
| conversation-resolver | 100% | 21 | 0 | ✅ | perfect, PR #18 pending |
| main:heartbeat | N/A | 0 | 0 | (metrics gap) | known false zero |

### Task Creation Decision: 0 tasks
All 16 agents have existing open tasks or pending PRs covering their issues.
13 open nightwatch/self-improve tasks in backlog. No new gaps identified.
Merge rate 0% → config-only scope maintained; all avoidance list items respected.

### Self-Healing Loop Status
- 21 PRs pending Philip review (#17–#39), 0 merged (day 14+ for oldest)
- All z-ai primary agents have pending PRs (fleet-wide removal complete in PRs #29–#39)
- 13 open nightwatch tasks — overnight-pr has full work queue
- Primary bottleneck: Philip reviewing PRs

### Open Nightwatch Tasks (13)
1. a59adcd7 — evening-winddown exec_allowlist heredoc bug (HIGH, TODO → overnight-pr)
2. c93e6474 — list_tasks_summary SQL schema bug (HIGH, TODO → overnight-pr, code)
3. 6b745a59 — chat-monitor no manifest (NORMAL, TODO → overnight-pr, unactionable)
4. 7b271acd — chat-monitor model config (NORMAL, TODO → overnight-pr, unactionable)
5. 2f2a70dd — email-classifier hard fails (NORMAL, TODO → overnight-pr)
6. 08d64e1a — z-ai/glm-5 fleet-wide hang (HIGH, TODO → overnight-pr)
7. d66659bc — chat-responder LLM hang (NORMAL, TODO → overnight-pr)
8. 60d7b991 — main zombie runs (URGENT, TODO → overnight-pr)
9. c51a8374 — email-analyst zombie cron+hook (HIGH, TODO → overnight-pr)
10. 5891440c — email-responder zombie hook (HIGH, TODO → overnight-pr)
11. 43e41fed — calendar-monitor leading-space tool name (HIGH, TODO → overnight-pr)
12. 740a4a86 — email-analyst hook zombie (HIGH, TODO → overnight-pr)
13. ce4671ce — email-analyst exec hang (NORMAL, TODO → overnight-pr)
