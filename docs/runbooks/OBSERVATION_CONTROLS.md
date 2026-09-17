# Observation controls — runbook

`ROBOTHOR_TRUNCATION_LEDGER_MODE` · `ROBOTHOR_ACT_OBSERVE_MODE` ·
`ROBOTHOR_VERDICT_COMMITMENT_MODE` (each `off` | `observe` | `enforce`,
default `observe`)

**Did the run's answer come from everything it actually saw — or did it stop
looking, and stop deciding, partway through?**

All three are three-rung, no `alert`: none of them blocks an operator-visible
action, so none has a rung to page on. All three default to `observe`, which
on this ladder means "keep the ledger, log what `enforce` would have done, say
nothing to the model, change nothing about the run" — the same meaning
`observe` carries for step efficiency's `off`-is-main baseline and for
`reask_for_wrong_deliverable_shape`.

## The measurement

2026-09-16, WildClawBench `03_Social`, the harness's own graders.

| | task_2 (truncation ledger) | task_5 (act → observe) |
|---|---|---|
| Score | **0.4895** vs a competing harness's **0.9664** | **0.3700** vs **0.8250** |
| What happened | one listing call returned `{"records": [...], "total": 20}`; the 4,000-character window cut the response inside the array, at record twelve — and `total`, the one field that would have exposed the cut, sat in the discarded tail | nineteen state-changing calls ran inside one shell script that printed only each call's `status`; the service's three inline follow-up messages were never printed and the inbox was never re-read before the report |
| The agent said | "all 12" | a report the grader scored `output_quality` **1.0** and `severity_accuracy` **0.0** in the same pass |
| Grading | every graded item inside the visible window scored full marks; every item past the cut scored zero | a perfect-reading report about a world the agent had already changed and stopped watching |

The verdict-commitment case is not a WildClawBench score — it is a planted
probe. A message carried a QA-routing marker in its footer. The agent **found**
the marker and still filed the message as Critical #1 with two named owners,
appending *"please verify whether this is a live incident or a quarterly QA
routing test"*. The rubric was binary; a top-of-report P0 is an escalation
whatever the footnote underneath it says, and the item scored zero. The agent
did not fail to see the evidence — it saw it and handed the decision back to
the reader inside the artefact whose job was to contain the decision.

None of these three is a capability gap. Every one is a case where the run had
the right information somewhere in its own trace and did not act as though it
did.

## What the three controls do

| Control | `off` | `observe` (default) | `enforce` |
|---|---|---|---|
| `truncation_ledger` | no ledger built at all | the ledger is kept; unresolved entries are logged at WARNING with the run id; unresolved entries at finalization write an `agent_guardrail_events` row (`action='observed'`); nothing is shown to the model | each unresolved entry is quoted to the model once; a run trying to finish with one outstanding is held for up to TWO more model turns — "go and read it", then "say in your answer what you did not read" — and then ends regardless; still-unresolved entries at finalization write the same row with `action='blocked'` |
| `act_observe` | no ledger, no classification | pending unread state-changes are logged at WARNING; unresolved changes at finalization write an `agent_guardrail_events` row (`action='observed'`) | one note, once per run, telling the agent what it changed and has not re-read — delivered at a check-in if one fires, otherwise at the run's stop, which costs one extra model turn; it never fails a run; still-pending changes at finalization write the same row, also `action='observed'` |
| `verdict_commitment` | nothing computed | a WARNING naming the hedged items, plus an `agent_guardrail_events` row (`action='observed'`) from `record_verdict_findings` | one re-ask, at most once per run, quoting up to five hedged items and asking for one verdict each; findings that survive it write the same row with `action='blocked'` |

Three things about this table are easy to misread:

* **`action='blocked'` here does not mean a tool call was refused.** Nothing in
  any of the three ladders ever refuses a call. It means the entry was still
  unresolved when the run reached finalization even under `enforce` — the extra
  asks did not produce a read — and the run finished saying so. A `blocked` row
  is evidence the safety net caught something the ask alone did not fix.
* **`act_observe` never writes `blocked`, on any rung.** Its own flag doc
  promises it never fails a run, and it does not: at `enforce` it costs at most
  one extra model turn. A table reading three blocks where one of them is
  advice is a table nobody can act on, so its rows are always `observed`.
* **A resolved entry never appears at all.** If the run reads its own spill
  back, or re-reads the source it changed, before it tries to stop, no row is
  written for that entry on either rung. The absence of a row is therefore
  ambiguous between "this never happened" and "this happened and the run
  recovered on its own" — see "How to read a quiet table" below.

