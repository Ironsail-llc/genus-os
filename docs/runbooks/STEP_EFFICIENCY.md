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

## What the controls do

| Control | `off` | `observe` | `enforce` |
|---|---|---|---|
| Deadline notes | **exactly main**: one note at 80%, main's text with no pace sentence, main's `logger.info` line, no iteration-0 guard | the same note, at WARNING with the run id, plus a log line for the 50% and 95% notes it withholds | notes at 50%, 80% and 95%, each with pace |
| **The budget ends the run** | **nothing, including the resolver**: `ROBOTHOR_RUN_BUDGET_SECONDS` is ignored and every clock uses the agent's own tempo-scaled `timeout_seconds`, exactly as before; the wallclock self-check ends the run as a timeout | resolves the imposed budget and writes a `run_budget` / `observed` row for each rung it would have entered, with the run id; acts on nothing | wrap-up at 90%, hard stop at 100% — see below |
| Repeat-call guard | **no guard object is built at all** — no thread hops, no state | runs every call, logs and counts what it would have done | answers an unchanged repeated `read_file`, notes a 3rd identical `exec`, refuses a 5th that spoke |
| **Varying-argument loops** | not tracked | tracked and logged | same ladder as an exact repeat, keyed on the command head / path / domain / query stem |
| Tool timeout clamp | requested timeout stands | stands, and the clamp it would have applied is logged with the run id | `min(requested, remaining − 30s)`, floor 5s |
| Progress check-in | only at `max_iterations`, main's wording | the same, plus a log line every 25 iterations | also every 25 iterations, asking what has been WRITTEN — and directive after two empty answers past halfway |

## The budget ends the run

`ROBOTHOR_RUN_BUDGET_SECONDS` · `ROBOTHOR_RUN_WRAPUP_FRACTION` (0.90) ·
`ROBOTHOR_RUN_BUDGET_GRACE_SECONDS` (20)

**The measurement.** 2026-09-17 Code Intelligence sweep,
`connect_the_dots_hard`: the task declared a 1200s budget and the harness
backstop was 1500s, but the engine resolved its own ceiling to **1600** — the
manifest number multiplied by the model's tempo factor. Every clock the engine
owned was aiming four hundred seconds past the point at which the container
would be destroyed. `wd.log` shows `wait=llm_inflight` at every tick from
`elapsed=1200` to `elapsed=1500`; `agent.log` holds one line, `HARNESS KILL
after 1500s`; `transcript.jsonl` was never written and the grader scored
**0.0** on a `FileNotFoundError`. The same task scored 0.545 the day before.
Four more runs in the same sweep died the same way.

**One resolver.** `run_deadline.resolve_run_budget` is the only derivation of a
run's wall-clock budget, and `watchdog_budgets_for` reads it too, so the loop
and the watchdog cannot disagree:

1. `ROBOTHOR_RUN_BUDGET_SECONDS`, when set **and the rung is not `off`**,
   taken **exactly** — an imposed budget is never tempo-scaled, because
   whoever imposed it is counting the same seconds. The bench harness exports
   it per task;
2. otherwise the agent's `timeout_seconds`, tempo-scaled exactly as before;
3. otherwise the fleet ceiling.

**The rung gates the resolver too, and that is load-bearing.** The harness
exports `ROBOTHOR_RUN_BUDGET_SECONDS` on every run whatever the rung says, so
if the resolver ignored the rung the `off` arm of a differential sweep would
already carry the largest part of this change — a run ending at the imposed
1200s instead of being destroyed at 1500s — and the measured `enforce − off`
delta would be small and mis-attributed to the wrap-up rung. The watchdog and
the loop still share the one derivation, so they cannot disagree about the
number; what the rung decides is *which* number they share. A value the
settings registry rejects logs a WARNING naming the variable and falls back to
the agent's own timeout rather than degrading silently.

**The ladder.** 50% / 80% / 95% notes as before, then:

- **90% — WRAP-UP.** The tool schema narrows to `write_file`, `read_file`,
  `list_directory`, `todo_write`, and **admission refuses anything else** —
  withdrawing a tool from the schema is a request, and a model that asked for
  `exec` anyway was still being served until this was enforced at the gate. A
  refused call gets a tool result naming the seconds left and the tools still
  available (the full sentence once per tool, a one-liner after), is not
  counted as an iteration error and never escalates. The agent is told how
  many seconds remain (a number, not a percentage) and asked to write its best
  current answer to the path the task named and read it back against the
  task's shape. Said once per run.
