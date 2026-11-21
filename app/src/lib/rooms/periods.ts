/**
 * Report periods, worked out in the browser so a stored edition can be shown
 * the moment its period is picked, and a future period refused before any
 * request. Dates are calendar dates (`2026-11-16`), handled in UTC so a date
 * never shifts with the reader's zone; "today" is read in the workspace's
 * own time zone. The engine resolves periods again on its side and decides.
 *
 * A week runs Monday to Sunday; a month from its first day to its last.
 */
import type { Edition } from "./types";

export type Grain = "day" | "week" | "month" | "custom";
export type FixedGrain = Exclude<Grain, "custom">;

export interface PeriodRange {
  grain: Grain;
  start: string;
  end: string;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function parse(value: string): Date | null {
  if (!ISO_DATE.test(value)) return null;
  const d = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

function iso(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function addDays(d: Date, days: number): Date {
  return new Date(d.getTime() + days * 86_400_000);
}

/** Today's date in `tz` (an IANA name); UTC when the zone is unknown. */
export function todayIn(tz: string | undefined | null, now: Date = new Date()): string {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: tz || "UTC",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(now);
  } catch {
    return iso(now);
  }
}

/** The day, week or month that contains `anchor`. */
export function resolvePeriod(grain: FixedGrain, anchor: string): PeriodRange {
  const d = parse(anchor) ?? parse(iso(new Date()))!;
  if (grain === "day") return { grain, start: iso(d), end: iso(d) };
  if (grain === "week") {
    const sinceMonday = (d.getUTCDay() + 6) % 7;
    const start = addDays(d, -sinceMonday);
    return { grain, start: iso(start), end: iso(addDays(start, 6)) };
  }
  const start = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1));
  const end = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0));
  return { grain, start: iso(start), end: iso(end) };
}

/** A custom range, or null when either end is missing or it runs backwards. */
export function customPeriod(start: string, end: string): PeriodRange | null {
  const from = parse(start);
  const to = parse(end);
  if (!from || !to || from.getTime() > to.getTime()) return null;
  return { grain: "custom", start, end };
}

/**
 * A period that has not started yet, or a custom range that ends after today:
 * there is nothing to report on, so it is refused before any request.
 */
export function isFuturePeriod(range: PeriodRange, today: string): boolean {
  if (range.start > today) return true;
  return range.grain === "custom" && range.end > today;
}

/**
 * A period whose last day is today or later: it has not ended, so there is
 * nothing complete to report on. The engine refuses exactly these
 * (`robothor/rooms/periods.py`, `resolve_period`), in the workspace's zone.
 */
export function isUnfinishedPeriod(range: PeriodRange, today: string): boolean {
  return range.end >= today;
}

/** The day before `today` (both `YYYY-MM-DD`): the latest date that has ended. */
export function dayBefore(today: string): string {
  const d = parse(today);
  return d ? iso(addDays(d, -1)) : today;
}

/** The latest whole day, week or month before the one `today` is in. */
export function lastCompletePeriod(grain: FixedGrain, today: string): PeriodRange {
  const current = resolvePeriod(grain, today);
  return resolvePeriod(grain, iso(addDays(parse(current.start)!, -1)));
}

/** The published, non-preview edition for exactly this period, or null. */
export function findStoredEdition(editions: readonly Edition[], range: PeriodRange): Edition | null {
  return (
    editions.find(
      (e) =>
        e.status === "published" &&
        e.preview !== true &&
        e.periodStart === range.start &&
        e.periodEnd === range.end &&
        // A day and a one-day custom range are the same period; any other
        // grain must match. An edition with no grain is matched on its dates.
        (e.grain == null ||
          e.grain === range.grain ||
          (range.start === range.end && (e.grain === "day" || e.grain === "custom"))),
    ) ?? null
  );
}
