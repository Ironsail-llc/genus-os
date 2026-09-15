/**
 * The Observe pages' copy of the bridge's time patterns.
 *
 * The cases below are the ones that were wrong on the bridge before round 1,
 * kept here so the app never sends what the bridge refuses: a trailing newline
 * used to smuggle a value past an `^…$` `.match()` and reach journald as a
 * different question from the one the operator asked.
 */
import { describe, expect, it } from "vitest";

import { isIsoTimestamp, isLogSince } from "../since";

describe("isIsoTimestamp", () => {
  it("takes a date, a date-time, and a zone", () => {
    for (const value of [
      "2026-09-15",
      "2026-09-15T12:00",
      "2026-09-15 12:00:00",
      "2026-09-15T12:00:00.123456",
      "2026-09-15T12:00:00Z",
      "2026-09-15T12:00:00+00:00",
      "2026-09-15T12:00:00-0400",
    ]) {
      expect(isIsoTimestamp(value)).toBe(true);
    }
  });

  it("refuses what is not one", () => {
    for (const value of ["", "yesterday", "notatimestamp", "2026-9-15", "1h", "2026-09-15T12"]) {
      expect(isIsoTimestamp(value)).toBe(false);
    }
  });

  /**
   * The bridge's check PARSES — `_params.iso_timestamp` hands the value to a
   * real date, so `2026-13-01` is a 422 there. A shape-only check here left the
   * page building a CSV export link out of a value the route was about to
   * refuse, which is the one thing this module's header says it exists to
   * prevent. These are calendar errors that pass the regex.
   */
  it("refuses a date the calendar does not have", () => {
    for (const value of [
      "2026-13-01",
      "2026-00-10",
      "2026-09-31",
      "2026-02-30",
      "2027-02-29",
      "2026-09-00",
      "2026-09-15T25:00",
      "2026-09-15T12:60",
    ]) {
      expect(isIsoTimestamp(value)).toBe(false);
    }
  });

  it("takes a leap day in a year that has one", () => {
    expect(isIsoTimestamp("2028-02-29")).toBe(true);
  });

  it("refuses a value with a trailing newline", () => {
    expect(isIsoTimestamp("2026-09-15\n")).toBe(false);
  });
});

describe("isLogSince", () => {
  it("takes a relative age in journald's units", () => {
    for (const value of ["30s", "15m", "1h", "7d", "999999d"]) {
      expect(isLogSince(value)).toBe(true);
    }
  });

  it("takes an ISO timestamp too", () => {
    expect(isLogSince("2026-09-15T12:00:00Z")).toBe(true);
  });

  it("refuses journald's English, which this route does not expose", () => {
    for (const value of ["yesterday", "2 hours ago", "-1h", "1w", "1h ", "1h\n", ""]) {
      expect(isLogSince(value)).toBe(false);
    }
  });
});
