# Interactive request performance

This change improves the existing Python/LiteLLM engine. It does not replace the
harness or change models. The calendar operation is opt-in through
`ROBOTHOR_CALENDAR_OPERATIONS_ENABLED`; migration 126 and the agent's explicit
`gws_calendar_add_attendees` permission are required.

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

The operation reads the event, preserves the complete existing attendee objects,
merges the new addresses, conditionally patches with `If-Match` and
`sendUpdates=all`, and reads back once. A version conflict gets one fresh merge.
An ambiguous write gets a read, never a blind retry. Notification requested is
reported separately from inbox delivery, which this operation cannot verify.

Operation state commits before the external write. Per-event advisory locks
serialize updates across processes. Duplicate confirmations return the recorded
outcome. An interrupted write is reconciled without re-sending. An uncertain
operation blocks both new requests and older drafts for that event. Known refusals
before writing permit a new draft. Repair tasks are unassigned and require human
reconciliation; filing them happens after releasing the calendar lock and database
connection, avoiding nested pool acquisition during concurrent failures.

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
2. Apply migration 126 with the normal packaged migration runner. Do not manually
   mark migrations applied. Keep the feature off until deployment checks pass.
3. Add `gws_calendar_add_attendees` to the intended interactive agent's instance
   manifest and enable `ROBOTHOR_CALENDAR_OPERATIONS_ENABLED`. Native authentication
   supports an explicit access token, an explicit credentials file, or the gws
   credential directory/export. Credentials are never returned to the agent.
4. Check the deployed revision, health, registered tool, flag, and operation table.
   Confirm a draft and repeat its confirmation; require one write and preserved RSVP.
5. Disable the flag or remove the permission to stop future admissions. Retain
   operation records. Roll back the engine artifact independently; the additive
   table is compatible with the older engine. A write already in flight is not
   reversed by disabling the flag.

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