- **100% — STOP.** No further LLM call is issued. A call already in flight is
  bounded by `remaining + grace` and then cancelled. The run records
  `budget_exhausted`, writes an honest closing summary naming what is and is
  not on disk, lands a `run_budget` / `blocked` row in
  `agent_guardrail_events`, and returns into the ordinary finalizer — which
  runs the deliverable verdicts as it does for any other ending.

The clock is read at the top of every iteration **and** again after each model
call returns, before any tool runs: `asyncio.timeout` has nothing to raise if a
callee swallows the `CancelledError` and returns normally, which this engine
has measured before.

**A run that finishes inside its budget is untouched** — no note, no narrowing,
no flag.

**The backstop must never fire.** A `harness_kills` row in the ledger is now an
engine defect rather than a slow task, and the ledger records
`harness_kill_reasons` beside the count.

## Varying-argument loops

The repeat guard keys on a canonical hash of the arguments, so it catches a
call repeated exactly. The runs that burn a whole budget do not repeat exactly.
Recorded shapes: `sam3_debug` re-ran `python test_sam3.py` after each
incremental `pip install` and read one module thirteen times at eight
`offset`/`limit` windows (two of them differing from an earlier window only in
that the numbers arrived as strings, which the canonical key reads as different
calls); it asked one question as twenty `search_files` patterns; `link_a_pix`
rewrote the same script as `rescan.py`, `rescan2.py`, `rescan3.py`, …

`robothor/engine/repeat_variants.py` groups calls into **families** by a coarse
signature — for `exec`, what the command runs and on WHAT (a leading
`cd … &&` peeled, first pipeline segment only, flags and shell punctuation
dropped, **every** remaining word kept); the path for `read_file` and
`list_directory`; domain plus path for `web_fetch`; and a stopworded token set
for `web_search` and `search_files` matched at Jaccard ≥ 0.6.

Keeping every word rather than the first two is a correction, not a
refinement: `-m` is a flag, so `python -m pytest <file>` collapsed into one
family and the fifth *different* test file was refused, and `python solve.py
--case N` did the same across six genuinely different cases. "Run the solver
over N cases" is the shape of WildClaw Code tasks 2/7/8/12. The target is what
the work is about, not an argument a loop varies to slip the guard. The trade
is that a heredoc keys its family on its whole body, so those are rarely
caught — the safe direction for a control that can withhold a call.

**Read a quiet `web_search` row count as "aimed at nothing", never as "the
loop shape is gone."** Jaccard ≥ 0.6 over a two- or three-token query means a
single synonym breaks the family — `{computed, score}` against
`{calculated, score}` is 0.33 — so five realistic rewordings of one question
produce four families and nothing is said. Lowering the threshold would merge
unrelated searches, which costs a capability, so the reworded shape is caught
for near-identical rewordings only. The `exec` and path families do the work;
the query families are a bonus. `test_repeat_guard_variants.py` pins this as a
limit rather than leaving it to be rediscovered from a flat table.

A coarse signature is only safe because the **trigger is the absence of new
information**, not the call count: a family climbs the ladder only while its
results keep returning what the run already has (identical digest, or token
overlap ≥ 0.85). Thirty different files read once each are thirty new results
and escalate nothing. The ladder is the exact guard's: note at the third such
call, refuse at the fifth, `exec` only, never a silent command, and a refusal
carries no `error` key. A refusal spends the family's streak, so the next call
runs — the exact guard's "any change to the arguments is a way out" is
preserved in spirit even though this control closes the letter of it.

## The directive check-in

The check-in asks what has been WRITTEN. Measured across the 09-16 and 09-17
sweeps, the runs that died at their budget answered it with a plan and kept
exploring. Past `DIRECTIVE_FRACTION` (0.5) of the budget, the second check-in
that finds **none** of the task's declared outputs on disk stops being a
question: it names the paths and says the next action is `write_file` to them,
before any further reading, searching, fetching or running.

The trigger is the workspace, never the model's prose — an agent that says it
has written the file and has not is the exact failure being caught. A task that
declared no output path never escalates, because there is no evidence either
way.

### `off` is the engine that shipped

Three things that all read as improvements belong to the ladder, not to the
baseline: an operator turning this off has to get the previous engine back, and
a sweep comparing `off` with `enforce` is only measuring the controls if `off`
is what it claims to be. So under `off`: the pace sentence is not appended; the
deadline line stays `logger.info("Deadline warning issued at iteration N")`,
emitted from main's own `robothor.engine.runner` logger so a journal filter on
that name does not watch the line vanish from an `off` box (raising it to
WARNING is item 0's fix and lands at `observe`, the default every existing
install gets); and a note at iteration 0 is not suppressed.
`robothor/engine/tests/test_step_efficiency_off_is_main.py` runs
main's own `deadline_note` beside the pacer across a grid of inputs and pins the
rest as snapshots taken from `origin/main`.