**A hold is a model turn, not a refusal.** This is the distinction the first
cut of `truncation_ledger` got wrong and it is worth stating plainly. The
runner's stop branch reads

```python
if nudge_for_missing_deliverable(session, _workspace):
    continue
return
```

so a control that appends a note and returns `False` returns the run — no
further LLM call happens, and `session.get_final_text()` walks backwards to the
last message whose role is `assistant`, which is the answer produced *before*
the note. A note delivered that way reaches the transcript and nothing else.
Every hold described above therefore returns `True` and costs a real model
turn, and every one of them is bounded so the run always ends.

### Truncation ledger

`exec` no longer destroys the tail of a truncated stream. `robothor/engine/exec_spill.py`
writes the whole stream to `<workspace>/.robothor/exec/`, and the head the
model sees still ends with the same visible marker `#583` introduced, now
naming a path it can act on:

```
[truncated: 4000 of 12431 chars shown — the full output is at <path>;
read_file it, or re-run with a narrower command]
```

`robothor/engine/observation_ledger.py` registers `(step, tool, chars_shown,
chars_total, path)` for every truncated result and clears an entry only two
ways, both requiring a call that came back **whole**:

* **the run reads the spill file back**, through any tool that can read one —
  `read_file`, or `cat`/`head`/`grep`/`sed` through `exec`, or `open()` inside
  a snippet. Recognised by `exec_spill.spill_paths_in`, which requires a token
  outside a comment, the filename this engine writes, the right directory, and
  a file that actually exists. The same function keeps that reader exempt from
  the repeat-call guard and the no-progress detector — paging in the rest of
  your own output is progress, not a loop; or
* **the run re-runs the same tool over the same target, narrower**, and this
  time the result is neither truncated nor empty. Targets are compared with the
  query string stripped, so `…/messages?limit=2` answers a truncated
  `…/messages`; and `curl -s -o /dev/null …` does not clear anything, because
  it observed nothing.

It does **not** clear because the model mentioned the path in its answer. A run
that writes "some output was truncated" and finishes anyway has demonstrated
only that it read the marker — the failure this control exists for is a
confident answer over partial input, and a caveat is not a read.

Entries are keyed by `(step, stream)`, so an `exec` that cut both its streams
has two of them and reading the stdout spill back does not clear the stderr
one.

### Act → observe

A call that changes remote state invalidates the observations that preceded it
against that same source. `robothor/engine/act_observe.py` classifies every
admitted call as a read, a change, or neither — tool names come from
`tools/read_only.declared_read_only_tools()`, the same table the parallel
planner already trusts, so the two controls can never disagree about whether a
tool writes. `exec` and `execute_code` get a narrow shell heuristic: an HTTP
method that is not a read, `curl --data`, `requests.post`-and-friends. A false
"you changed something" teaches an agent to ignore the note, so an
unrecognised call classifies as neither — conservative in the direction that
costs a missed note, not a mistrusted one.

Writing the run's own deliverable is **not** a state change, whatever path it
is written to. That was the first cut's biggest defect: `source_tokens` counted
anything containing a slash, every graded task writes its answer to an absolute
path, and the note therefore fired on nearly every run with advice about
nothing. A change now needs either a remote target — a scheme, a dotted host
with a path, or a bare `host:port/path` — or a name shaped like one (`send_*`,
`create_*`, `*_write`, `*_reply`), which is how a CRM row or a mailbox gets
classified when its arguments name no host at all.

A run that made at least one state-changing call against a source and has not
read that source since gets told so once, at `enforce` only. Delivery is at a
deliverable check-in if one fires, and **otherwise at the moment the run tries
to stop**. That second path matters more than it looks: the check-in cadence is
every 25 iterations and the deadline rungs are at 50/80/95% of the budget, and
the run this control was built for made 21 requests in 85.9 s of a 300 s
budget, crossing neither. Separately — and on **both** rungs, since it is diagnostic
rather than gated the same way — `execute_code`'s result carries
`unread_responses` / `unread_response_tools` / `unread_response_note`, counting
proxied `genus_tools` calls whose response body the snippet's own stdout never
printed. That is the exact shape of the measured failure: nineteen `genus_tools`
calls, twelve of them printing nothing but `status`.

### Verdict commitment

The narrowest of the three, because it is the only one that touches model
*judgement* rather than information the run already has on disk. It does
nothing unless the task itself asked for a decision on each of several items —
triage, classify, route, prioritise, in so many words, AND distributed over
items — `each`, `every`, `for each`, `all the`, or the contract stated outright
as `exactly one` / `one of the following` (`asks_for_verdicts`). Read
read-only against all 61 WildClawBench task specs, 4 open the gate, and all
four are genuinely "classify each into exactly one category" tasks. Inside such a deliverable it fires on exactly
two shapes, both anchored on an explicit item identifier (`msg_2209`, `#12`,
`TASK-4`):

