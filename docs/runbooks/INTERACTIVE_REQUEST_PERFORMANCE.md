# Interactive request performance

This runbook covers how the existing Python/LiteLLM engine keeps routine
interactive requests — "add Sam to the 3pm", "move my sync to Thursday" — short
and bounded. It does not replace the harness or change models.

## What was wrong

An incident investigation found that a routine attendee addition continued after
its first successful write. A partial trace contained 45 model calls taking about
960 seconds, versus 63 tool calls taking about 26 seconds, and 2.238 million input
tokens. The request drifted into CLI investigation, upgrades, and notification
troubleshooting. Replacing an attendee array during that investigation also reset
an existing RSVP. Repeated ineffective compaction amplified the delay.

The fix targets those causes: native calendar edits that preserve existing guests,
an explicit completion point, bounded recovery, and effective context reduction.

## Calendar edits are direct writes

`gws_calendar_update`, `gws_calendar_add_attendees` and `gws_calendar_respond`
write directly — no draft, no "reply Go to confirm", no feature setting. Editing a
meeting is ordinary assistant work, not a payment or an irreversible external
action, so it carries no approval gate. See [Tools](../TOOLS.md#whose-calendar).

An earlier release put attendee additions behind a draft and a `Calendar
operation: <id>` marker that a bare "yes"/"Go" in the next turn confirmed. That
flow, its `ROBOTHOR_CALENDAR_OPERATIONS_ENABLED` setting and the confirmation
binding are removed: a bystander's "yes" in a group chat could fire a write.
Setting the variable now has no effect.

Each edit reads the event, keeps the complete existing attendee objects (RSVP,
optional flag, comment), merges what was asked for, conditionally patches with
`If-Match` and the configured `sendUpdates` (`ROBOTHOR_CALENDAR_SEND_UPDATES`,
default `all`), and reads back once. A version conflict gets one fresh read and
merge. An ambiguous write gets a read, never a blind retry. Notification
requested is reported separately from inbox delivery, which the tool cannot
verify. Start and end times are compared as **instants**, not as dicts: Google
normalises an echoed `timeZone` into an equivalent offset.

An attendee who has **declined** is reported as declined, not as "already
invited". The result states whether the event **recurs** (`scope: "series"` or
`scope: "instance"`). Task cancellation and the operator's interrupt flag are
checked immediately before the write; a request already sent to Google cannot be
recalled, but its result can still be read.

### Records left by the retired flow

Rows the old flow left in `calendar_operations` with `status = 'executing'` are
still settled by the calendar recovery worker from evidence: the event is read
back, never re-written. An unchanged event version with none of the requested
attendees present is proof the write never landed, and the record clears itself.
The table and migrations 126/127 are kept; nothing new is written to them.

## Context control and progress

Context control shares compaction state across the loop and provider preflight,
keeps tool exchanges intact, bounds the recent tail by tokens, pins the current
request, and deterministically shrinks when summarization is ineffective. Reasoning
fields remain on surviving exchanges for provider-specific replay handling.

Every 30 seconds during the loop, progress includes elapsed time, completed tool
count, and the observed stage. This uses lifecycle events, not another model call.
Telegram updates its thinking message. Startup before entering the loop is not
covered by this timer. Benchmark runs use background priority. Benchmark records
include duration, model calls, input tokens, overlapping tool wall time,
compaction cost, verified completion, and post-completion tool calls.

## Reproducible checks

The offline attendee merge/verification microbenchmark never touches Google:

```bash
python -m bench.interactive.measure_native
```

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

1. Grant `gws_calendar_update`, `gws_calendar_respond` (and, for older manifests,
   `gws_calendar_add_attendees`) in the interactive agent's instance manifest.
   Removing a grant takes effect on the next run and needs no restart.
2. Native authentication supports an explicit access token, an explicit
   credentials file, or the gws credential directory/export.
   `GOOGLE_WORKSPACE_CLI_TOKEN` is an access token and expires about an hour after
   it is minted; a rejected token is refreshed once from the configured
   credentials, so configure a credentials source even when a token is supplied.
   Credentials are never returned to the agent.
3. Remove any instance drop-in that set `ROBOTHOR_CALENDAR_OPERATIONS_ENABLED`;
   it is no longer read.

## Harness decision

Retain the current engine. No evidence currently justifies the risk of replacing
identity, permissions, persistence, cancellation, and delivery with a different
harness. OpenCode and Pi are valid candidates for a later controlled comparison,
not measured winners.

`bench/interactive/compare.py` can compare current, optimized, minimal, OpenCode,
and Pi records. It rejects mixed model, reasoning, startup, prompt, tool, and
machine cohorts. At least 30 repetitions per case and independent state assertions
are required. Local and cloud models are separate cohorts. Replacement requires
at least a 20% p95 improvement over the optimized engine and equivalent correctness,
permissions, isolation, interruption, recovery, and upgrade behavior.

References: [OpenCode SDK](https://opencode.ai/v2/docs/build/sdk/),
[Pi SDK](https://pi.dev/docs/latest/sdk),
[Google event patch semantics](https://developers.google.com/workspace/calendar/api/v3/reference/events/patch),
and [Workspace CLI authentication](https://github.com/googleworkspace/cli).
