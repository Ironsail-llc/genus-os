# Step efficiency — runbook

`ROBOTHOR_STEP_EFFICIENCY_MODE` (`off` | `observe` | `enforce`, default `observe`)

**A run has a fixed wall-clock budget. Is it spending steps on anything?**

## The measurement

2026-09-15 WildClawBench sweep, the harness's own budgets. Five of twelve Code
tasks and one Productivity task hit their task budget (1200s / 900s) and scored
**zero**. Every Code task that *finished* scored **0.9–1.0**. The gap is not
capability — it is that a killed run has written nothing, and the graders award
per criterion, so a partial file earns partial credit and an empty results
directory earns none.

Two timed-out runs profiled from the bench pod's `agent_run_steps`:

| | `sam3_debug` | `connect_the_dots_medium` |
|---|---|---|
| LLM calls | 79, 584s (p50 3s, one 108s) | 66, 885s (p50 7.7s, one 93s) |
| Tool calls | 86, 435s — all `exec`, 41 calls, max 68s | 65 `exec`, 174s |
| Repeats | same `exec` **9×**; one file re-read **6×**; four more 2–4× | none exact |
| Context | p50 41k tokens | — |
| Killed | 1200s, last activity `tool:read_file`, nothing written | mid-LLM-call, nothing written |

Context is **not** the problem — compaction works. `connect_the_dots_medium` is
provider-throughput bound (675 output tokens/call at 25–85 tok/s) and out of
scope for this control. `sam3_debug` is what it is for.

## What the four controls do

| Control | `off` | `observe` | `enforce` |
|---|---|---|---|
| Deadline notes | one note at 80% of the ceiling | same, plus a log line for the 50% and 95% notes it withholds | notes at 50%, 80% and 95%, each with pace |
| Repeat-call guard | nothing | runs every call, logs and counts what it would have done | answers an unchanged repeated read, notes a 3rd identical `exec`, refuses a 5th |
| Tool timeout clamp | requested timeout stands | stands, and the clamp it would have applied is logged | `min(requested, remaining − 30s)`, floor 5s |
| Progress check-in | only at `max_iterations` | same, plus a log line every 25 iterations | also every 25 iterations, asking what has been WRITTEN |

### Deadline notes

Three rungs, never more, never at iteration 0. Each carries elapsed, remaining,
the run's measured seconds per iteration, and how many steps that pace leaves.
The 80% rung keeps its shipped wording verbatim (write FIRST, improve after) and
both the 80% and 95% rungs name the declared deliverables that are not on disk
yet. A rung spends every rung below it, so a run can never be told it is halfway
through a budget it has nearly exhausted.

### Repeat-call guard

Allow-list only. `read_file` and `list_directory` name one stat-able target, so
an identical call whose target has the same mtime and size as at the previous
read is answered from what the run already has:

```json
{"unchanged_since_step": 14, "note": "identical to your read at step 14; the content is unchanged on disk since then"}
```

**The full content comes back instead** whenever the earlier result is no longer
in `session.messages` — compaction or thinning removed it — because a pointer to
something the model can no longer see is worse than the repeat it prevented.
Results over 200k characters are not tracked at all, so they can never be
withheld.

`exec` and `search_files` have no single target to stat, so they are counted by
whether their output changed. The third identical-output occurrence gets a note
and **still runs** — a command may have side effects the engine cannot see. Only
`exec` can be refused, and only on the fifth occurrence after four byte-identical
outputs. A different output resets the count, and any change to the arguments is
a different key that runs immediately, so a refusal is never a trap.

`write_file`, `web_fetch`, `web_search`, `view_image` and everything else are
untouched.

### Tool timeout clamp

Every one of the 41 `exec` calls in the profiled failure asked for `timeout: 900`
against a 1200s budget. The tool's own `MAX_EXEC_TIMEOUT` knows nothing about the
run asking. The effective timeout is now `min(requested, remaining − 30s)`, never
below 5s, and the result says when it was clamped:

```json
{"stdout": "...", "exit_code": 0, "timeout_note": "timeout clamped to 212s: the run has 242s left"}
```

### Progress check-in

The soft check-in fires at `max_iterations`, which the bench agent sets to 80.
The run this exists for took 79 iterations, so it never fired once. Under
`enforce` it also fires every 25 iterations and asks the checkable question —
what has been written, and where — rather than "are you making progress", which
an agent always answers yes to.

## Reading the logs

**Every line is WARNING, including observe-mode lines, and that is
load-bearing.** The benchmark container installs no logging configuration, so
Python's `logging.lastResort` handler applies and its level is WARNING. The
pre-existing deadline note announced itself with `logger.info` and was therefore
invisible in `agent.log` — a whole day of profiling concluded it had never fired
when in fact nobody could see it. An observe rung logged at INFO would repeat
that exact mistake.

Grep `agent.log` (or the journal) for:

| Line | Means |
|---|---|
| `Deadline warning issued at 50% of budget, iteration N, run R` | a pace note was injected |
| `Deliverable check-in issued at iteration N, run R` | the 25-iteration check-in fired |
| `Tool timeout clamped to Ns on run R` | a tool asked for more than the run had |
| `repeat guard answered a repeated read_file on run R` | a read was served from context |
| `repeat guard refused a repeated exec on run R` | a fifth identical command was refused |
| `step-efficiency observe: run R would ...` | `observe` — what `enforce` would have done |

Counting what happened afterwards:

```sql
SELECT action, tool_name, count(*)
FROM agent_guardrail_events
WHERE guardrail_name = 'repeat_guard'
GROUP BY 1, 2;
```

`observed` is the observe-mode shadow decision, `warned` a note or a served
read, `blocked` a refusal. This is the flag's evidence source, so
`genus flags` / the Controls page read the same rows.

## Where it lives

| Piece | File |
|---|---|
| Pace notes, check-in cadence, timeout clamp | `robothor/engine/run_pacing.py` |
| Repeat-call guard | `robothor/engine/repeat_guard.py` |
| Guard decision point | `robothor/engine/tools/dispatch.py` (`_execute_tool`) |
| Note text for the 80% rung | `robothor/engine/deliverables.py`, `robothor/engine/run_budget.py` |
| Call sites | `robothor/engine/runner.py` (`_run_loop`), `robothor/engine/tools/handlers/filesystem.py` (`_exec`) |
| Flag reader | `robothor/engine/feature_flags.py: step_efficiency_mode` |
| Evidence source | `robothor/flags/evidence.py` |
| Intent, soak and gates | `infra/flags.yaml` |

## Promoting it

`enforce` changes what the model sees mid-run and can refuse a tool call, so it
is a behaviour change rather than bookkeeping. Before flipping the fleet:

1. Run a sweep in `observe` and count the `observed` rows above. Zero rows means
   the guard is aimed at nothing on this workload — do not promote on a quiet
   table.
2. Re-measure WildClawBench 02_Code and 01_Productivity **in the sandbox** with
   `ROBOTHOR_STEP_EFFICIENCY_MODE=enforce` and check that the budget-exhausted
   tasks fall.
3. Confirm no run regressed from `scored` to `error` — a refusal the agent could
   not recover from would show there.