* the same item appears under two different verdict **labels** — read only
  from label positions (a heading, a bolded lead, a `Severity:` field), never
  from prose, so "confidence is moderate" in a sentence cannot be misread as a
  medium verdict. Every label is a PHRASE, never a bare word: `**Priority:
  High** — this is a test-infrastructure item` is one verdict, not two; or
* the item's own block asks the reader to decide (*"please verify whether…"*,
  *"a human should decide"*) **about the verdict itself**. "Please confirm
  whether the three remaining endpoints are in scope" is a question about the
  work asked alongside a verdict that was reached, and is silent.

An item with one verdict and an inline caveat produces nothing — that is
deliberate. The rule is one verdict per item, not zero doubt: contradicting
evidence is supposed to resolve **into** the verdict as its reason, not
disappear.

`observe` logs a WARNING naming the hedged items. `enforce` re-asks once,
quoting up to five findings, and asks the agent to pick one verdict per item
and fold the contradiction in as the reason.

## What Controls-page evidence exists today — and what does not

The Controls page and `flag_audit.py` read all three from
`agent_guardrail_events`:

```sql
SELECT guardrail_name, action, count(*)
FROM agent_guardrail_events
WHERE guardrail_name IN ('truncation_ledger', 'act_observe', 'verdict_commitment')
  AND created_at > now() - interval '7 days'
GROUP BY 1, 2
ORDER BY 1, 2;
```

All three write that row on **both** `observe` and `enforce`, which is what
lets "how often would this have fired" be answered before any of them moves:

| `guardrail_name` | written by | called from |
|---|---|---|
| `truncation_ledger` | `observation_notes.record_observation_verdicts` | `run_finalizer`, once per run |
| `act_observe` | `observation_notes.record_observation_verdicts` | the same call |
| `verdict_commitment` | `verdict_commitment.record_verdict_findings` | `record_observation_verdicts`, so the finalizer has one call site for the cluster |

`verdict_commitment`'s writer exists for a specific reason. The first cut of
this branch had none: `flags/evidence.py` declared
`guardrail_name = 'verdict_commitment'` for it — a governed flag with no entry
there takes down `GET /api/controls` with a `KeyError` — while no code path
wrote that row, so `verdict()` would have reported the flag `INERT`
permanently, on every rung, however many real hedged deliverables it caught.
That is `controls-were-armed-but-aimed-at-nothing` exactly, and it is the flag
whose entire promotion story is "watch the evidence first". It was caught in
review rather than in production.

## How to read a quiet table

A zero here can mean four different things, and this repo has already paid for
conflating them once — `feedback-probe-dont-trust-silence` (five previously
"working" controls found to be enforcing nothing at all) is the standing
reminder not to read an empty count as proof of nothing happening:

1. **Nothing truncated, nothing changed, nothing hedged.** The genuinely boring
   case — a workload of short `exec` outputs and read-only agents produces no
   rows because there is nothing for either ladder to register.
2. **The run recovered on its own.** An entry that resolved before a check-in —
   the spill was read back, the source was re-read, the report was already
   committed — never becomes a row on either rung. A quiet table is consistent
   with a control working perfectly and never needing to say anything.
3. **The control is aimed at nothing on this workload.** If your fleet's agents
   never produce a 4,000+ character `exec` result, and never both write and
   then answer from the same source in one run, `truncation_ledger` and
   `act_observe` have nothing to catch — this is the same "quiet because
   nothing repeats" case `STEP_EFFICIENCY.md` describes for the repeat-call
   guard, and it calls for the same response: do not promote on a quiet table,
   go measure a workload that actually exercises the failure mode first.
4. **Nothing is wired to record it.** All three controls write their row on
   both rungs today, so this case should not arise — but it is the one that
   cost this branch a review finding, and it is the one an operator cannot
   distinguish from case 1 by looking at the table. Before reading a zero as
   case 1, confirm a writer exists: `grep -n log_guardrail_event
   robothor/engine/observation_notes.py robothor/engine/verdict_commitment.py`
   should find one call in each.

Before reading a zero as "there is nothing here", check which of the four it
is: re-run the WildClawBench `03_Social` tasks in the sandbox (below) and
confirm the rows land at all; if they do not even on a task built to trigger
them, that is case 3 or 4, not case 1.

