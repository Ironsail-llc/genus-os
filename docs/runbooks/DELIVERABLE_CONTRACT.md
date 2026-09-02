# Deliverable contract — runbook

`ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED` + `ROBOTHOR_DELIVERABLE_CONTRACT_MODE`

**Did the run produce the artifact the task named?**

## What it is, and what it is not

This is the complement of `ROBOTHOR_COMPLETION_CONTRACTS_MODE`, not a second
copy of it:

| Control | Question |
|---|---|
| `completion_contract` | Are the agent's **claims** backed by evidence in its own trace? |
| `run_verification` | Same question, without needing a session goal |
| `deliverable_contract` | Does the **artifact the task named** actually exist? |

An agent passes all the claim-shaped checks and still fails this one by doing
the work correctly and saving it to the wrong path. Nothing it said was untrue;
nothing it produced was usable.

The case that motivated it, 2026-08-26 (WildClaw task_4). The spec said *"save
them to `/tmp_workspace/results/2022.tsv`"*. The agent did the research
properly — 7 of 9 author homepages verified with live HTTP 200s — then wrote
`/tmp_workspace/results/summary.md`. Every criterion scored 0.00,
`output_exists` included, after 3.4M tokens. That single task carries **−0.87
of a −1.04 competitive gap in which 7 of the 10 tasks were already at parity**.
The gap was not a capability deficit. It was a contract failure.

## Where it lives

| Piece | File |
|---|---|
| Extraction + verdict (pure, importable) | `robothor/engine/deliverable_contract.py` |
| Post-run verdict + guardrail event | `robothor/engine/run_finalizer.py` |
| In-loop nudge (one per run) | `deliverable_nudge`, via `robothor/engine/loop_guards.py` |
| Flag reader | `robothor/engine/feature_flags.py: deliverable_contract_mode` |
| Evidence source | `robothor/flags/evidence.py` |
| Intent, soak and gates | `infra/flags.yaml` |

Task text comes from `task_text_for_run`: the originating `crm_task`
(title + objective + next_action) when there is one, otherwise the run's
originating message. The message fallback matters — most runs have no
`crm_task` at all, benchmark tasks included, and that is exactly the population
this contract was built for.

Extraction is deliberately conservative. Only an explicit output verb
(`save`/`write`/`store`/`export`/`output`/`put`/`place`/`dump`/`emit`) pointed
at a concrete, local, extensioned path counts. Reads, prose, bare extensions
and URLs do not. A control that nags about a file that was never a deliverable
gets muted, and a muted control protects nothing.

Existence alone is not satisfaction: an empty file at the right path is a
touched path, not a produced deliverable.

## The ladder

| Rung | Behaviour |
|---|---|
| `off` | never computed. Default. |
| `observe` | one `agent_guardrail_events` row (`action='observed'`) per **unsatisfied** verdict. Satisfied and not-applicable runs write nothing — a vacuous pass on every run in the fleet would bury the real verdicts. Nothing else changes. |
| `alert` | observe + `notify_guardrail_alert` to the operator |
| `enforce` | the event is recorded as `action='blocked'` and the operator is alerted. It does **not** currently stop the run or the task closing. |

Read that last row carefully before promoting: at `enforce` this control is
still a recorder, not a gate. Promote it to say "we have read these verdicts
and they are right", not to make something stop.

The in-loop nudge is the half that can actually change an outcome, and it is
budgeted at **one per run** (`MAX_DELIVERABLE_NUDGES`). An unbounded "you are
not done" is a loop, and the agent may have a good reason the artifact is
absent that trying again will not fix.

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

Know why the table is quiet before you read anything into it. Probed against
production 2026-08-27: of **4,000 `crm_tasks` over 60 days, ZERO named an
explicit output path**. Delegated tasks on this instance are written in prose;
the contracts live in prompt-borne task specs, which is why the message
fallback exists. A control wired only to `crm_tasks` here would be a guard on
an empty table — the mistake this instance has now made six times
(`feedback-probe-dont-trust-silence`).

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

## Probe (do not trust silence)

The control is quiet by design on most runs. To prove it can fire at all,
exercise it rather than inferring from the absence of rows:

```python
from robothor.engine.deliverable_contract import check_deliverables, required_deliverables

req = required_deliverables("Research the 2022 papers and save them to /tmp/probe/2022.tsv")
assert req, "extraction found no deliverable — the control cannot fire"
print(req, check_deliverables(req))   # satisfied=False while the file is absent
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
