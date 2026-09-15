/**
 * The one reader of the ENGINE half of a manifest write.
 *
 * The defect these pin: both list views printed "saved, the engine re-derived
 * its jobs" on any 2xx, while the bridge answers a perfectly ordinary 200
 * carrying `reconcile.applied === false` whenever the engine is unreachable.
 */
import { describe, expect, it } from "vitest";

import { reconcileNote, warningLine, warningsOf } from "../reconcile";

describe("reconcileNote", () => {
  it("names the engine's own reason when it did not pick the change up", () => {
    const note = reconcileNote({ saved: true, reconcile: { applied: false, error: "timeout" } });
    expect(note).toMatch(/did not pick up/i);
    expect(note).toMatch(/timeout/);
    expect(note).toMatch(/watchdog/i);
  });

  it("still says so when the bridge gave no reason", () => {
    expect(reconcileNote({ reconcile: { applied: false } })).toMatch(/did not reconcile/i);
  });

  it("is silent when the engine DID reconcile", () => {
    expect(reconcileNote({ saved: true, reconcile: { applied: true } })).toBeNull();
  });

  it("is silent for a route that does not reconcile at all", () => {
    // The breaker reset. Inventing a caveat here would teach the operator to
    // ignore the real one.
    expect(reconcileNote({ id: "main", consecutive_errors: 0 })).toBeNull();
    expect(reconcileNote(null)).toBeNull();
    expect(reconcileNote("not even an object")).toBeNull();
  });
});

describe("warningsOf", () => {
  it("carries both the findings this write answered and the pre-existing ones", () => {
    const issues = warningsOf({
      warnings: [{ path: "schedule.cron", code: "check.F", message: "fires every minute" }],
      pre_existing: [{ path: "tools_allowed", code: "check.D", message: "unknown tool" }],
    });
    expect(issues).toHaveLength(2);
  });

  it("answers empty for a body with neither, and never throws on a malformed one", () => {
    expect(warningsOf({})).toEqual([]);
    expect(warningsOf({ warnings: "nope" })).toEqual([]);
    expect(warningsOf(null)).toEqual([]);
  });
});

describe("warningLine", () => {
  it("is null when there is nothing to say, so the row renders no empty slot", () => {
    expect(warningLine([])).toBeNull();
  });

  it("names the path a finding is about", () => {
    expect(
      warningLine([{ path: "schedule.cron", code: "check.F", message: "fires every minute" }])
    ).toBe("schedule.cron — fires every minute");
  });

  it("has a word for a finding with no path", () => {
    expect(warningLine([{ path: "", code: "check.B", message: "structure" }])).toBe(
      "(manifest) — structure"
    );
  });
});
