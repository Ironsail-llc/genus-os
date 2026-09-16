# Deliverable contract — runbook

`ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED` + `ROBOTHOR_DELIVERABLE_CONTRACT_MODE`

**Did the run produce the artifact the task named, in the shape it named?**

## What it is, and what it is not

This is the complement of `ROBOTHOR_COMPLETION_CONTRACTS_MODE`, not a second
copy of it:

| Control | Question |
|---|---|
| `completion_contract` | Are the agent's **claims** backed by evidence in its own trace? |
| `run_verification` | Same question, without needing a session goal |
| `deliverable_contract` | Does the **artifact the task named** exist, and is it the **shape the task described**? |

An agent passes all the claim-shaped checks and still fails this one by doing
the work correctly and saving it to the wrong path, or to the right path in the
wrong shape. Nothing it said was untrue; nothing it produced was usable.

The case that motivated it, 2026-08-26 (WildClaw task_4). The spec said *"save
them to `/tmp_workspace/results/2022.tsv`"*. The agent did the research
properly — 7 of 9 author homepages verified with live HTTP 200s — then wrote
`/tmp_workspace/results/summary.md`. Every criterion scored 0.00,
`output_exists` included, after 3.4M tokens. That single task carries **−0.87
of a −1.04 competitive gap in which 7 of the 10 tasks were already at parity**.
The gap was not a capability deficit. It was a contract failure.

**The case that made it check shapes, 2026-09-16.** Three Productivity tasks,
run like-for-like against a competing harness — same tasks, same model, the
benchmark authors' own graders. The competitor scored **86 / 91 / 49**; we
scored **0 / 0 / 0**, in every run. In all three the agent did the research and
then ignored the output contract:

| Task shape | What the spec said | What we wrote |
|---|---|---|
| tabular | six columns in an exact order, tab-delimited | five columns of our own; two required fields dropped |
| exact output set | three named outputs under `results/` | one file of our own naming in the workspace root; `results/` empty |
| structured document | the section headings verbatim, *"do not rename these"* | the right path, our own headings |

A path-only contract sees a file at the right place and says nothing. In the
first of those the path check scored 1 and every other criterion scored 0.

## Where it lives

| Piece | File |
|---|---|
| Item types, statuses, the report | `robothor/engine/deliverable_items.py` |
| Extraction from task text (pure) | `robothor/engine/deliverable_extract.py` |
| Checking the workspace (pure, confined) | `robothor/engine/deliverable_check.py` |
| Façade, task text, the notes it writes | `robothor/engine/deliverable_contract.py` |
| Ladder, guardrail event, honest failure | `robothor/engine/deliverable_verdict.py` |
| In-loop nudge (one per run) | `deliverable_nudge`, via `loop_guards.nudge_for_missing_deliverable` |
| In-loop shape re-ask (one per run) | `loop_guards.reask_for_wrong_deliverable_shape` |
| Check-in comparison | `contract_checkin_note`, via `loop_guards.append_engine_note` |
| Task text column | `agent_runs.task_text` (migration 123) |
| Flag reader | `robothor/engine/feature_flags.py: deliverable_contract_mode` |
| Evidence source | `robothor/flags/evidence.py` |
| Intent, soak and gates | `infra/flags.yaml` |

### Where the task text comes from

`task_text_for_run`, in this order:

1. **`run.task_text`** — the originating prompt, persisted at session start
   (migration 123). The only source that survives compaction, a resumed run,
   and the finalizer, which runs after the loop has ended. Redacted through the
   same door as chat history (`robothor.secrets.redaction.redact`) and capped
   at 32,768 characters, eliding the **middle** so that a spec's
   "Output Requirements" section at the end survives the cap.
2. the live session's originating message, for a run still in flight.
3. the originating `crm_task` (title + objective + next action).

The crm_task is **last**, on measured evidence: of 4,000 crm_tasks over 60 days
on the first production instance, ZERO named an explicit output path. Reading
it first meant a run that had a task row could never see its own prompt.

### Keeping the contract in front of the model

The checker can only be as good as what the model still has. Measured
2026-09-16 across ten benchmark runs on one model: the four whose transcript
began with a compaction summary scored a mean of **0.016**, the six that never
compacted **0.450**. The required output header appears **zero** times in the
worst one's entire context — the spec had been summarised away, and the agent
invented its own columns. The competing scaffold compacts *harder* and is
immune, on one constant: it pins the first three messages.

Two defences, both general:

| Defence | Where | Gated on |
|---|---|---|
| The first N messages are never summarised away | `compaction.protected_prefix_len`, `ROBOTHOR_COMPACTION_PROTECT_FIRST_N` (default 3) | nothing — losing the task statement is a correctness bug, not a guardrail |
| The output contract is re-rendered verbatim after every compaction | `deliverable_contract.contract_sticky_block`, via `context._restore_output_contract` | `ROBOTHOR_DELIVERABLE_CONTRACT_MODE != off` |

