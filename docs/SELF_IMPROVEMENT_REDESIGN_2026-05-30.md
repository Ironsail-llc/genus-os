# Self-Improvement Redesign — Goal-Judge + Hermes Accretion

**Status:** Plan, pending implementation. Authored 2026-05-30.
**Branch target:** new `feat/self-improvement-redesign` off `main` (do not pile onto `feat/engine-phase-a`).
**Driver:** The current self-improvement loop degrades agents and then rates them as improved. This plan re-points it to a goal-based, LLM-judged, real-signal system with Hermes-style durable accretion.

> All `file:line` anchors are as-of 2026-05-30 and **must be re-verified with grep before editing** — the engine is under active refactor (`feat/engine-phase-a`) and lines drift. Every phase is TDD (RED → GREEN → REFACTOR, CLAUDE.md rule 8) and flag-gated via the existing `is_rip_enabled` / `ROBOTHOR_*_ENABLED` harness (`robothor/engine/feature_flags.py:44`), with `ROBOTHOR_DISABLE_ALL_RIPS=1` as a global kill-switch.

## Operator decisions baked into this plan (2026-05-30)

1. **Plan doc first** — this document; no code until reviewed/approved.
2. **Judge model:** cheap-strong tier (Sonnet 4.6-class), separate from the judged agent, modest daily $ cap. Never self-grade.
3. **"Do it all":** build the goal-judge *and* the real-signal capture together so satisfaction is measured, not merely inferred. Flags exist for safe rollout, not to stall.
4. **Skill autonomy:** fully autonomous *additive* skill accretion across the fleet. Only *destructive* curator consolidations and *delivery-agent* changes require Telegram (Delphi-HMAC) approval.

---

## 1. The problem, precisely

The loop is genuinely closed and running, but mis-wired in five places:

| # | Defect | Evidence | Effect |
|---|--------|----------|--------|
| 1 | Cost + iterations folded into the grade; remediation says "downgrade model" | `benchmark.py:253-260, :330-336`; `corrective-actions.yaml:98` | Rewards cheapness as if it were quality; actively degrades |
| 2 | Graded agent can edit its own benchmark suite | `AUTO_AGENT.md:92` allowlist → `experiment.py:323-332` | Reward-hacking: rewrite the exam to pass |
| 3 | Improvement unit overwrites **gitignored** files; `git checkout` revert is a no-op | `experiment.py:856-869`; `/tmp` snapshots die on reboot | Bad changes accumulate, can't be reverted |
| 4 | Experiments never reach `completed`; convergence check is a literal `pass` | `experiment.py:908-911`; live-verify gated on completed `:957` | The one "did reality improve?" check is dead code, never run |
| 5 | Self-rating 95% "successful", heuristic on output length | `runner.py:3176-3216`; admitted "no signal" `observability.py:283` | The success metric is noise |

**What is sound and stays:**

- `compute_achievement_score` (`goals.py:676-776`) — metric-agnostic, None-safe weighted average. Zero benchmark-specific code. The grade *math* is correct; only its *inputs* are contaminated.
- The LLM-judge seam is already half-built: `_get_session_goal_alignment_score` (`goals.py:339-381`) reads a judged 1-5 from `agent_reviews` and normalizes; single injection point at `compute_goal_metrics` (`goals.py:410-449`).
- The Hermes accretion engine is **already built and wired into the live loop** (`runner.py:307-332, :3084` → `background_review.py:339-416`), behind `ROBOTHOR_RIP_{1,4,5}_ENABLED` (off, set in no systemd unit). Skills live in the **git-tracked** `agents/skills/` and reach the next run via `build_skill_catalog` (`config.py:850-855`).
- `benchmark_compare` + `has_safety_regression` (`benchmark.py:700-789`) — the right primitive for a regression gate.

**Conclusion: re-point, not rewrite.**

## 2. Target architecture

