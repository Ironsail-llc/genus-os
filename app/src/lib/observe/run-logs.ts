/**
 * The Logs search for one run. The engine's structured lines carry
 * `run_id=<id>` (`robothor/engine/log_fields.py`), so the quoted phrase finds
 * that run's lines in the organization's Datadog logs.
 */
export function runLogsQuery(runId: string): string {
  return `"run_id=${runId}"`;
}

/** How far before a run's start its Logs window opens, for clock skew and warm-up lines. */
const LEAD_MS = 5 * 60_000;

/** The Runs page's own default period, for a run whose start is not known. */
export const RUN_LOGS_FALLBACK_SINCE = "7d";

/**
 * The window a run's Logs search opens on: a little before the run started,
 * as an ISO timestamp the bridge reads, so a run from days ago still shows its
 * lines. Without a readable start, the last 7 days.
 */
export function runLogsSince(startedAt: string | null | undefined): string {
  const start = startedAt ? Date.parse(startedAt) : Number.NaN;
  return Number.isNaN(start) ? RUN_LOGS_FALLBACK_SINCE : new Date(start - LEAD_MS).toISOString();
}
