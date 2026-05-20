# `crm_task_history.metadata.kind` — Canonical Enum

Every row in `crm_task_history` may carry a JSONB `metadata` field. By
convention, that JSON object includes a `kind` discriminator naming the
semantic event the row records. The forward planner (`thread_planner.py`)
reads `kind` to infer what happened last on a thread; observability
dashboards roll up event types by this value. Both depend on the set
staying small, stable, and predictable.

This document is the single source of truth. Migration
`crm/migrations/067_task_history_kind_schema.sql` encodes the same set as
a Postgres CHECK constraint on new rows. The meta-test
`robothor/engine/tests/test_history_kind_docs.py` parses this table and
fails CI if a code path writes a `kind` value that isn't listed here, or
if a required kind is removed.

## The enum

| Kind | Who writes it | Extra metadata fields | Purpose |
|---|---|---|---|
| `plan` | `dal.set_next_action` (driven by `thread_planner.apply_plan` action="execute") | `agent` (str), `planner_version` (int, optional) | The planner chose a concrete next step and wrote it to `next_action` / `next_action_agent`. |
| `ask` | `dal.set_question` (driven by `thread_planner.apply_plan` action="ask") | `question` (str) | The planner escalated to the operator with a concrete question. Status flips to REVIEW; `escalation_count` bumps. |
| `answer` | `dal.answer_question` (Phase 4 — operator answer via Helm) | `answer` (str), `channel` (str, e.g. `helm`), `advance_to` (str, optional next status) | The operator answered a pending question. Clears `question_for_operator`, resets `escalation_count`. |
| `email_sent` | Email-responder tool (when it sends a reply or chase) | `to` (str, optional), `subject` (str, optional) | An outbound email was dispatched on this thread. The planner uses the row's age to decide whether to chase again. |
| `calendar_offer_received` | Calendar ingest tool (when a counter-party proposes a meeting) | `from` (str, optional), `slots` (list, optional) | A meeting-link or slot proposal arrived. Multiple occurrences within 7 days trigger the planner's "drop vendor or hand off?" question when the objective vetoes meetings. |
| `todo_promoted` | `robothor.engine.todo_promotion.promote_todo_to_subtask` (Phase 3) | `content_hash` (str), `from_run_id` (str), `item_count` (int) | An unfinished `todo_write` item from a worker run was promoted to a real CRM subtask under the parent thread. Idempotency key is the content hash. |
| `acceptance` | `runner._run_acceptance_block` (existing) | `passed` (bool), `ran` (int), `failures` (list, optional) | Result of running the parent task's fenced ` ```accept ``` ` block. The worker decides whether to advance based on `passed`. |

## Adding a new kind

1. Add a row to the table above.
2. If the migration's CHECK constraint is already `VALIDATE`d, ship a
   follow-up migration that drops + recreates the constraint with the new
   value included.
3. Update `robothor/engine/tests/test_history_kind_docs.py` — if the new
   kind is mandatory (read by the planner or dashboards), add it to
   `test_documented_kinds_minimum_set`.
4. Update the producer of the new kind to call `_record_transition` with
   `metadata={"kind": "<new-kind>", ...}`.

## Anti-patterns

- **Free-form `kind` strings.** Always pick from this table. The
  validator in 067 ships as `NOT VALID` so existing rows aren't broken,
  but new writes are checked.
- **Re-using a kind for unrelated meanings.** If the meaning of `plan`
  drifts (e.g. it now sometimes records sub-agent dispatch), add a new
  kind instead of overloading.
- **Putting extra-large blobs in `metadata`.** The column is JSONB —
  feasible, but the planner reads many rows per beat. Keep payloads
  small; large data goes in the task `body` or a separate table.
- **Recording every internal step.** Only events the planner or the
  operator care about belong here. Tool call traces live in
  `agent_run_steps`, not history.
