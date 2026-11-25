/**
 * The Logs search for one run. The engine's structured lines carry
 * `run_id=<id>` (`robothor/engine/log_fields.py`), so the quoted phrase finds
 * that run's lines in the organization's Datadog logs.
 */
export function runLogsQuery(runId: string): string {
  return `"run_id=${runId}"`;
}
