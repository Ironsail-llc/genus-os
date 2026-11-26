/**
 * The shapes of a Logs `since` value, on their own: what the app shell needs
 * to read `?v=logs&since=` from the address bar. Kept apart from `./since`
 * (which re-exports them) so every page's first load carries these two
 * patterns and not the calendar check the Observe pages use.
 */

/** ISO-8601, as much of it as this appliance's callers write. */
export const ISO_TIMESTAMP =
  /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?)?(?:Z|[+-]\d{2}:?\d{2})?$/;

/** A relative age in journald's units — the half `--since` has and a column does not. */
export const RELATIVE_AGE = /^\d{1,6}[smhd]$/;

/** What `GET /api/logs?since=` accepts: a relative age or an ISO-8601 stamp. */
export function isLogSince(value: string): boolean {
  return RELATIVE_AGE.test(value) || ISO_TIMESTAMP.test(value);
}
