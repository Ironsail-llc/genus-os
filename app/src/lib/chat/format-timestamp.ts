/**
 * How a chat message's time reads in its bubble: the time alone today, the
 * weekday within the last six days, the date beyond that, and the year when it
 * is not this one. `locale` is for tests; the browser's own locale otherwise.
 */

const DAY_MS = 24 * 60 * 60 * 1000;

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

export function formatMessageTime(date: Date, now: Date = new Date(), locale?: string): string {
  if (Number.isNaN(date.getTime())) return "";
  const time = date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
  // A clock a little ahead of this one still lands "today", never in the future.
  const daysAgo = Math.round((startOfDay(now) - startOfDay(date)) / DAY_MS);
  if (daysAgo <= 0) return time;
  if (daysAgo < 7) return `${date.toLocaleDateString(locale, { weekday: "short" })} ${time}`;
  const sameYear = date.getFullYear() === now.getFullYear();
  const day = date.toLocaleDateString(locale, {
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
  });
  return `${day} ${time}`;
}

/** The whole date and time, for a tooltip. */
export function fullMessageTime(date: Date, locale?: string): string {
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(locale, { dateStyle: "full", timeStyle: "short" });
}