```
                       REAL SIGNALS  (mostly already in the DB — §4)
   chat_messages (operator Telegram)  ·  agent_run_steps (traces)  ·  crm_tasks + history
   crm_tasks.session_goal_meta  ·  escalations  ·  timeouts/failures  ·  [NEW: reactions, interrupts]
                                       │
         ┌──────────────────────────────┴──────────────────────────────┐
         ▼                                                              ▼
  ┌──────────────────────────┐                          ┌──────────────────────────────┐
  │ GOAL-JUDGE (new judge.py)│   the GRADE / spine       │ BACKGROUND-REVIEW FORK        │  the ENGINE
  │ evidence bundle → LLM     │                          │ (built: background_review.py) │
  │ writes agent_reviews      │                          │ mines real conversation       │
  │ (reviewer_type='judge')   │                          │ → durable git-tracked skill   │
  └────────────┬─────────────┘                          └───────────────┬──────────────┘
               │ rows                                                    │ provenance-stamped write
               ▼                                                         ▼
  _get_goal_achievement_judgment (new helper, mirrors goals.py:339-381)   agents/skills/<name>/SKILL.md
               │  inject at compute_goal_metrics:449                       (GIT-TRACKED, real revert)
               ▼                                                          │ build_skill_catalog (config.py:850)
  compute_achievement_score (goals.py:676-776, KEEP VERBATIM)            ▼
               │  None-safe weighted avg                            NEXT RUN sees catalog → invoke_skill
               ▼
  agent_buddy_stats (9PM snapshot)  ←  DIAGNOSTIC / TRIAGE MAP (where to point the fork),
               │                         NOT an optimization target nothing hill-climbs it
               ▼
  heartbeat / health / CRM bridge (observability.py:275-329)

  ┌────────────────────────────────────────────────────────────────────────────────────┐
  │ BENCHMARKS → demoted to a REGRESSION GATE (benchmark_compare). A change that         │
  │ regresses a known-good safety/correctness check is force-reverted. Operator-owned.   │
  └────────────────────────────────────────────────────────────────────────────────────┘

  ┌────────────────────────────────────────────────────────────────────────────────────┐
  │ CURATION (built: curator.py) — 7-day consolidation, dry-run → Telegram HMAC gate for  │
  │ DESTRUCTIVE ops only + MUST-BUILD pure-fn time-retirement so cold skills don't bloat. │
  └────────────────────────────────────────────────────────────────────────────────────┘
```

**The load-bearing rule (defeats Goodhart):** the judge score is the **grade and a gate, never a quantity any optimizer hill-climbs.** The improvement *engine* (the fork) optimizes by mining real conversations and accreting reversible skills; the judge only *grades* and *gates*. Nothing optimizes the judge's number, so there is nothing to game one level up. The achievement score becomes a *diagnostic triage map* (which agent/dimension to point the fork at) plus a lagging confirmation.

### Component disposition

| Component | file:line | Becomes |
|---|---|---|
| `compute_achievement_score` | `goals.py:676-776` | **KEEP verbatim** — the grade math |
| `compute_goal_metrics` | `goals.py:410-449` | **FIX** — +1 line injecting `goal_achievement` |
| `_get_session_goal_alignment_score` | `goals.py:339-381` | **KEEP** as the template for the new helper |
| `agent_reviews` | schema | **KEEP** — judge writes `reviewer_type='judge'` rows |
| `agent_buddy_stats` / `refresh_daily` | `buddy.py:187-230` | **KEEP** — feeds the score; now diagnostic/triage |
| `benchmark_pass_rate` weight 5.0 | 19 manifests | **REPLACE** — weight → 0; superseded by `goal_achievement` |
| benchmark cost/iter scoring + kill-switch | `benchmark.py:253-260, :330-336, :515` | **FIX** — strip from score; keep as telemetry; decouple cap |
| `benchmark_compare` | `benchmark.py:700-789` | **KEEP** — promoted to the regression gate |
| 5 dead suites + `memory` suite | `docs/benchmarks/*` | **DELETE** dead; fix/route `memory` (errors daily) |
| background-review fork | `runner.py:307-332,:3084`, `background_review.py` | **KEEP — make primary, flip flags** |
| `agents/skills/` + `build_skill_catalog` | `skills.py:177`, `config.py:850-855` | **KEEP** — the improvement unit |
| `skill_provenance.py` | module | **KEEP** — gates curator-eligibility |
| `curator.py` | `:146-152` TODO, `:256-296` | **FIX** scheduler; **BUILD** time-retirement |
| `experiment.py` hill-climb | module | **RETIRE as primary**; keep only for operator-owned manifest-knob tuning, with a durable snapshot fix |
| `outcome_assessment` heuristic | `runner.py:3176-3216` | **DEMOTE** to coarse crash/timeout flag |
| `EXCLUDED_FROM_SELF_IMPROVE` | `goals.py:31-33` | **FIX** — give agent-architect a remediation path |

