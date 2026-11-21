/**
 * Dates on room screens. Periods are calendar dates (`2026-11-16`), read and
 * written in UTC so a date never shifts by a day with the reader's zone.
 */

const DAY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
const DAY_YEAR = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const WHEN = new Intl.DateTimeFormat("en-GB", {
  weekday: "short",
  day: "numeric",
  month: "short",
  hour: "numeric",
  minute: "2-digit",
});

function date(value: string | null | undefined): Date | null {
  if (typeof value !== "string" || !value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "16 Nov – 22 Nov 2026", "16 Nov 2026" for one day, or "" when unknown. */
export function formatPeriod(start: string | null | undefined, end: string | null | undefined): string {
  const from = date(start);
  const to = date(end);
  if (!from && !to) return "";
  if (from && to) {
    if (from.getTime() === to.getTime()) return DAY_YEAR.format(from);
    const sameYear = from.getUTCFullYear() === to.getUTCFullYear();
    return `${(sameYear ? DAY : DAY_YEAR).format(from)} – ${DAY_YEAR.format(to)}`;
  }
  return DAY_YEAR.format((from ?? to)!);
}

/** A moment, in the reader's zone: "Mon 23 Nov, 08:00". "" when unknown. */
export function formatWhen(value: string | null | undefined): string {
  const d = date(value);
  return d ? WHEN.format(d) : "";
}
