# Interactive request latency and calendar recovery

Status: implementation on `fix/interactive-request-performance`. Not deployed.
The new calendar capability defaults off. No production migration, service restart,
calendar write, or invitation was performed by this implementation work.

## Incident evidence (2026-09-19, America/New_York)

The original `Go` run `57e5e5be-88fe-4763-8497-b9e303a0b15d` started at
19:03:50. It added the guest at approximately 19:05:27, then investigated CLI
help, notification semantics, source code, and a CLI upgrade inside the same
interactive request. At 19:25 the sampled trace had 45 model calls consuming
about 960 seconds, 63 tool calls consuming about 26 seconds, and 2.238 million
input tokens. These are partial-run measurements, not a complete latency breakdown.

At approximately 19:18 it replaced the attendee array, removing the new guest
and resetting an existing accepted RSVP to needsAction. At 19:19:49 it added the
guest again with notification requests enabled. Subsequent readback showed all
five attendees on the meeting. The original run was cancelled externally at
19:30:17. The engine stopped and restarted outside this work. A later status
check also found a separate run explicitly resuming that original request;
cancelling one run does not prevent a separate resume request.

Contributing mechanisms:

- The main agent had create/list/delete calendar tools but no native attendee update.
- It treated integration repair as part of fulfilling a routine user request.
- Repeated near-ineffective compaction retained a large tool/reasoning tail;
  runner and provider preflight could both attempt compaction.
- A successful write did not terminate the task. Notification requested and
  notification delivered were treated as questions requiring more investigation.
- Benchmark reporting omitted suite duration and had no attendee-update state case.

The harness is the project's Python `AgentRunner` plus LiteLLM. It is not an
embedded OpenCode installation. Changing model providers alone would leave these
control-flow and correctness defects in place.

## Implemented behavior

`gws_calendar_add_attendees` reads an event, preserves the complete existing
attendee objects, merges new addresses, patches only the attendee array with
`If-Match` and `sendUpdates=all`, then reads back once. A 412 gets one fresh merge;
a timeout or ambiguous write gets readback, never a blind retry. Existing guests
are included in contact-policy screening because Google may notify them too.
Missing version, incomplete attendees, changed draft details, and cancelled events
refuse mutation. It reports notification *requests*, never guaranteed inbox delivery.

Migration 126 adds requester/tenant/agent-scoped durable operation records. Drafts
store event identity and details; plain `Go` binds only to the immediately preceding
assistant draft's operation ID. The normal tool admission and read-only checks still
apply. The execution marker commits before Google is called. Per-event advisory
locks serialize writers; duplicate confirmations return the stored outcome. A crash
leaves an executing record that is reconciled by reading, never re-sending. Blocked
outcomes create an unassigned repair task requiring human reconciliation.

A confirmed operation uses zero model calls in the runner regression test, stops
after its tool result, and skips planning, compaction, and model verification.
Cancellation signalled while the worker is reading prevents its subsequent write.
A request already sent to Google cannot be recalled; the worker can finish readback
and retain the result. A blocked record with an uncertain write prevents a new operation on that event until
an operator reconciles it. A known refusal before writing permits a new draft.
Do not clear uncertain state by automatically retrying.

Context control memoizes compaction per run, keeps complete tool exchanges together,
bounds the recent tail by tokens, pins the active request, and deterministically
shrinks after unsuccessful summarization. Surviving exchanges retain their reasoning
fields. The existing hard context-fit ceiling remains in force.

Progress callbacks run every 30 seconds without a model request during the loop.
Telegram updates its thinking message. Setup before the loop is not covered by this
new timer. Benchmarks get background priority even when manually triggered. Run
measurements include model calls, input tokens, overlapping tool wall time,
compaction duration, first action, verified completion, and post-completion tools.
Suite duration now reaches the benchmark-results table.

## Validation and rollout

All calendar tests use fake Google transports; durability tests use unique schemas
in `robothor_test` and remove them afterward. They cover duplicate confirmations,
concurrent writers, changed drafts, requester isolation, crash-after-write,
contact refusal, uncertain responses, authorization failures, conflicts, and
cancellation before mutation. The existing benchmark refusal of all real Google
access remains enabled, including reads. It must not be disabled for benchmarking.

1. Review and merge the isolated branch; do not overwrite unrelated browser/desktop
   edits in the primary checkout.
2. Apply packaged migration 126 through the normal migration runner. Keep
   `ROBOTHOR_CALENDAR_OPERATIONS_ENABLED=false` initially.
3. In a separate test deployment, give the test agent the
   `gws_calendar_add_attendees` permission and enable the flag. Use a dedicated
   test calendar and controlled test recipients. Credential support is currently
   an explicit access token or authorized-user refresh credentials from the gws
   credential file/export; service-account credentials are not implemented.
4. Confirm real Google preserves existing RSVPs, rejects stale etags, and requests
   notifications. Exercise an interrupted write and duplicate confirmation. No
   real user meeting is a canary.
5. Enable the capability for the production interactive agent only after those
   checks. Update its instance manifest; `docs/agents/main.yaml` is instance data
   and deliberately not changed by this branch. Restart/reload through the normal
   deployment procedure, coordinating with other active work.
6. Roll back by disabling the flag/removing the tool permission; retain operation
   records for reconciliation. This blocks future admissions, not a write already
   sent to Google. Explicitly interrupt active requests when stopping them is needed.

## Comparative harness evaluation

`bench/interactive/measure_native.py` is an executable, offline operation
microbenchmark. Its checked-in result measures only in-memory merge/verification.
It is **not** a measurement of end-to-end Google, DB, or runner latency.
`bench/interactive/compare.py` compares recorded samples from current, optimized,
minimal, OpenCode, and Pi adapters, refusing mixed model/prompt/tool/machine cohorts.
No OpenCode/Pi end-to-end comparison or live-calendar canary has been run in this
work; adapter integration and those measurements remain rollout work.

Use the same task text, history, model, reasoning settings, tools, fixture server,
and machine for each harness. Run local-model and cloud-model cohorts separately.
Include routine additions, already-present guests, missing permission, timeout after
commit, 412 conflict, duplicate `Go`, expired/changed draft, read-only requests,
cancellation, and long-history compaction. Run at least 30 repetitions per case,
including cold and warm startup separately. Grade actual fixture state (attendee
set, RSVP preservation, unchanged time/link, write count), not the final prose.

Initial gates: no correctness or permission regressions; at most two model calls
for routine operations; no post-completion exploratory calls; p95 harness overhead
under two seconds excluding provider/tool waits; at least 80% reduction in model
calls and input tokens against the same-model baseline. A replacement harness must
beat the optimized runner's p95 by at least 20% and match identity, permissions,
tracing, tenant isolation, cancellation, resumption, and repair behavior. The
comparator's latency gate alone does not approve replacement.

Candidate interfaces verified against upstream documentation:

- [OpenCode embedded SDK](https://opencode.ai/v2/docs/build/sdk/): an in-process
  host with session APIs and plugin customization. Evaluate this before adding a
  separate server hop.
- [Pi SDK](https://pi.dev/docs/latest/sdk): custom tools, in-memory sessions,
  model selection, events, and cancellation.
- [Google Calendar patch](https://developers.google.com/workspace/calendar/api/v3/reference/events/patch):
  attendee arrays are replaced, which is why a read/merge and conditional write are required.

Pin package versions when implementing adapters. Disable default filesystem/shell
tools and global extension discovery; expose only the fixture-backed tools. Supply
only benchmark model credentials, never the production Google identity.