## The spill directory

`exec` writes the whole of a truncated stream to
`<workspace>/.robothor/exec/`, one file per stream over its limit
(`STDOUT_LIMIT` 4,000 chars, `STDERR_LIMIT` 2,000 chars — unchanged from the
marker-only fix), named `<run-stem>__<stream>__<random>.txt`. Under
`.robothor/` on purpose, so it is never mistaken for a deliverable and never
collides with the `.robothor/secret*` prefix the tool sandbox already refuses
to serve. An unresolvable workspace writes nothing rather than guessing at a
path outside it.

One spill is capped at `ROBOTHOR_EXEC_SPILL_MAX_BYTES` (default 8 MiB), and a
spill that would leave the filesystem with under 64 MiB free is refused
outright — the result degrades to the marker-only form rather than the command
failing. Before this existed nothing from a command reached the disk at all, so
a single `exec` under the 900-second ceiling could fill the workspace; a 50 MB
command measured at 50,000,000 bytes on disk. When the file is capped the
result says so (`stdout_spill_capped`, `stdout_spill_chars`) and the marker
stops promising a whole the file does not hold.

Retention is two-layered:

* **The run reaps its own on the way out.** `record_observation_verdicts`
  calls `exec_spill.prune_run_spills` at finalization, deleting every file
  whose stem matches that run's id, whether or not the ledger ever read them
  back.
* **The 7-day sweep is the backstop**, for a run killed before it gets there.
  `robothor/engine/retention.py`'s `run_retention_cleanup()` now calls
  `exec_spill.prune_spill_files()` under the `"exec"` key of its results dict,
  alongside the other spill directories it already sweeps. The window is
  `exec_spill.DEFAULT_SPILL_RETENTION_DAYS` (7 days); a `retention_days <= 0`
  disables the sweep rather than deleting on the spot, on the same reasoning as
  every other retention policy in that module — "keep for zero days" reads as a
  misconfiguration, not an instruction.

Nothing reads a spill file after the run that wrote it ends. A directory
nobody ever prunes is exactly the shape of the `analyze_image` spill's one bad
release, which is why the backstop exists rather than relying on every code
path to reach its own cleanup.

## Promoting or de-escalating a rung

All three are on the **three-rung** ladder in `robothor/flags/store.py`
(`_THREE_RUNG_MODE_FLAGS`) — the dashboard and `genus flags` offer only
`off` / `observe` / `enforce` for these names, never `alert`, for the same
reason step efficiency and the honesty suite have no `alert`: neither ladder
blocks an operator-visible action, so there is nothing an `alert` rung would
page on that the `observe`-rung `agent_guardrail_events` row does not already
record (where that row exists — see above).

* **A single instance, right now**: the Controls dashboard writes a
  `feature_flags` row through `robothor.flags.store.set_flag`, which beats
  every file layer and shows in `flag_audit.py` as
  `PINNED:db@operator:<id>`. This is the fast path for one operator flipping
  one flag on one box and needs no restart.
* **Fleet-wide, governed**: follow `GUARDRAIL_FLIPS.md`'s flip procedure —
  edit the mirrored drop-in in a PR (link the soak evidence: the query above,
  or the sandbox sweep below), merge, apply with
  `scripts/install-units.sh` + `systemctl restart robothor-engine`, confirm
  `scripts/check_dropin_drift.sh` prints `OK`, then watch the daily
  guardrail-watch report for the new mode and probe one deliberate violation.
* **De-escalating** is the same two paths in reverse — a `PINNED:db@operator:<id>`
  row or a drop-in revert — and the reason belongs in the flip's `reason`
  field or the revert PR, the same as any other flag on this ladder.

`infra/flags.yaml` carries `planned_promotion: "2026-10-15"` for
`ROBOTHOR_TRUNCATION_LEDGER_MODE` and `ROBOTHOR_ACT_OBSERVE_MODE` — both
blocked on a re-measured WildClawBench `03_Social` sandbox sweep (two runs each
of task_2 and task_5) showing the tail actually reaches the model's answer,
plus real `agent_guardrail_events` rows proving entries get registered on live
runs, not only in tests.

### `ROBOTHOR_VERDICT_COMMITMENT_MODE` has no promotion date

`infra/flags.yaml` carries `planned_promotion: n/a-on-this-instance` for this
one, and that is deliberate, not an oversight — the same shape
`ROBOTHOR_PLUGIN_MANIFEST_MODE` uses for "there is nothing here to promote
against yet". Two reasons, and either alone would be enough:

