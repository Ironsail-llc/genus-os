/**
 * Who the welcome canvas greets, and at what time of day.
 *
 * Both come from the person looking at it. The server's clock is UTC in a
 * container, and the configured owner name is a deployment-wide fallback whose
 * unset default is not a name at all.
 */

export interface LocalMoment {
  /** 0-23 in the viewer's time zone. */
  hour: number;
  dayOfWeek: string;
  /** "November 6, 2026" in the viewer's time zone. */
  date: string;
}

/** The unset default of the configured owner name: a word, not a name. */
const NO_NAME = "there";

function usableZone(timeZone: string | null | undefined): string | undefined {
  if (!timeZone) return undefined;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone });
    return timeZone;
  } catch {
    return undefined;
  }
}

/** The hour, weekday and date at `now` in `timeZone`, or on the server's clock without one. */
export function localMoment(now: Date, timeZone?: string | null): LocalMoment {
  const zone = usableZone(timeZone);
  const hourText = new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    hourCycle: "h23",
    timeZone: zone,
  }).format(now);
  return {
    hour: Number.parseInt(hourText, 10) % 24,
    dayOfWeek: now.toLocaleDateString("en-US", { weekday: "long", timeZone: zone }),
    date: now.toLocaleDateString("en-US", {
      month: "long",
      day: "numeric",
      year: "numeric",
      timeZone: zone,
    }),
  };
}

/** "Good afternoon, Alice", or just "Good afternoon" when there is no name to use. */
export function greetingFor(hour: number, name?: string | null): string {
  let phrase: string;
  if (hour >= 6 && hour < 12) phrase = "Good morning";
  else if (hour >= 12 && hour < 17) phrase = "Good afternoon";
  else if (hour >= 17 && hour < 22) phrase = "Good evening";
  else phrase = "Hey";
  return name ? `${phrase}, ${name}` : phrase;
}

/**
 * The first name to greet: the signed-in person's, else the configured owner's.
 * An email address standing in for a display name, and the unset placeholder,
 * are not names.
 */
export function viewerFirstName(
  sessionName: string | null | undefined,
  ownerName: string | null | undefined,
): string | null {
  for (const candidate of [sessionName, ownerName]) {
    const first = candidate?.trim().split(/\s+/)[0];
    if (first && !first.includes("@") && first !== NO_NAME) return first;
  }
  return null;
}