The budget stop and the varying-argument families are on the same terms: under
`off` the resolver ignores `ROBOTHOR_RUN_BUDGET_SECONDS` entirely, so the
ceiling is the agent's own tempo-scaled `timeout_seconds` and a run past it is
ended by the loop's wallclock self-check, as a TIMEOUT, at the same second as
before — no wrap-up, no narrowed tool schema, no admission refusal, no
`budget_exhausted` from this path, no guardrail row — and no family is tracked
at all. `robothor/engine/tests/test_budget_ends_the_run.py` drives the loop at
`off` with the variable set and pins it.

### Deadline notes

Three rungs, never more, never at iteration 0. Each carries elapsed, remaining,
the run's measured seconds per iteration, and how many steps that pace leaves.
The 80% rung keeps its shipped wording verbatim (write FIRST, improve after) and
both the 80% and 95% rungs name the declared deliverables that are not on disk
yet. A rung spends every rung below it, so a run can never be told it is halfway
through a budget it has nearly exhausted.

### Repeat-call guard

Allow-list only. `read_file` names one stat-able target whose bytes are the
whole answer, so an identical call whose target has the same mtime and size as
at the previous read is answered from what the run already has:

```json
{"unchanged_since_step": 14, "note": "identical to your read at step 14; the content is unchanged on disk since then", "repeat_guard": "answered"}
```

**The full content comes back instead** whenever the earlier result is no longer
in `session.messages` — compaction or thinning removed it — because a pointer to
something the model can no longer see is worse than the repeat it prevented.
Results over 200k characters are not tracked at all, so they can never be
withheld.

**A resend is itself a read, and the guard records what it put back.** It did
not, and that is what run `8cd032aa` measured: 23 read decisions in one
`enforce` run and every one of them carried the whole file, `sam3_image.py`
(37 KB) five times. `after` never runs for a call the guard answered — dispatch
returns the decision and the handler is skipped — so `_still_in_context` went on
looking for the payload stored at the original read, a string that no message
holds once the resent dict (three extra keys) has replaced it. The first resend
after a compaction is the design; every later one was a repeat answered with the
bytes the tool would have returned, which costs exactly what the repeat would
have and reads from outside as a control that never fired. The in-context check
still runs on every decision, so this bookkeeping cannot cause a pointer at
content that is gone.

Because of that, **a `warned` row is not by itself a saving**. The row's
`reason` now says which branch it took: a reason ending *"and is repeated here
because it is no longer in your context"* is a resend that cost a full copy of
the file; one that does not is a short answer that cost ~160 characters.

`list_directory` is **not** short-circuited, and the reason is worth stating: the
handler reports every entry's `size` (and with `recursive: true` the whole
subtree) while a fingerprint can only stat the directory — whose mtime does not
move when a file inside it grows, and does not move at all when a file appears
two levels down. The guard would have said "unchanged on disk" about an
out-of-date listing, breaking exactly the "did my deliverable land, and how big
is it" loop the rest of this control exists to encourage. A listing can only be
trusted by taking it again, so it is output-counted like a search and costs the
call it would have saved.

`exec`, `search_files` and `list_directory` are therefore counted by whether
their **output** changed — the tool's own fields only (`stdout`, `stderr`,
`exit_code`, `error`, …), never an engine annotation such as the clamp's
`timeout_note`, whose remaining-seconds number shrinks with the clock and would
otherwise reset the counter on every call.

**No engine-computed number may reach the digest by any route.** The clamp's
note was the first to try; the second was the timeout message itself, which used
to read *"Command timed out (5s limit)"* with the **clamped** seconds inside it —
and `error` is in the projection, so the wall clock walked straight back in and
six identical timeouts under a moving clock produced no note and no refusal. Both
`exec` branches now return `{"error": "Command timed out…", "timeout_seconds": N}`
with the number in its own field, which the allow-list does not name. The agent
still reads it; the digest does not. A test asserts that a synthetic result
carrying an extra engine field that differs on every call digests equal, so the
rule holds for the next one rather than for today's two.

An identical **error** counts too: five identical clamped timeouts is ~1060s of a
1200s budget, the most expensive repeat a run can make.

