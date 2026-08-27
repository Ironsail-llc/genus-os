-- 110: record WHY a tool failed, not only THAT it failed.
--
-- 2026-08-27: 68.4% of tool failures over 7 days (976 of 1,426) carried
-- error_type='unknown', and joining agent_tool_events.step_id to
-- agent_run_steps.error_message recovered a reason for ZERO of them. The
-- degradation detector therefore paged "Tool degradation: create_task" on
-- failures whose cause was written down nowhere -- undiagnosable by an
-- operator and by an investigating agent alike.
--
-- Bounded at the write site (tracking.MAX_TOOL_ERROR_CHARS) so one
-- pathological payload cannot bloat the table. Nullable and written only on
-- failure, so successful calls carry no extra bytes.

ALTER TABLE agent_tool_events
    ADD COLUMN IF NOT EXISTS error_message TEXT;

COMMENT ON COLUMN agent_tool_events.error_message IS
    'Truncated failure reason. NULL on success. Added 2026-08-27 because 68% '
    'of tool failures classified as unknown with no recoverable cause.';
