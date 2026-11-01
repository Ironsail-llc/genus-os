import { describe, it, expect } from "vitest";
import { webVitalsEnabled } from "@/lib/web-vitals";

describe("webVitalsEnabled", () => {
  it("is on by default", () => {
    expect(webVitalsEnabled({})).toBe(true);
    expect(webVitalsEnabled({ GENUS_DASHBOARD_WEB_VITALS: "" })).toBe(true);
  });

  it.each(["false", "0", "off", "no", "FALSE"])("is off for %s", (value) => {
    expect(webVitalsEnabled({ GENUS_DASHBOARD_WEB_VITALS: value })).toBe(false);
  });

  it.each(["true", "1", "on", "yes"])("is on for %s", (value) => {
    expect(webVitalsEnabled({ GENUS_DASHBOARD_WEB_VITALS: value })).toBe(true);
  });
});
