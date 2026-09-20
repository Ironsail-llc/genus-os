# Interactive request performance

This change improves the existing Python/LiteLLM engine. It does not replace the
harness or change models. The calendar operation is opt-in through
`ROBOTHOR_CALENDAR_OPERATIONS_ENABLED`; migrations 126 and 127 and the agent's
explicit `gws_calendar_add_attendees` permission are required.

`ROBOTHOR_CALENDAR_OPERATIONS_ENABLED` is **restart-required**. Settings are read
once per process, so changing the variable — including turning the feature off —
takes effect only after `systemctl restart robothor-engine`.

## What was wrong

An incident investigation found that a routine attendee addition continued after
its first successful write. A partial trace contained 45 model calls taking about
960 seconds, versus 63 tool calls taking about 26 seconds, and 2.238 million input
tokens. The request drifted into CLI investigation, upgrades, and notification
troubleshooting. Replacing an attendee array during that investigation also reset
an existing RSVP. Repeated ineffective compaction amplified the delay.

The fix targets those causes: a native operation, durable confirmation binding,
bounded recovery, an explicit completion point, and effective context reduction.
Previous meetings are not modified or repaired by deploying it.

## Behavior

A requested calendar draft records the exact event and proposed attendees. A plain
`Go` confirms only the immediately preceding operation. The stored arguments supply
the tool call; no model call, planner, compactor, or model verifier is needed for
that confirmed operation. The existing identity, permission, read-only, contact-policy,
hook, and benchmark-isolation gates remain in place.

A confirmation binds only when **all** of the following hold. Each one is a gate
the round-1 review found missing, and together they are why a bare "yes" cannot
become a replay:

* the stored operation is still a **draft** — `completed`, `blocked` and
  `executing` are answers already given, and re-running one is a replay;
* the operation belongs to **this** tenant, user and agent, so a bystander in a
  group chat (whose identity is `telegram:{chat_id}`) cannot confirm someone
  else's draft;
* the turn is not read-only — plan mode owes the operator a plan, not a write;
* the operation id is consumed on use, so a second loop iteration reaches the
  model rather than replaying the write.

Only a live draft may carry the `Calendar operation: <id>` marker. The terminal
reply of a finished operation never carries it: a marker is an offer to confirm,
and offering "Go" against an outcome is what made the trap permanent.

Every calendar result — the draft, the confirmation and each error — names the
calendar it applies to (`{"kind": "operator"|"own"|"other", "id": ...}`), and the
deterministic reply on the confirmed path says it in words. The model verifier
that would otherwise catch a wrong-calendar claim is deliberately off on that
path, so the sentence is produced by the engine instead.

The operation reads the event, preserves the complete existing attendee objects,
merges the new addresses, conditionally patches with `If-Match` and
`sendUpdates=all`, and reads back once. A version conflict gets one fresh merge.
An ambiguous write gets a read, never a blind retry. Notification requested is
reported separately from inbox delivery, which this operation cannot verify.
Start and end times are compared as **instants**, not as dicts: Google normalises
an echoed `timeZone` into an equivalent offset, and comparing representations
turned writes that had succeeded into blocked operations.

An attendee who has **declined** is reported as declined, not as "already
invited". Re-adding a declined guest is a no-op at Google — they stay declined
and no new invitation is sent — so the result says so and asks the operator how
to proceed.

The result states whether the event **recurs**: `scope: "series"` for a master
(the change and every notification cover all occurrences) or `scope: "instance"`
for a single occurrence. The advisory lock is keyed on the **series**, derived
from Google's `<master id>_<instance timestamp>` naming, so a master and one of
its own occurrences cannot be written concurrently by two engines. The
uncertainty barrier is series-wide for the same reason.

Operation state commits before the external write, together with the event
version (`etag`) observed immediately before it. Per-series advisory locks
serialize updates across processes. Duplicate confirmations return the recorded
outcome. An interrupted write is reconciled without re-sending. Known refusals
before writing permit a new draft. Repair tasks are unassigned and require human
reconciliation; filing them happens after releasing the calendar lock and database
connection, avoiding nested pool acquisition during concurrent failures.

### The uncertainty barrier, and how it clears

An operation whose outcome is unknown refuses every later draft and every direct
write for that series, so a second invitation cannot be sent on top of a first
one that may have landed. A barrier has to have a way out, and there are three:

1. **It clears itself when the evidence is conclusive.** On reconciliation the
   event is read back. If its version is unchanged from the recorded pre-write
   `etag` and none of the requested attendees are present, the request never
   reached Google: the operation records `invitations_requested: false` and stops
   blocking. Nothing is retried; the operator prepares a new draft.
2. **The repair task clears it.** For a genuinely uncertain outcome, the
   unassigned repair task is the record. After a human confirms in Google
   Calendar what actually happened, release the barrier with:

   ```sql
   -- Only after verifying the meeting in Google Calendar.
   UPDATE calendar_operations
      SET result = coalesce(result, '{}'::jsonb) || '{"invitations_requested": false}'::jsonb,
          updated_at = now()
    WHERE id = '<operation id from the reply or the repair task>';
   ```

   A row still stuck in `status = 'executing'` (the process died mid-write) is
   settled by confirming that operation once more, which runs the reconciliation
   in step 1 rather than writing.