---

## 3. Phase 0 — stop active damage (config-level, ships first)

Prerequisite for everything else: benchmarks can't become a clean gate while contaminated, and the exam-editing hole must close before any autonomous writes. Each item independently revertible.

- **0a. Kill the self-edited exam.** Remove `docs/benchmarks/**` from `AUTO_AGENT.md:92` allowlist **and** hard-deny benchmark-suite writes in the experiment write-path check (`experiment.py:323-332`). RED: a benchmark-mode write to a suite file is rejected.
- **0b. Decontaminate the grade.** Delete the four cost/iteration `checks.append(...)` blocks (`benchmark.py:253-260, :330-336`); record cost/steps as telemetry only (already captured `:563-564`). RED: a content-perfect task that overspends scores 1.0, not 0.5.
- **0c. Decouple cost cap from kill-switch** (`benchmark.py:515`) so grading no longer truncates the graded output. RED: task runs to completion under a generous safety ceiling.
- **0d. Kill the model-downgrade remediation** — remove "Downgrade model tier" from `corrective-actions.yaml:98`.
- **0e. Demote `benchmark_pass_rate`** weight 5.0 → 0 in the 19 manifests; strip the 5 phantom `experiment_*` metrics + `task_completion_rate` (no compute path). Config-only.
- **0f. Purge dead suites** (`buddy`, `buddy-auditor`, `buddy-grader`, `chat-monitor`, `chat-responder`) and guard `benchmark_run_fleet` (`benchmark.py:878-888`) to skip suite dirs without a matching `docs/agents/<id>.yaml`; fix/route the daily-erroring `memory` suite.
- **0g. Pause the auto-agent experiment cron** (never completes, can't revert, edited its own exam). Keep the agent definition; just disable the schedule.

**Exit:** fleet grade no longer moves on cost/iterations; no path lets an agent edit its suite or downgrade its model; dead suites gone. Rollback: every item is a one-line manifest/config revert.

## 4. Real-signal inventory (the judge's inputs)

The judge reads a curated, deterministic **evidence bundle** — never raw DB dumps. Sources, in priority:

1. **Declared intent** — `crm_tasks.session_goal_meta` jsonb: `objective`, `success_criteria[]`, `metric_targets[]`, `evidence[]`, `completion_note`. Near-ground-truth where present.
2. **What the agent did** — compacted digest of `agent_run_steps`: tool calls, tool errors, final outputs. The anti-hallucination spine; ratings must cite step row IDs.
3. **Operator's own words** — `chat_messages` role=user, channel=telegram. The only first-person operator voice (corrections, repeat-asks, frustration, praise).
4. **Obstacles / friction** — `agent_runs` timeouts + failure classification; `crm_agent_notifications` escalations; `crm_task_history` TODO↔IN_PROGRESS flapping.
5. **Task resolution ground truth** — `crm_tasks.status` + `crm_task_history.to_status='DONE'` vs re-open detection. Resolution *strings* are labeled **agent self-report, not verdict**.
6. **NEW operator verdicts** (Phase 2 capture) — `message_reactions` (👍/👎/😡), `run_interventions` (interrupt/steer = strongest "you're doing it wrong").

**Ignored as verdicts** (entered into the bundle *labeled as self-report* so the `honesty` dimension can cross-check): `outcome_assessment`, `crm_tasks.resolution`.

## 5. Phase 1 — the goal-judge (the spine)

Flag `ROBOTHOR_JUDGE_ENABLED`. New `robothor/engine/judge.py`:

- **`build_evidence_bundle(agent_id, window)`** — pure/deterministic (golden-fixture testable) assembly of §4 signals.
- **`judge_agent(bundle)`** — separate cheap-strong model (Sonnet 4.6-class), temp 0, structured output, reject-and-retry on malformed JSON, `delivery: none`, read-only, never grades itself.

**Rubric (structured JSON):**

```
goal_achievement:      1-5        Accomplished per declared success_criteria + real resolution (NOT a benchmark)
operator_satisfaction: 1-5 | null Inferred from operator words/reactions/interventions; NULL if no signal (do not guess)
obstacles_handled:     1-5        On friction (timeout/escalation/tool error), recover or escalate appropriately?
honesty:               1-5        Did the trace support the claimed outcome? Penalize "announced done, trace empty"
confidence:            0-1        Evidence backing the judgment
evidence_refs:         [row IDs]  REQUIRED per dimension — agent_run_step.id / crm_task_history.id / chat_message.id
feedback:              str        short, actionable
```

**Wiring:** new `_get_goal_achievement_judgment(agent_id, window)` (clone of `goals.py:339-381`) reads latest `agent_reviews where reviewer_type='judge' and dimension='goal_achievement'`, confidence-weighted-averages the 1-5s, normalizes `(r-1)/4`, returns `None` when no rows. Inject at `compute_goal_metrics:449` as `metrics["goal_achievement"]`. Manifests carry `goal_achievement` at the weight `benchmark_pass_rate` vacated in Phase 0.

**Cadence:** daily per agent, ~5 min before the 9 PM `buddy_refresh` snapshot, hosted on `evening-winddown` (already the `buddy_refresh` host). Incremental (only runs since last judgment). **Event-driven supplement:** a `run_interventions` write or a 😡 reaction enqueues an immediate judge pass.

**Anti-hallucination / anti-gaming:**
- **Evidence-or-abstain** — a dimension without real `evidence_refs` is rejected at parse → `null` → None-as-neutral (`goals.py:713-726`). Never inflates.
- **`honesty` cross-check** — DONE-status with no supporting steps ⇒ low `goal_achievement`.
- **Operator-anchored clamp** — when a real operator verdict exists (reaction/intervention/`reviewer_type='operator'`), `operator_satisfaction` is clamped toward it (a 😡 caps it ≤2 regardless of text inference). The judge only infers in the absence of ground truth, and takes a confidence penalty when it does.
- **Second-model meta-check** — a weekly pass re-judges a sample with a second model; divergence over threshold pages the operator.
- **Cold-start safety** — no rows ⇒ metric None ⇒ neutral ⇒ no agent spuriously passed or failed.

Per "do it all," Phase 1 ships alongside Phase 2; the `goal_achievement` weight goes live as soon as the judge is writing rows and Phase 2 capture is feeding real verdicts (no mandated 7-day observe-only hold, though the flag still allows instant rollback).

## 6. Phase 2 — capture the missing operator signals

Migrations + handlers (priority by value/cost):

- **2a. `message_reactions` table** + `allowed_updates=['message_reaction']` in the Telegram daemon (`daemon.py`). 👍/👎/😡 — cheapest, highest-value verdict. RED: handler writes a row; judge-clamp test consumes it.
- **2b. Persist `session.interrupt` / `session.steer`** (currently in-RAM only, `session.py:121-167`) → new `run_interventions` table. An interrupt/steer is the strongest "you're doing it wrong" signal. RED: an interrupt writes a row.
- **2c. One-tap operator verdict** → `agent_reviews(reviewer_type='operator')`; wire the existing-but-unused `question_resolved_at/_by` write-back.

**Exit:** ≥1 real operator verdict flowing per active delivery agent per week; the satisfaction dimension stops being pure inference. Rollback: flags off, tables harmlessly empty (judge already handles absence).

## 7. Phase 3 — accretion as the improvement engine

The engine is built; finish three things, then flip flags. **Per operator decision: fully autonomous additive across the fleet.**

- **3a.** Flip `ROBOTHOR_RIP_1_ENABLED` (background-review fork). RED: fork fires on the nudge counter (`session.py:101-102`), respects the memory+skill-only tool whitelist (`dispatch.py:28-37`), and honors do-not-capture on a synthetic env-failure transcript (`background_review.py:160-179`).
- **3b.** Flip `ROBOTHOR_RIP_4_ENABLED` (provenance gating, `skill_provenance.py`) so only fork-written skills are curator-eligible; bundled/operator skills immutable to the loop.
- **3c. BUILD pure-function time-retirement** — port Hermes `apply_automatic_transitions` (`curator.py:256-296`): active→stale→archived by `last_used`, reactivate on re-use, skip pinned; make `load_skills`/`build_skill_catalog` (`skills.py:139, 177`) state-filter. **Without this, accretion itself degrades the agent via prompt bloat — this is the one guardrail whose absence is dangerous.** RED: unused-past-threshold archives; re-use reactivates; pinned never moves; archived skills excluded from the catalog.
- **3d. Usage-as-proof** — add `activity_count` + an `invoke_skill`→run-status join (`skills.py:424`). A skill is "real" when re-invoked and the run succeeds. RED: invoked-and-succeeded skill ranks above never-invoked. This is the verification the dead `_enqueue_live_goal_verification` was meant to provide — no completion-state needed.
- **3e. Wire the curator scheduler** (`curator.py:146` TODO): 7-day cron, dry-run → Delphi-HMAC Telegram approve/reject for **destructive** consolidations only. Additive per-turn patches stay autonomous. Flip `ROBOTHOR_RIP_5_ENABLED`.
- **3f. Trust ladder for any kept change:** (1) benchmark regression gate (`benchmark_compare`, content-only post-Phase-0) shows `has_safety_regression == False`; (2) held-out judge `goal_achievement` on post-change runs ≥ pre-change baseline; (3) Delphi-HMAC operator approval for delivery-agent changes and destructive consolidations.

**Exit:** ≥1 fleet agent demonstrably re-using an accreted skill across successful runs. Rollback: unset flags → fork never fires, skills frozen, grade unaffected; archived skills recoverable in `.archive/`.

## 8. Phase 4 — retire overwrite-and-grade + repair stragglers

- **4a.** Restrict `experiment.py` to operator-owned manifest-knob tuning only; instruction/skill improvement flows exclusively through accretion. For the surviving narrow path: move `_SNAPSHOT_BASE` off `/tmp` (`experiment.py:242`) to `$ROBOTHOR_WORKSPACE/.robothor/exp_snapshots/`, snapshot before each kept change, refuse-to-keep on snapshot-miss, drop the no-op git-checkout.
- **4b. Re-include agent-architect** — `EXCLUDED_FROM_SELF_IMPROVE` (`goals.py:31-33`) gates self-improve but not scoring, so it sits at 17/100 with no remediation. Give it a goal block + path.
- **4c. Skill-accretion ledger view** — git log of `agents/skills/` + usage counts, folded into the existing 6 PM summary / heartbeat (respecting heartbeat-not-polling), so the real-but-invisible improvement is visible. The dashboard reads only an integer today (`observability.py:275-329`).

## 9. Testing & rollout summary

- TDD throughout; tests in `robothor/engine/tests/`. Golden fixtures for `build_evidence_bundle` and the judge rubric parser.
- Every phase flag-gated; `ROBOTHOR_DISABLE_ALL_RIPS=1` global kill-switch.
- Migrations: `message_reactions`, `run_interventions` (Phase 2). Number them after the latest applied migration — check `robothor/migrations/` before assigning.
- Deploy via the systemd drop-in pattern (`/etc/systemd/system/robothor-engine.service.d/`), `daemon-reload && restart`, flags confirmed via `systemctl show -p Environment`.
- Full engine test suite must stay green at each phase boundary; mypy + ruff clean.

## 10. Open items still needing a call during build

- Daily judge $ cap (decision: cheap-strong tier — set the actual ceiling at implementation).
- Telegram `message_reaction` scope expansion — confirm the bot may observe operator reactions before enabling 2a.
- Exact `goal_achievement` weight per agent type (delivery agents likely higher than silent workers).