The third identical-output occurrence gets a note and **still runs** — a command
may have side effects the engine cannot see. Only `exec` can be refused, only on
the fifth occurrence after four byte-identical outputs, and **only if the command
said something**. A silent command (`mkdir -p`, `cp`, `rm -f`, `chmod`,
`git add`, anything redirected to `/dev/null`) returns
`{"stdout": "", "stderr": "", "exit_code": 0}` forever whatever it did, so four
identical silences establish nothing about the fifth; those are noted and never
refused. A different output resets the count, and any change to the arguments is
a different key that runs immediately, so a refusal is never a trap.

A refusal returns
`{"refused": true, "reason": "…", "identical_runs": 4, "repeat_guard": "refused"}`
with **no** `error` key. `runner.py` reads `result.get("error")` straight into
the per-tool circuit breaker, which appends *"Tool 'exec' has failed 3 times this
run. Do NOT call it again"* at three — so a refusal shaped like an error would
have told a Code-task agent to abandon its only way to run code. The
`repeat_guard` marker (also `"answered"` on a served read) is what keeps the same
result out of the *opposite* bucket: the runner skips `escalation.record_success`
and `checkpoint.record_success` for it, because a tool that never ran is not
progress either.

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
| `Tool timeout clamped to Ns on run R` | a tool asked for more than the run had left |
| `Deadline warning issued at iteration N` (INFO, no run id, logger `robothor.engine.runner`) | the run is at `off` — main's line, level and logger name |
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

A count of `warned` rows does **not** say the guard saved anything — that is
exactly how 23 full-file resends read as 23 answers. Split them:

```sql
SELECT tool_name,
       count(*) FILTER (WHERE reason LIKE '%no longer in your context') AS resent,
       count(*) FILTER (WHERE reason NOT LIKE '%no longer in your context') AS short
FROM agent_guardrail_events
WHERE guardrail_name = 'repeat_guard' AND action = 'warned' AND tool_name = 'read_file'
GROUP BY 1;
```

`resent` counting everything is the defect above; a healthy long run shows one
resend per file per compaction and short answers after it.

**Observe understates this particular split.** Under `observe` the tool runs
anyway, so its result is back in `session.messages` on every call and the shadow
decision is always the short one — the rung never shows the resend branch that
`enforce` takes. Check the branch on an `enforce` run, or with the doctor probe
below.

## The positive control

```
genus doctor --only step_efficiency.guard
```

Three identical reads of a file in a temp directory, through the real
`dispatch._execute_tool` with a registered session, the conversation compacted
between the first and the second. It passes only when the last repeat comes back
without content: a guard that decides nothing, resends every time, or points at
content that is gone each fail with a different line. Nothing it does reaches
the instance, and it writes no `agent_guardrail_events` rows.

Run it before reading a zero in the table as "the workload has no repeats".

## Where it lives

| Piece | File |
|---|---|
| Pace notes, check-in cadence, timeout clamp | `robothor/engine/run_pacing.py` |
| Budget resolver, wrap-up, hard stop | `robothor/engine/run_deadline.py` |
| Repeat-call guard | `robothor/engine/repeat_guard.py` |
| Varying-argument families | `robothor/engine/repeat_variants.py` |
| Guard decision point | `robothor/engine/tools/dispatch.py` (`_execute_tool`) |
| Positive control | `robothor/doctor/checks/step_efficiency.py` |
| Note text for the 80% rung | `robothor/engine/deliverables.py`, `robothor/engine/run_budget.py` |
| Call sites | `robothor/engine/runner.py` (`_run_loop`), `robothor/engine/tools/handlers/filesystem.py` (`_exec`) |
| Flag reader | `robothor/engine/feature_flags.py: step_efficiency_mode` |
| Evidence source | `robothor/flags/evidence.py` |
| Intent, soak and gates | `infra/flags.yaml` |
| Benchmark container env | `bench/wildclaw/harness.py` (`_container_command`) |

## Measuring it in the sandbox

Nothing needs setting. `bench/wildclaw/harness.py` writes
`ROBOTHOR_STEP_EFFICIENCY_MODE=enforce` into the task container's env file
unless the host exports a different value, so a sandbox re-run of 02_Code and
01_Productivity measures `enforce`; export
`ROBOTHOR_STEP_EFFICIENCY_MODE=off` on the host for the baseline half of a
differential sweep. The harness builds that env from a fixed dict and forwards a
host `ROBOTHOR_*` only when a task's own `env:` block names it — so before this
entry existed, an operator exporting the rung would have measured the
in-container default and read a flat result as "the controls do nothing".

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