3. **It is never armed with nobody watching.** If the repair task could not be
   filed, the operation records `barrier_released: true` and does *not* block
   later changes — a barrier whose only clearing path is a task that does not
   exist would freeze the meeting for good. The operator's reply says plainly
   that no repair task was filed and that the meeting should be checked before
   retrying.

Task cancellation and the operator's interrupt flag are checked before writing.
A request already sent to Google cannot be recalled; its result can still be read
and recorded. Stopping a run does not authorize a separate resume.

Context control shares compaction state across the loop and provider preflight,
keeps tool exchanges intact, bounds the recent tail by tokens, pins the current
request, and deterministically shrinks when summarization is ineffective. Reasoning
fields remain on surviving exchanges for provider-specific replay handling.

Every 30 seconds during the loop, progress includes elapsed time, completed tool
count, and the observed stage. This uses lifecycle events, not another model call.
Telegram updates its thinking message. Startup before entering the loop is not
covered by this timer. Benchmark runs use background priority. Benchmark records
now include duration, model calls, input tokens, overlapping tool wall time,
compaction cost, verified completion, and post-completion tool calls.

## Reproducible checks

Calendar integration tests use a private schema in the configured test database,
never the production schema. They are marked `integration` and run in the existing
CI integration lane using `ROBOTHOR_TEST_DB_DSN`. Missing integration infrastructure
fails those checks instead of silently skipping them.

Run the actual engine benchmark from a source checkout:

```bash
python -m bench.interactive.run_runner --samples 30 --output /tmp/runner-benchmark.json
```

It exercises `AgentRunner`, tool admission, and PostgreSQL operation persistence.
Google and the model are deterministic fixtures; token counts are synthetic and
must not be presented as real provider billing. Both paths receive an equal warmup.
The normal loop is compared with the confirmed-operation path in the same checkout,
not with an unrelated historical production session.

The measured 30-repeat fixture run recorded:

| Measure | Normal loop | Confirmed operation |
|---|---:|---:|
| Model calls | 2 | 0 |
| Total p95 | 215 ms | 204 ms |
| Harness overhead p95 | 197 ms | 189 ms |
| State checks | 30/30 | 30/30 |

The large expected live benefit comes from eliminating unnecessary model waits;
these fixture timings do not measure that benefit. They show that the harness
itself stays below the two-second overhead gate. This is not a concurrency/load
capacity certification.

An explicitly authorized live Google test also passed: verified attendee addition,
existing RSVP/time preservation, only the two controlled participants, and no
notification on a duplicate call. The native operation took 1,036 ms. The new test
event was removed without sending a cancellation notice. No existing meeting was
changed. Google accepted the notification request; inbox delivery was not measured.

The optional live check requires an explicit recipient and action flag:

```bash
python -m bench.interactive.live_calendar_check --recipient operator@example.com --output /tmp/calendar-check.json --send-test-invitation
```

This creates only a new disposable event in the authenticated account's primary
calendar. It uses the configured operator tenant for contact-policy screening.
Never run it against a user meeting. Its private receipt includes event identity;
do not commit that receipt. Ordinary agent benchmarks remain forbidden from
accessing real Google accounts, including reads.

## Release and rollback

1. Run lint, type checking, unit and database integration checks. Preserve unrelated
   changes in other worktrees and the existing deployed feature set.
2. Apply migrations 126 and 127 with the normal packaged migration runner. Do not
   manually mark migrations applied. Keep the feature off until deployment checks
   pass.
3. Add `gws_calendar_add_attendees` to the intended interactive agent's instance
   manifest, set `ROBOTHOR_CALENDAR_OPERATIONS_ENABLED`, and **restart the engine**
   — the flag is process-cached. Native authentication supports an explicit access
   token, an explicit credentials file, or the gws credential directory/export.
   `GOOGLE_WORKSPACE_CLI_TOKEN` is an access token and expires about an hour after
   it is minted; a rejected token is refreshed once from the configured credentials
   rather than failing the feature, so configure a credentials source even when a
   token is supplied. Credentials are never returned to the agent.
4. Check the deployed revision, health, registered tool, flag, and operation table.
   Confirm a draft and repeat its confirmation; require one write and preserved RSVP.
5. Disable the flag (and restart the engine) or remove the permission to stop
   future admissions. Retain operation records. Roll back the engine artifact
   independently; the additive table and column are compatible with the older
   engine. A write already in flight is not reversed by disabling the flag.

Removing the permission takes effect on the next run and needs no restart, so it
is the faster of the two kill switches.

## Harness decision

Retain the current engine for this release. No evidence currently justifies the
risk of replacing identity, permissions, persistence, cancellation, and delivery
with a different harness. OpenCode and Pi are valid candidates for a later
controlled comparison, not measured winners.

`bench/interactive/compare.py` can compare current, optimized, minimal, OpenCode,
and Pi records. It rejects mixed model, reasoning, startup, prompt, tool, and
machine cohorts. At least 30 repetitions per case and independent state assertions
are required. Local and cloud models are separate cohorts. Replacement requires
at least a 20% p95 improvement over the optimized engine and equivalent correctness,
permissions, isolation, interruption, recovery, and upgrade behavior. No OpenCode
or Pi adapter timings have been measured in this change.

References: [OpenCode SDK](https://opencode.ai/v2/docs/build/sdk/),
[Pi SDK](https://pi.dev/docs/latest/sdk),
[Google event patch semantics](https://developers.google.com/workspace/calendar/api/v3/reference/events/patch),
and [Workspace CLI authentication](https://github.com/googleworkspace/cli).
