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
| Extraction + verdict (pure, importable) | `robothor/engine/deliverable_contract.py` |
| Post-run verdict, guardrail event, honest failure | `robothor/engine/run_finalizer.py: _record_deliverable_shape` |
| In-loop nudge (one per run) | `deliverable_nudge`, via `robothor/engine/loop_guards.py` |
| In-loop shape re-ask (one per run) | `reask_for_wrong_deliverable_shape`, same file |
| Check-in comparison | `contract_checkin_note`, injected from `robothor/engine/runner.py` |
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
| `SortItem` | rows parsed and compared, but only on a file whose header already matches — otherwise the header item carries the same fault twice. |

**Everything is confined to the workspace.** Task text is untrusted input in
any deployment where someone else can file a task, and these paths reach the
filesystem: the path that is finally touched is always rebuilt from the trusted
workspace root, a path containing `..` is never a deliverable, and an item that
lands outside the workspace produces no finding at all.

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