The protected prefix shrinks rather than ending on an assistant turn that
called a tool: protecting the CALL while its RESULT is summarised away leaves a
dangling `tool_call`, which several providers reject outright.

The sticky block is extracted from the **pre-compaction** messages, where the
spec certainly still is; appended last, so it is the most recent thing the model
reads; appended once, never stacked; bounded at 2,000 characters, because it is
re-sent on every compaction of every long run; and marked as the task's own
words, because an agent that cannot tell an engine reminder from the task itself
will argue with one of them.

### What is extracted

Only from language that states the requirement explicitly. Nothing is inferred
from an example, a mention or a read instruction.

| Item | Anchor in the task text | What it carries |
|---|---|---|
| `PathItem` | an output verb (`save`/`write`/`store`/`export`/`output`/`put`/`place`/`dump`/`emit`) pointed at a concrete local extensioned path | the path |
| `ExactSetItem` | *"the following outputs under `dir/`:"* (or *"into `dir/` with the following files:"*) + a bullet list | the directory, the names, and whether extras are forbidden |
| `HeaderItem` | *"use exactly the following header"* + a fenced line, or *"columns: …"* | the header line, a best-effort column split, the delimiter |
| `JsonFieldsItem` | *"must contain exactly these fields"* + a fenced JSON example | the field names and whether the container is an array or an object |
| `SectionsItem` | *"using exactly the following structure"* / *"do not rename these section headings"* + a fenced block | the heading names |
| `SortItem` | *"sorted by X ascending, then by Y"* | the key columns — **only** where a `HeaderItem` names them |

Every item carries the sentence it came from (`evidence`), so the re-ask can
quote the spec rather than assert at the agent.

Extras are forbidden only when the spec said so **and** the list could be
enumerated: a bullet reading *"one or more source `.tex` files"* is a
requirement with no name in it, and a set with an unknown member cannot call
anything an intruder.

### What is checked

| Item | Rule |
|---|---|
| `PathItem` | exists and is non-empty. An empty file at the right path is a touched path, not a deliverable. |
| `ExactSetItem` | every name present (a trailing `/` must be a directory); extras named only when the set is exact. Dotfiles are workspace bookkeeping and are ignored. |
| `HeaderItem` | the first line and the required line compared with **whitespace collapsed on both sides**, after stripping a BOM, a CRLF and per-cell padding. A file that does not use the delimiter at all fails even so. |
| `JsonFieldsItem` | parsed; array-vs-object checked; field names compared exactly, missing and unexpected both named. |
| `SectionsItem` | heading **names**, normalised (case, trailing colon, `#` level ignored). The spec said do not rename them, not do not re-nest them. |
| `PatternItem` | at least one non-empty file matching the glob. A spec that writes a placeholder (`results/scp-XXX/text.md`) promised a shape, not a filename; the literal parts still hold. |
| `SortItem` | rows parsed and compared, but only on a file whose header already matches — otherwise the header item carries the same fault twice. |

### When a check declines to answer: `unchecked`

A verdict computed on part of a file is a lie, and the expensive kind — under
`enforce` it fails a correct run. So a file the check cannot read **whole**
produces `unchecked` rather than a mismatch:

| Check | Limit | Beyond it |
|---|---|---|
| header | first line only (64 KB) | never declines |
| JSON fields | **64 MB**, parsed whole | `unchecked` |
| section headings | **2 MB** | `unchecked` — a long report is exactly the deliverable most likely to cross this |
| sort order | **2,000,000 rows**, streamed | `unchecked` |
| path, pattern, exact set | `stat` only | never declines |

`unchecked` is **not** a failure: `satisfied` stays True and `enforce` does not
fail the run, because not checking is not a fault of the run. It is also not
silence, which is the trap it fell into first (re-review R3): it is named in
`ContractReport.message` as a `NOT VERIFIED:` line, it reaches the agent at the
mid-run check-in while the agent can still do something about it, and it writes
its own guardrail row:

```sql
SELECT action, count(*) FROM agent_guardrail_events
WHERE guardrail_name = 'deliverable_contract'
GROUP BY 1;   -- blocked | observed | unchecked
```

`action='unchecked'` exists so the evidence query separates *"we looked and it
was wrong"* from *"we could not look"*. A promotion decision read off a table
that conflated them would be reading the wrong number.

### What counts as a refusal

Some tasks should not be completed, so `enforce` must not fail a run that
correctly declines — and equally must not be escapable by a run that simply
fell short. `deliverable_verdict.reads_as_a_refusal` requires **both**:

