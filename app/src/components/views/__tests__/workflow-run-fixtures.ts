/**
 * Fixture JSON in the shape the bridge returns for a workflow's runs
 * (`GET /api/workflows/{id}/runs`) and for one run's detail
 * (`GET /api/workflows/{id}/runs/{run_id}`). Generic data only.
 */

export const WORKFLOW_ID = "example-pass";

export const OK_RUN_ID = "11111111-1111-4111-8111-111111111111";
export const FAILED_RUN_ID = "22222222-2222-4222-8222-222222222222";

export const RUNS = [
  {
    id: FAILED_RUN_ID,
    workflow_id: WORKFLOW_ID,
    status: "failed",
    trigger_type: "cron",
    steps_total: 3,
    steps_completed: 1,
    steps_failed: 1,
    steps_skipped: 1,
    duration_ms: 4200,
    started_at: "2026-06-15T11:00:00+00:00",
    completed_at: "2026-06-15T11:00:04.200000+00:00",
    error_message: "Step 'update_record' failed",
  },
  {
    id: OK_RUN_ID,
    workflow_id: WORKFLOW_ID,
    status: "completed",
    trigger_type: "manual",
    steps_total: 2,
    steps_completed: 2,
    steps_failed: 0,
    steps_skipped: 0,
    duration_ms: 850,
    started_at: "2026-06-15T09:00:00+00:00",
    completed_at: "2026-06-15T09:00:00.850000+00:00",
    error_message: null,
  },
];

export const FAILED_RUN_DETAIL = {
  ...RUNS[0],
  trigger_detail: null,
  steps: [
    {
      step_id: "read_record",
      step_type: "tool",
      status: "completed",
      agent_id: null,
      agent_run_id: null,
      tool_name: "read_record",
      tool_input: { record_id: "rec-42" },
      tool_output: { id: "rec-42", owner: "Alice", state: "open" },
      condition_branch: null,
      output_text: null,
      error_message: null,
      duration_ms: 120,
      started_at: "2026-06-15T11:00:00+00:00",
      completed_at: "2026-06-15T11:00:00.120000+00:00",
    },
    {
      step_id: "update_record",
      step_type: "tool",
      status: "failed",
      agent_id: null,
      agent_run_id: null,
      tool_name: "update_record",
      tool_input: { record_id: "rec-42", state: "closed" },
      tool_output: { error: "write refused: the record is read-only" },
      condition_branch: null,
      output_text: null,
      error_message: "write refused: the record is read-only",
      duration_ms: 300,
      started_at: "2026-06-15T11:00:00.200000+00:00",
      completed_at: "2026-06-15T11:00:00.500000+00:00",
    },
    {
      step_id: "export_rows",
      step_type: "tool",
      status: "skipped",
      agent_id: null,
      agent_run_id: null,
      tool_name: "export_rows",
      tool_input: {
        _truncated: true,
        original_bytes: 70000,
        preview: '{"rows": [{"id": 1}, {"id": 2',
      },
      tool_output: null,
      condition_branch: null,
      output_text: null,
      error_message: null,
      duration_ms: null,
      started_at: null,
      completed_at: null,
    },
  ],
};
