/** The date part of an ISO timestamp, or "" when there is none. */
export function isoDate(value?: string | null): string {
  return value ? value.slice(0, 10) : "";
}
