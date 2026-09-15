/**
 * The time bounds the Observe pages are allowed to send, in the app's copy of
 * the bridge's own patterns.
 *
 * Deliberately a mirror, not a guess. `crm/bridge/routers/_params.py` holds
 * `ISO_TIMESTAMP_PATTERN` and `routers/logs.py` adds the relative age on top of
 * it (`30m`, `1h`, `7d` — journald understands those and a timestamp column
 * does not). Both refuse **before** the query runs or the process is spawned,
 * with a flat `{"detail": …}` 422.
 *
 * Checking here as well is not a substitute for that — it is so that a typo in
 * a free-text field is a red line under the field rather than a round trip that
 * comes back as a banner somewhere else on the screen, and so that the CSV
 * export link, which is a plain `<a href download>` the browser follows
 * without this page ever seeing the reply, is never built out of a value the
 * bridge is about to refuse.
 *
 * Anchored with `^…$` and used on single-line input values. That is safe in
 * JavaScript, where `$` does not also match before a trailing newline the way
 * Python's does — which is the bug `fullmatch` was brought in to close on the
 * bridge.
 */

/** ISO-8601, as much of it as this appliance's callers write. */
export const ISO_TIMESTAMP =
  /^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?(?:Z|[+-]\d{2}:?\d{2})?$/;

/** A relative age in journald's units — the half `--since` has and a column does not. */
export const RELATIVE_AGE = /^\d{1,6}[smhd]$/;

export function isIsoTimestamp(value: string): boolean {
  return ISO_TIMESTAMP.test(value);
}

/** What `GET /api/logs?since=` accepts: a relative age or an ISO-8601 stamp. */
export function isLogSince(value: string): boolean {
  return RELATIVE_AGE.test(value) || ISO_TIMESTAMP.test(value);
}
