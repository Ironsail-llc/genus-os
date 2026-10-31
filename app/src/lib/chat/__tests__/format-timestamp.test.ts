import { describe, it, expect } from "vitest";
import { formatMessageTime, fullMessageTime } from "../format-timestamp";

// Local-time constructors, so the cases hold in any timezone the suite runs in.
const NOW = new Date(2026, 9, 31, 16, 30); // Sat 31 Oct 2026, 16:30

describe("formatMessageTime", () => {
  it("shows only the time for a message sent today", () => {
    expect(formatMessageTime(new Date(2026, 9, 31, 14, 5), NOW, "en-GB")).toBe("14:05");
  });

  it("shows the weekday and time for a message sent in the last six days", () => {
    // Monday 26 Oct
    expect(formatMessageTime(new Date(2026, 9, 26, 9, 7), NOW, "en-GB")).toBe("Mon 09:07");
  });

  it("shows the date and time for an older message this year", () => {
    expect(formatMessageTime(new Date(2026, 9, 12, 8, 0), NOW, "en-GB")).toBe("12 Oct 08:00");
  });

  it("adds the year for a message from another year", () => {
    expect(formatMessageTime(new Date(2025, 11, 24, 23, 59), NOW, "en-GB")).toBe("24 Dec 2025 23:59");
  });

  it("treats a clock a little ahead as today rather than inventing a weekday", () => {
    expect(formatMessageTime(new Date(2026, 9, 31, 16, 31), NOW, "en-GB")).toBe("16:31");
  });

  it("refuses an invalid date instead of printing Invalid Date", () => {
    expect(formatMessageTime(new Date("not a date"), NOW, "en-GB")).toBe("");
  });
});

describe("fullMessageTime", () => {
  it("spells out the whole date for the tooltip", () => {
    const full = fullMessageTime(new Date(2026, 9, 26, 9, 7), "en-GB");
    expect(full).toContain("2026");
    expect(full).toContain("October");
    expect(full).toContain("09:07");
  });
});
