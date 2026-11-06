/**
 * The welcome canvas greets the person looking at it, at their time of day.
 *
 * It took the hour from the server's clock. A container runs on UTC, so an
 * operator five hours west of it was wished a good evening at half past noon,
 * and near midnight was shown tomorrow's date. And it took the name from an
 * environment variable whose unset default is the word "there".
 */
import { describe, expect, it } from "vitest";

import { greetingFor, localMoment, viewerFirstName } from "../welcome-greeting";

const HALF_PAST_NOON_EASTERN = new Date("2026-11-06T17:30:00Z");

describe("localMoment — the viewer's time, not the server's", () => {
  it("reads the hour, day and date in the viewer's time zone", () => {
    expect(localMoment(HALF_PAST_NOON_EASTERN, "America/New_York")).toEqual({
      hour: 12,
      dayOfWeek: "Friday",
      date: "November 6, 2026",
    });
  });

  it("keeps yesterday's date for a viewer who has not reached midnight yet", () => {
    expect(localMoment(new Date("2026-11-07T02:30:00Z"), "America/New_York")).toEqual({
      hour: 21,
      dayOfWeek: "Friday",
      date: "November 6, 2026",
    });
  });

  it("reads midnight as hour 0", () => {
    expect(localMoment(new Date("2026-11-07T05:10:00Z"), "America/New_York").hour).toBe(0);
  });

  it.each([undefined, null, "", "Not/AZone", "<script>"])(
    "falls back to the server's clock for an unusable zone (%s)",
    (zone) => {
      const now = HALF_PAST_NOON_EASTERN;
      expect(localMoment(now, zone).hour).toBe(now.getHours());
    },
  );
});

describe("greetingFor", () => {
  it.each([
    [6, "Good morning"],
    [11, "Good morning"],
    [12, "Good afternoon"],
    [16, "Good afternoon"],
    [17, "Good evening"],
    [21, "Good evening"],
    [22, "Hey"],
    [3, "Hey"],
  ])("at %i it says %s", (hour, phrase) => {
    expect(greetingFor(hour, "Alice")).toBe(`${phrase}, Alice`);
  });

  it("greets without a name when it has none, never a placeholder", () => {
    expect(greetingFor(12, null)).toBe("Good afternoon");
    expect(greetingFor(12, "")).toBe("Good afternoon");
  });
});

describe("viewerFirstName", () => {
  it("prefers the signed-in person's first name", () => {
    expect(viewerFirstName("Alice Example", "Bob")).toBe("Alice");
  });

  it("uses the configured owner name when nobody's name is on the session", () => {
    expect(viewerFirstName(null, "Bob")).toBe("Bob");
    expect(viewerFirstName("  ", "Bob")).toBe("Bob");
  });

  it("does not greet an email address or the unset placeholder by name", () => {
    expect(viewerFirstName("agent@example.com", "there")).toBeNull();
    expect(viewerFirstName(undefined, "there")).toBeNull();
  });
});
