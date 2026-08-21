# Tool Post-Condition Verification — Rollout Runbook

**Flags:** `ROBOTHOR_TOOL_VERIFY_ENABLED` + `ROBOTHOR_TOOL_VERIFY_MODE`
**Ladder:** off → **observe** (ships here) → alert → enforce
**Code:** `robothor/engine/tools/verification.py`, wired at
`robothor/engine/tools/dispatch.py::_execute_tool`
**Generic flip/apply/rollback procedure:** [`GUARDRAIL_FLIPS.md`](GUARDRAIL_FLIPS.md)

## What it does

After a side-effectful tool reports success, the dispatcher makes **one**
independent read-back call and asks the environment whether the write actually
landed:

| Tool | Read-back |
|------|-----------|
| `gws_gmail_send`, `gws_gmail_reply` | `gmail.users.messages.get` on the returned message id |
| `gws_calendar_create` | `calendar.events.get` on the returned event id (a `cancelled` event does not count) |
| `create_task` | `dal.get_task` — row exists, title matches |
| `update_task` | `dal.get_task` — the requested fields actually changed |
| `resolve_task` | `dal.get_task` — status actually reads `DONE` |
| `create_person`, `update_person` | `dal.get_person` — row exists, requested fields match |
| `send_notification` | `dal.get_notification` — the row exists |

The verdict is written to the `agent_run_evidence` ledger as
`kind='tool_verify'` (`verified` true/false) or `kind='verify_error'` when the
checker itself failed.

## Why it exists

The existing completion contract only fires when an active *session goal*
exists **and** the final prose matches one of five "task/goal complete"
regexes. It has fired once in its lifetime. The failure class it cannot see is
the one that actually happened here: a run that answered "✅ Payment confirmed"
whose entire tool trace was a single `write_file` to `/tmp`, while the CRM task
it claimed to have handled sat untouched at `TODO`.

Post-conditions do not read the transcript at all. They compare a tool's
asserted effect against durable state, which is the only signal an agent cannot
talk its way past.

## Budget

- **One** extra call per check — never a search, never a list.
- `CHECK_TIMEOUT_SECONDS = 5` per checker, timeout recorded as `verify_error`.
- `MAX_CHECKS_PER_RUN = 20` per `run_id`; further calls pass through unchecked.
- Every failure path is caught. Verification can record a bad verdict; it can
  never fail, block, or slow an agent's real work beyond the budget above.

## Rungs

| Mode | Behavior |
|------|----------|
| `off` | Nothing runs. No read-backs, no rows. |
| `observe` | Read back, write the evidence row, log a warning on failure. The tool result the model sees is **byte-identical** to what it would be with the flag off. |
| `alert` | Observe, plus `notify_guardrail_alert` to the operator on a failed read-back. |
| `enforce` | Observe, plus inject `verification_failed: true` and an actionable `verification_message` **into the tool result**, so the agent learns in-loop that its action did not take effect instead of reporting it as done. Still never fails the call. |

`enforce` is implemented and unit-tested but **not enabled**. It changes what
the model sees mid-run, which is a behavior change, not just bookkeeping.

## Soak criteria before promoting past observe

1. **The mechanism is proven, not merely quiet.** Zero rows is not evidence —
   it is the signature of an inert control (see
   `feedback-probe-dont-trust-silence`). Before reading any verdict, confirm
   `agent_run_evidence` holds `kind='tool_verify'` rows with `verified = true`
   for real sends and CRM writes.
2. **Triage every `verified = false`.** Each one is either a real
   claim-vs-reality gap (the thing this exists to catch) or a checker bug.
   Promoting with unexplained false rows would teach agents to distrust
   correct writes.
3. **`verify_error` rate ~0.** A checker that errors is a bug in this module,
   not an agent failure.
4. **Latency.** Compare mean tool-call duration for the verified tools against
   the pre-flag window. One read-back per write should be in the noise; if it
   is not, shrink `MAX_CHECKS_PER_RUN` before promoting.

```sql
-- verdict counts for the soak window
SELECT kind, verified, count(*)
  FROM agent_run_evidence
 WHERE kind IN ('tool_verify', 'verify_error')
   AND created_at > now() - interval '7 days'
 GROUP BY 1, 2 ORDER BY 1, 2;

-- every unverified write, newest first — triage list
SELECT created_at, run_id, reference, detail
  FROM agent_run_evidence
 WHERE kind = 'tool_verify' AND verified = false
 ORDER BY created_at DESC LIMIT 50;
```

## Probe (do this after deploy — do not trust silence)

Fire a **real** violation rather than waiting for one:

1. Ask an agent to update a CRM task, then verify the read-back path by
   pointing a checker at a row that does not exist:

   ```sql
   -- pick a live task, note its id and status
   SELECT id, status FROM crm_tasks WHERE status = 'TODO' ORDER BY created_at DESC LIMIT 1;
   ```

   ```python
   # on the box, in the engine venv
   import asyncio
   from robothor.engine.tools.dispatch import ToolContext
   from robothor.engine.tools.verification import verify_tool_result

   ctx = ToolContext(agent_id="probe", run_id="<a real agent_runs id>", tenant_id="<tenant>")
   # claim a resolve that never happened: the row still reads TODO
   print(asyncio.run(verify_tool_result(
       "resolve_task", {"id": "<that task id>"}, {"success": True, "id": "<that task id>"}, ctx
   )))
   ```

   Expect: an `agent_run_evidence` row with `kind='tool_verify'`,
   `verified=false`, `detail->>'status' = 'TODO'`, and — in observe — the
   returned dict unchanged.

2. Confirm the positive case too: send a real email through `gws_gmail_send`
   and check for a `verified = true` row referencing that message id. A control
   that only ever records failures is as broken as one that never fires.

## Ordering dependency

The `agent_run_evidence` table is created by the run-verification work
(`agent_run_evidence`: `run_id, step_id, kind, reference, verified, detail,
created_at`). Until that migration is applied, every ledger write is caught and
skipped: verification still computes and logs its verdict, but nothing is
persisted, so **the soak clock does not start until the table exists**. Confirm
with:

```sql
SELECT to_regclass('agent_run_evidence');
```

## Apply / rollback

Follow [`GUARDRAIL_FLIPS.md`](GUARDRAIL_FLIPS.md). The flags live in the
versioned drop-in mirror
(`infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf`); check that
neither is also set in `/etc/robothor/robothor.env`, which would shadow the
drop-in silently. Rollback is `ROBOTHOR_TOOL_VERIFY_ENABLED=0` plus a restart —
the code path returns the tool result untouched before doing anything else.
