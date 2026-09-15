import { describe, expect, it } from "vitest";

import {
  completedReading,
  deliveredReading,
  durationText,
  normalizeAutomations,
  ranReading,
  type Automation,
  type AutomationRun,
} from "../run-truth";

const NOW = Date.parse("2026-06-15T12:00:00Z");

function run(overrides: Partial<AutomationRun> = {}): AutomationRun {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    started_at: "2026-06-15T09:00:00Z",
    status: "completed",
    duration_ms: 4200,
    delivery_status: "delivered",
    delivered_at: "2026-06-15T09:01:00Z",
    delivery_channel: "telegram",
    delivery_mode: "announce",
    verified_status: "verified",
    outcome_assessment: "successful",
    ...overrides,
  };
}

function automation(overrides: Partial<Automation> = {}): Automation {
  return {
    id: "invoice-chaser",
    name: "Invoice Chaser",
    kind: "agent",
    description: "",
    cron: "0 9 * * *",
    timezone: "UTC",
    enabled: true,
    next_run_at: "2026-06-16T09:00:00Z",
    last_run: run(),
    consecutive_errors: 0,
    breaker_tripped: false,
    breaker_threshold: 5,
    delivery: { mode: "announce", channel: "telegram", to: "agent@example.com" },
    ...overrides,
  };
}

describe("ran", () => {
  it("says when it started and how that attempt ended", () => {
    expect(ranReading(run(), NOW)).toEqual({ text: "3 hours ago · completed", tone: "good" });
  });

  it("calls a failure a failure", () => {
    expect(ranReading(run({ status: "failed" }), NOW).tone).toBe("bad");
    expect(ranReading(run({ status: "timeout" }), NOW).tone).toBe("bad");
  });

  it("says an automation has never run rather than showing nothing", () => {
    expect(ranReading(null, NOW)).toEqual({ text: "never run", tone: "unknown" });
  });

  it("does not guess at a status it has never heard of", () => {
    expect(ranReading(run({ status: "quarantined" }), NOW).tone).toBe("unknown");
  });

  it("survives a run row with no timestamp and no status", () => {
    const reading = ranReading(run({ started_at: null, status: null }), NOW);
    expect(reading.text).toContain("unrecorded");
    expect(reading.text).toContain("no status");
  });
});

describe("delivered", () => {
  it("names the status, the channel and when", () => {
    expect(deliveredReading(automation(), NOW)).toEqual({
      text: "delivered · via telegram · 3 hours ago",
      tone: "good",
    });
  });

  it("a silent agent reads as none expected, never as a failure", () => {
    const silent = automation({ delivery: { mode: "none", channel: "", to: "" } });
    expect(deliveredReading(silent, NOW)).toEqual({ text: "none expected", tone: "unknown" });
  });

  it("a run that never recorded a delivery is NOT delivered", () => {
    const missed = automation({ last_run: run({ delivery_status: null, delivered_at: null }) });
    const reading = deliveredReading(missed, NOW);
    expect(reading.text).toBe("not delivered");
    expect(reading.tone).toBe("warn");
  });

  it("a delivery the channel refused is the case the single status pill hid", () => {
    const failed = automation({ last_run: run({ delivery_status: "failed" }) });
    expect(deliveredReading(failed, NOW).tone).toBe("bad");
  });

  it("says nothing has been delivered yet when the automation has never run", () => {
    expect(deliveredReading(automation({ last_run: null }), NOW).tone).toBe("unknown");
  });
});

describe("completed", () => {
  it("prefers the verifier's verdict to the agent's own opinion of itself", () => {
    const graded = run({ verified_status: "unverified_claims", outcome_assessment: "successful" });
    expect(completedReading(graded)).toEqual({ text: "unverified claims", tone: "warn" });
  });

  it("falls back to the outcome when there is no verdict", () => {
    const rated = run({ verified_status: null, outcome_assessment: "partial" });
    expect(completedReading(rated).text).toBe("partial");
    expect(completedReading(run({ verified_status: null, outcome_assessment: "incorrect" })).tone)
      .toBe("bad");
  });

  it("never borrows the run status — exiting zero is not doing the job", () => {
    const unjudged = run({ verified_status: null, outcome_assessment: null, status: "completed" });
    expect(completedReading(unjudged)).toEqual({ text: "not assessed", tone: "unknown" });
  });

  it("has an answer for an automation that has never run", () => {
    expect(completedReading(null).tone).toBe("unknown");
  });
});

describe("durationText", () => {
  it("scales the unit to the length of the run", () => {
    expect(durationText(420)).toBe("420 ms");
    expect(durationText(4200)).toBe("4.2 s");
    expect(durationText(42_000)).toBe("42 s");
    expect(durationText(420_000)).toBe("7 min");
    expect(durationText(7_200_000)).toBe("2.0 h");
  });

  it("answers null for a duration the engine never recorded", () => {
    expect(durationText(null)).toBeNull();
    expect(durationText(undefined)).toBeNull();
    expect(durationText(Number.NaN)).toBeNull();
  });
});

describe("normalizeAutomations", () => {
  it("reads the listing", () => {
    expect(normalizeAutomations({ automations: [automation()] })).toHaveLength(1);
  });

  it("renders a malformed body as empty rather than throwing the page away", () => {
    expect(normalizeAutomations(null)).toEqual([]);
    expect(normalizeAutomations({ automations: "nope" })).toEqual([]);
    expect(normalizeAutomations({ automations: [null, 3, { id: "ok" }] })).toHaveLength(1);
  });
});
