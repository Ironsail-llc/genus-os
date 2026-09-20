import { describe, expect, it } from "vitest";

import { personalAutomationEnabled } from "../autonomy";

/**
 * The autonomy dashboard shipped unconditionally: every instance got an
 * `/account/autonomy` route and a link to it from `/account/security`,
 * enrolled or not. This is the switch that makes the feature absent rather
 * than merely inert, and it has to default to off.
 */
describe("personalAutomationEnabled", () => {
  it("is off when nothing is configured", () => {
    expect(personalAutomationEnabled({})).toBe(false);
  });

  it("is off for an empty or unset value", () => {
    expect(personalAutomationEnabled({ ROBOTHOR_AUTONOMY_ENABLED: "" })).toBe(false);
    expect(personalAutomationEnabled({ ROBOTHOR_AUTONOMY_ENABLED: "  " })).toBe(false);
  });

  it("is off for the explicit negatives an env file actually contains", () => {
    for (const value of ["0", "false", "no", "off", "False"]) {
      expect(personalAutomationEnabled({ ROBOTHOR_AUTONOMY_ENABLED: value })).toBe(false);
    }
  });

  it("accepts the ways an operator writes yes", () => {
    for (const value of ["1", "true", "TRUE", "yes", "on", " true "]) {
      expect(personalAutomationEnabled({ ROBOTHOR_AUTONOMY_ENABLED: value })).toBe(true);
    }
  });

  it("does not treat an arbitrary string as consent", () => {
    // `Boolean(process.env.X)` is the bug this replaces: it turns "false",
    // "off" and a stray comment into "on".
    expect(personalAutomationEnabled({ ROBOTHOR_AUTONOMY_ENABLED: "maybe" })).toBe(false);
  });
});
