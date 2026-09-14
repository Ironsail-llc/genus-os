/**
 * The pure half of the agent builder: the id the operator is shown before the
 * server derives its own, the sentence a cron expression means, and the field
 * a server-side validation error belongs next to.
 *
 * These are tested apart from the view because each one is a claim about
 * agreeing with something outside this file — `_kebab` in
 * `crm/bridge/routers/agent_manifests.py`, the cron the engine schedules on,
 * and `FORM_OWNED_PATHS`. A rendering test would pass while any of the three
 * silently disagreed.
 */
import { describe, expect, it } from "vitest";

import {
  describeCron,
  deriveAgentId,
  fieldForPath,
  nextRunText,
} from "../manifests";

describe("deriveAgentId", () => {
  it("kebab-cases a display name the way the bridge does", () => {
    expect(deriveAgentId("Vendor Follow Up")).toBe("vendor-follow-up");
    expect(deriveAgentId("Weekly   Digest")).toBe("weekly-digest");
    expect(deriveAgentId("  Invoice Chaser  ")).toBe("invoice-chaser");
  });

  it("collapses runs of punctuation into one separator", () => {
    expect(deriveAgentId("Q3 // Reporting!!")).toBe("q3-reporting");
    expect(deriveAgentId("A_B.C")).toBe("a-b-c");
  });

  it("trims separators off both ends", () => {
    expect(deriveAgentId("--Lead Scout--")).toBe("lead-scout");
    expect(deriveAgentId("...")).toBe("");
  });

  it("keeps digits, because agent ids routinely carry them", () => {
    expect(deriveAgentId("Shift 2 Reporter")).toBe("shift-2-reporter");
  });
});

describe("describeCron", () => {
  it("renders the common shapes in words", () => {
    expect(describeCron("0 9 * * *")).toMatch(/09:00|9:00/);
    expect(describeCron("0 9 * * 1-5")).toMatch(/Monday/i);
    expect(describeCron("*/15 * * * *")).toMatch(/15 minutes/i);
  });

  it("says a bad expression is not a schedule instead of throwing", () => {
    expect(describeCron("nonsense")).toBe("not a valid schedule");
    expect(describeCron("0 99 * * *")).toBe("not a valid schedule");
  });

  it("treats an empty expression as no schedule at all", () => {
    expect(describeCron("")).toBe("no schedule — this agent only runs when triggered");
    expect(describeCron("   ")).toBe("no schedule — this agent only runs when triggered");
  });
});

describe("nextRunText", () => {
  it("names the next firing in the agent's own timezone", () => {
    const text = nextRunText("0 9 * * *", "UTC", new Date("2026-09-14T12:00:00Z"));
    // The next 09:00 UTC after noon on the 14th is the 15th.
    expect(text).toContain("2026");
    expect(text).toMatch(/09:00|9:00/);
  });

  it("names the clock, because 09:00 reads the same in every zone", () => {
    const utc = nextRunText("0 9 * * *", "UTC", new Date("2026-09-14T12:00:00Z"));
    const tokyo = nextRunText("0 9 * * *", "Asia/Tokyo", new Date("2026-09-14T12:00:00Z"));
    // Same wall time, two different instants: only the named zone tells them
    // apart, and without it the line is a confident ambiguity.
    expect(utc).not.toBe(tokyo);
    expect(utc).toContain("UTC");
  });

  it("returns null rather than throwing on an expression cron cannot parse", () => {
    expect(nextRunText("nonsense", "UTC")).toBeNull();
    expect(nextRunText("0 9 * * *", "Not/AZone")).toBeNull();
    expect(nextRunText("", "UTC")).toBeNull();
  });
});

describe("fieldForPath", () => {
  it("maps the dotted schema paths the bridge refuses on to form fields", () => {
    expect(fieldForPath("schedule.cron")).toBe("cron");
    expect(fieldForPath("schedule.timezone")).toBe("timezone");
    expect(fieldForPath("model.primary")).toBe("model");
    expect(fieldForPath("model.fallbacks")).toBe("fallbacks");
    expect(fieldForPath("delivery.mode")).toBe("deliveryMode");
    expect(fieldForPath("delivery.channel")).toBe("deliveryChannel");
    expect(fieldForPath("delivery.to")).toBe("deliveryTo");
    expect(fieldForPath("name")).toBe("name");
    expect(fieldForPath("description")).toBe("job");
    expect(fieldForPath("id")).toBe("id");
    expect(fieldForPath("tools_allowed")).toBe("tools");
  });

  it("drops a list index so an error on one tool lands on the tool field", () => {
    expect(fieldForPath("tools_allowed[2]")).toBe("tools");
    expect(fieldForPath("model.fallbacks[0]")).toBe("fallbacks");
  });

  it("returns null for a path no field on this form owns", () => {
    expect(fieldForPath("v2.sandbox.image")).toBeNull();
    expect(fieldForPath("")).toBeNull();
  });
});
