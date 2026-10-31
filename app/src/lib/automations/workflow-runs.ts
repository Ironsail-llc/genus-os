/**
 * Workflow runs as the bridge reports them, and the pure helpers the runs list
 * and the run-detail drawer share.
 *
 * `GET /api/workflows/{id}/runs` lists a workflow's runs; `GET
 * /api/workflows/{id}/runs/{run_id}` adds each step's tool name, input, output
 * and error. Both are operator-only. A step's input or output larger than the
 * engine's cap is stored as `{"_truncated": true, "original_bytes": N,
 * "preview": "<the start of its JSON>"}`.
 */

const BRIDGE = "/api/bridge";

/** Both routes are operator-only; a 403 says so rather than reading as a fault. */
export const OPERATOR_ONLY = "Operator access required.";

export type WorkflowRun = {
  id: string;
  workflow_id: string;
  status?: string | null;
  trigger_type?: string | null;
  steps_total?: number | null;
  steps_completed?: number | null;
  steps_failed?: number | null;
  steps_skipped?: number | null;
  duration_ms?: number | null;
  started_at?: string | null;
  completed_at?: string | null;
  error_message?: string | null;
};

export type WorkflowRunStep = {
  step_id: string;
  step_type?: string | null;
  status?: string | null;
  agent_id?: string | null;
  agent_run_id?: string | null;
  tool_name?: string | null;
  tool_input?: unknown;
  tool_output?: unknown;
  condition_branch?: string | null;
  output_text?: string | null;
  error_message?: string | null;
  duration_ms?: number | null;
  started_at?: string | null;
  completed_at?: string | null;
};

export type WorkflowRunDetail = WorkflowRun & {
  trigger_detail?: string | null;
  steps: WorkflowRunStep[];
  /** A scheduled plugin job's returned value; it records no steps. */
  result?: unknown;
  /** True when the engine cut `result` to its cap; `result` is then the start of its JSON. */
  result_truncated?: boolean;
};

/** `workflow_runs.trigger_type` of a scheduled plugin job's run. */
export const PLUGIN_JOB = "plugin_job";

export function runsUrl(workflowId: string): string {
  return `${BRIDGE}/api/workflows/${encodeURIComponent(workflowId)}/runs`;
}

export function runUrl(workflowId: string, runId: string): string {
  return `${runsUrl(workflowId)}/${encodeURIComponent(runId)}`;
}

type Counts = Pick<WorkflowRun, "steps_completed" | "steps_failed" | "steps_skipped">;

/** `2 ok, 1 failed[, 1 skipped]`, or null when the run never recorded counts. */
export function stepTally(run: Counts): string | null {
  const ok = run.steps_completed;
  const failed = run.steps_failed;
  if (typeof ok !== "number" && typeof failed !== "number") return null;
  const parts = [`${ok ?? 0} ok`, `${failed ?? 0} failed`];
  if (typeof run.steps_skipped === "number" && run.steps_skipped > 0) {
    parts.push(`${run.steps_skipped} skipped`);
  }
  return parts.join(", ");
}

/** The engine's truncation marker, read; null for any other value. */
export function truncationOf(value: unknown): { originalBytes: number; preview: string } | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const marker = value as Record<string, unknown>;
  if (marker._truncated !== true) return null;
  return {
    originalBytes: typeof marker.original_bytes === "number" ? marker.original_bytes : 0,
    preview: typeof marker.preview === "string" ? marker.preview : "",
  };
}

/** A recorded payload, indented; a string that is not JSON is shown as it is. */
export function prettyJson(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}