1. an explicit declining form (`I will not`, `I won't`, `I refuse`,
   `I'm declining`, `I shouldn't`, and the `I can't` / `I cannot` /
   `I am unable to` family, which is how an aligned model most often declines)
   **and** a reason about the task — harmful, misleading, against policy, told
   not to. The declining form alone is not enough: *"I cannot reach the site"*
   has a declining form and a reason about the tooling, and it stays a failure;
2. that nothing was attempted — every failing item `missing`. A refusal
   produces no file; a shortfall usually produces a wrong one.

Measured before the second condition existed: **7 of 9** ordinary failure
summaries read as refusals, because the engine's own hard-abort message asks
the agent to end with "What failed and why". Measured after: **0 of 9**.

A refusal is recorded as `observed` and never alerts — the promotion gate's
refusal audit needs the rows. A genuine refusal phrased without any of the
reason vocabulary is treated as a shortfall and fails; that is the safe
direction, and it is the known limit of the recogniser. The vocabulary is a
closed list, so **widen it from the audit's own rows**, not from imagination:
a `blocked` row on a Safety task whose output text is plainly a refusal is the
signal that a phrasing is missing.

**Everything is confined to the workspace.** Task text is untrusted input in
any deployment where someone else can file a task, and these paths reach the
filesystem: the path that is finally touched is always rebuilt from the trusted
workspace root, a path containing `..` is never a deliverable, and an item that
lands outside the workspace produces no finding at all.

**One verdict, not two.** The finalizer used to run a separate path-only check
(`check_run_deliverables`) alongside this. It asks the same question over the
same extracted paths, but resolves the task's own string against the whole
filesystem rather than the run's workspace — so running both reported the same
absent file twice and disagreed about it whenever the workspace was not the
directory the task's absolute path named. The `PathItem` is now the only place
that question is asked at finalization. `check_run_deliverables` remains as a
pure importable helper and still backs the in-loop path nudge.

## The ladder

| Rung | Behaviour |
|---|---|
| `off` | never computed. Default. |
| `observe` | one `agent_guardrail_events` row (`action='observed'`) per **unsatisfied** verdict. Satisfied and not-applicable runs write nothing — a vacuous pass on every run in the fleet would bury the real verdicts. Nothing else changes. |
| `alert` | observe + `notify_guardrail_alert` to the operator |
| `enforce` | the deliverable check-in becomes a comparison; the loop **re-asks once** with the report when the agent stops with a wrong-shaped deliverable; the finalizer then records `action='blocked'`, alerts, and **fails the run** with the report in `error_message` rather than letting it report `completed`. |

Read the `enforce` row carefully before promoting. Until 2026-09-16 that rung
was a recorder, not a gate; it now changes two things — what the model is told
mid-run, and whether a wrong-shaped run may call itself complete. A run that
had already failed for another reason keeps its own cause; only a run that
would otherwise claim success is turned over.

Two budgets, both **one per run**, for the same reason: an unbounded "you are
not done" is a loop, and the agent may have a good reason the deliverable is
absent or misshapen that trying again will not fix.

| Budget | Constant | Asks about |
|---|---|---|
| path nudge | `MAX_DELIVERABLE_NUDGES` | the artifact is absent or empty |
| shape re-ask | `session._deliverable_contract_reasks` | the artifact is there and is the wrong shape |

**The re-ask leaves room to refuse.** Some tasks *should* not be completed —
`06_Safety_Alignment` tasks name an output path for content an agent is right
to decline. A gate that can only say "produce it" converts a correct refusal
into a fabricated artifact, so the re-ask says in as many words that a task the
agent should not complete may be refused with a plain explanation, and that it
must not invent content to fill the file. This is the sharpest known hazard of
the `enforce` rung; the promotion gates below ask for it to be audited.

### One caveat on the relaxed error budget

`HARD_ABORT_TOTAL_ERRORS` now forgives one error per successful tool call
(`ERRORS_FORGIVEN_PER_SUCCESS`), which is what stops a run that recovers from a
hostile network being killed at 341 seconds of a 1,200-second budget. Be plain
about what that costs: a run that **alternates** failure and success forever is
never hard-aborted, and `THRESHOLD_STOP` (five *consecutive* errors) never fires
on it either. Such a run is then bounded only by the wall-clock watchdog and the
iteration safety cap. Both do end it, so it terminates — but the error budget no
longer contributes a bound, and that is a control being loosened rather than
tightened.

### What the extractor can read

English anchors, and a minimal Chinese set (`保存` / `写入` / `输出` / `存储`,
and the labelled `输出文件路径`). 34 of the 60 specs in the reference corpus
produce at least one item; before the Chinese anchors it was 22, and every
Chinese spec was a silent skip. A spec in any other language is invisible to
this control — silently, which is the safe direction, but do not read the sweep
numbers as coverage.

## Reading the evidence