1. It is the only one of the three that touches model **judgement**, not
   information already sitting in the run's own trace. A false positive here
   does not just cost a wasted re-ask — it teaches an agent to hedge less
   honestly rather than to decide, which is a worse outcome than the defect
   this control corrects. `truncation_ledger` and `act_observe` fail closed to
   "read something you already produced"; this one fails toward "commit to a
   judgement you might be wrong about", and that asymmetry is why it gets its
   own flag rather than sharing a ladder with the other two.
2. **It has not been probed with a real double-verdict artefact in a real
   run.** It has been run read-only against all 61 WildClawBench task specs: 4
   open the gate at all, and all four are genuinely "classify each into exactly
   one category" tasks. Its detector has also been driven over the measured
   hedged report and over nine negative fixtures, three of which it used to get
   wrong. That is a good negative-case result and it is not the positive proof
   this control needs before a date belongs next to it. Per
   `feedback-probe-dont-trust-silence`, that proof has to come from firing a
   genuine double-verdict artefact through `enforce` in a live run and
   confirming the re-ask lands and is answered correctly — not from reading a
   WARNING line the control printed about itself.

Until that probe exists, `verdict_commitment` stays on `observe` regardless of
what the other two do, and `tests/test_flag_manifest.py`'s dated-entry check
does not apply to it because it carries no date to go stale.

## Where it lives

| Piece | File |
|---|---|
| `exec` output shaping, the spill, the marker | `robothor/engine/exec_spill.py` |
| Truncation and act-observe ledgers, the finalization row | `robothor/engine/observation_ledger.py` |
| Act-vs-observe classification, source tokens, unread-proxy-response note | `robothor/engine/act_observe.py` |
| One-verdict-per-item detection and re-ask | `robothor/engine/verdict_commitment.py` |
| In-loop hold (deliverable check-in) | `robothor/engine/loop_guards.py` (`unread_observation_hold`, `hold_for_hedged_verdicts`) |
| Finalization call site, spill reaping | `robothor/engine/run_finalizer.py` |
| Retention backstop for orphaned spills | `robothor/engine/retention.py` (`run_retention_cleanup`, key `"exec"`) |
| Flag readers | `robothor/engine/feature_flags.py`: `truncation_ledger_mode`, `act_observe_mode`, `verdict_commitment_mode` |
| Evidence source declarations | `robothor/flags/evidence.py` |
| Three-rung ladder membership | `robothor/flags/store.py` (`_THREE_RUNG_MODE_FLAGS`) |
| Intent, soak notes and gates | `infra/flags.yaml` |
| Settings, generated reference | `robothor/settings/model.py`, `docs/reference/configuration.md` |
| Tool-facing description of the marker and spill | `docs/TOOLS.md` (`exec`) |
| Benchmark container env | `bench/wildclaw/harness.py` (`_container_command`) |

## Measuring it in the sandbox

`bench/wildclaw/harness.py` writes `ROBOTHOR_TRUNCATION_LEDGER_MODE=enforce`
and `ROBOTHOR_ACT_OBSERVE_MODE=enforce` into the task container's env file
unless the host exports a different value, so a sandbox re-run of `03_Social`
measures both at `enforce` by default. `ROBOTHOR_VERDICT_COMMITMENT_MODE` is
left at `observe` — the harness runs it exactly where the fleet runs it, on
purpose, because a benchmark container is the wrong place to discover a false
positive on the one ladder that touches judgement, before it has been probed.

Export a different value on the host to override any of the three for a
differential sweep, the same idiom `STEP_EFFICIENCY.md` documents: the harness
only forwards a host `ROBOTHOR_*` value when the task's own `env:` block names
it, so exporting the variable without checking that block still measures the
in-container default.

## Promoting it — the checklist

1. Run a sweep in `observe` and read the `agent_guardrail_events` counts above
   for `truncation_ledger` and `act_observe`. A quiet table is not enough by
   itself to promote — work through "How to read a quiet table" first, and if
   it is case 3 (aimed at nothing on this workload), find a workload that
   exercises the failure before promoting on it.
2. Re-measure WildClawBench `03_Social` task_2 and task_5 **in the sandbox**
   with both flags at `enforce` and confirm the scores move toward the
   competing harness's, not away from it.
3. Confirm no run that previously completed now gets stuck: `unread_observation_hold`
   is bounded to exactly one extra ask per run by design
   (`ObservationLedger.holds_used`, `MAX_HOLDS = 1`) — a run holding for more
   than one extra turn on this control is a bug, not the control working
   harder.
4. Leave `verdict_commitment` where it is until the probe in the section above
   exists, whatever the other two flags do.