```sql
-- Every verdict this control has ever recorded.
SELECT action, count(*), max(created_at)
FROM agent_guardrail_events
WHERE guardrail_name = 'deliverable_contract'
GROUP BY 1 ORDER BY 2 DESC;

-- The verdicts themselves, newest first, with the run that produced each.
SELECT e.created_at, r.agent_id, r.trigger_detail, e.reason
FROM agent_guardrail_events e
JOIN agent_runs r ON r.id = e.run_id
WHERE e.guardrail_name = 'deliverable_contract'
ORDER BY e.created_at DESC LIMIT 50;
```

`agent_guardrail_events` is pruned at 30 days, so an empty result means "no
evidence in the window", never "never fired".

## Promotion gates

**Do not promote on a quiet table.** As of 2026-09-02 there is exactly **one**
observed row. That is enough to prove the writer is wired and nowhere near
enough to characterise anything.

Know why the table was quiet before you read anything into it. Two reasons, one
of them now closed:

1. Probed against production 2026-08-27: of **4,000 `crm_tasks` over 60 days,
   ZERO named an explicit output path**. Delegated tasks on this instance are
   written in prose; the contracts live in prompt-borne task specs. A control
   wired only to `crm_tasks` here would be a guard on an empty table — the
   mistake this instance has now made six times
   (`feedback-probe-dont-trust-silence`).
2. A run's prompt was not persisted, so at finalization the control had nothing
   to read even for the runs that *did* carry a contract. **Closed by migration
   123** (`agent_runs.task_text`). Rows written before that migration have
   `task_text IS NULL` and were never eligible.

So re-probe before reading the table: an `observe` window that starts before
migration 123 is not evidence about the current control.

Before `alert`:

1. At least **20 observed verdicts across three or more agents**.
2. Every `observed` row hand-checked against the run that produced it. A
   "deliverable" the task never actually named is a checker bug, and it is the
   dominant false-positive class here.
3. The −0.87-shaped failure reproduced in **production** data, not only in the
   benchmark that found it.

Additionally before `enforce`:

4. Seven days at `alert` with no operator-reported false block.
5. A hand-audit of one week of verdicts confirming each names an artifact a
   reader agrees was genuinely promised.
6. **A refusal audit.** At least one task the agent was right to decline, run
   at `enforce`, confirming the re-ask did not push it into producing the
   artifact anyway. This is the rung's sharpest hazard and it is not visible in
   a false-positive count.

## Probe (do not trust silence)

The control is quiet by design on most runs. To prove it can fire at all,
exercise it rather than inferring from the absence of rows:

```python
from robothor.engine.deliverable_contract import check_deliverables, required_deliverables

req = required_deliverables("Research the 2022 papers and save them to /tmp/probe/2022.tsv")
assert req, "extraction found no deliverable — the control cannot fire"
print(req, check_deliverables(req))   # satisfied=False while the file is absent
```

Same for the shape half. Both halves are pure, so this needs no run, no
database and no flag:

```python
from pathlib import Path
from robothor.engine.deliverable_contract import check_contract, extract_contract

fence = "`" * 3
spec = "\n".join([
    "Save the table to `/probe/results/rows.tsv`.",
    "",
    "The TSV must use exactly the following header:",
    "",
    fence + "text",
    "Track   Title   Speakers",
    fence,
])

root = Path("/tmp/probe-shape")
(root / "results").mkdir(parents=True, exist_ok=True)
(root / "results" / "rows.tsv").write_text("Title\tSpeakers\n")

contract = extract_contract(spec)
assert contract.items, "extraction found no shape — the control cannot fire"
print(check_contract(contract, root).message)
# `results/rows.tsv` header is `Title Speakers`; the task requires
# `Track Title Speakers` — missing required columns `Track`.
```

A run tagged `trigger_detail = 'probe:ROBOTHOR_DELIVERABLE_CONTRACT_MODE...'`
is what `scripts/flag_audit.py` reports in its `last_probe` column.

## Flipping it

The flag ships in the versioned drop-in
(`infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf`) and its
intent in `infra/flags.yaml`. Both move in the same PR — see
[`GUARDRAIL_FLIPS.md`](GUARDRAIL_FLIPS.md) for the procedure and for why it
must never be set in `/etc/robothor/robothor.env`.

It is also a governed flag (`robothor.flags.store.GOVERNED_FLAGS`), so the
Controls dashboard can flip it at runtime with no restart. A dashboard flip
beats every file layer and shows up in `flag_audit.py` as
`PINNED:db@operator:<id>`; clear the row when you are done, or the drop-in
value stays inert.

## Rollback

Set `ROBOTHOR_DELIVERABLE_CONTRACT_MODE=off` (or clear
`ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED`) in the drop-in, `daemon-reload`,
restart the engine. At `off` the check is never computed and the finalizer
path is skipped entirely. Nothing it wrote needs unwinding: every artifact of
this control is an append-only event row.
