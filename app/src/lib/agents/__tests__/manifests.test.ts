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
  DEPARTMENTS,
  describeCron,
  deriveAgentId,
  fieldForIssue,
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

/**
 * Fix round 1 — the describer and the next-run line, judged against the thing
 * that actually schedules these agents.
 *
 * The engine runs `APScheduler.CronTrigger.from_crontab`, which takes exactly
 * five fields. `cron-parser` takes six (it has a seconds field) and refuses a
 * day-of-month/month pair that never occurs, which APScheduler accepts. Both
 * disagreements reached the screen: a six-field expression previewed green and
 * then failed the save, and `0 0 30 2 *` — a real, schedulable manifest — was
 * rendered in destructive red in the fleet list as "not a valid schedule".
 *
 * Every expectation below was probed against
 * `/home/philip/robothor/.venv/bin/python -c "CronTrigger.from_crontab(...)"`
 * first; this table is that probe's output, not this module's opinion.
 */
describe("describeCron — parity with APScheduler", () => {
  const ACCEPTED = [
    "0 9 * * *",
    "0 9 * * 1-5",
    "*/15 * * * *",
    "0 9 * * MON-FRI",
    // A day/month pair cron-parser refuses and APScheduler schedules. It fires
    // rarely or never, which is the operator's business, not a syntax error.
    "0 0 30 2 *",
    "0 9 31 2 *",
    "0 0 29 2 *",
  ];

  const REFUSED = [
    // Six fields: cron-parser reads a seconds field the engine does not have.
    "* * * * * *",
    // Nicknames are a cron-parser extension; from_crontab wants five fields.
    "@daily",
    "nonsense",
    "0 99 * * *",
    "0 9 32 * *",
    "0 9 * 13 *",
    "0 9 * * 8",
  ];

  it.each(ACCEPTED)("phrases %s, because the engine schedules it", (expression) => {
    const text = describeCron(expression);
    expect(text).not.toBe("not a valid schedule");
    expect(text.length).toBeGreaterThan(0);
  });

  it.each(REFUSED)("refuses %s, because the engine refuses it", (expression) => {
    expect(describeCron(expression)).toBe("not a valid schedule");
  });

  it("never calls a schedulable expression broken in the fleet list", () => {
    // The list row renders `describeCron` directly, so a false negative here
    // shows a healthy agent to the operator as broken.
    expect(describeCron("0 0 30 2 *")).toContain("February");
  });
});

describe("nextRunText — never an instant in a zone nobody chose", () => {
  it("returns null when no timezone has been given", () => {
    // The engine's own default is America/New_York
    // (robothor/engine/config.py), not UTC. Substituting UTC printed a
    // fully-spelled instant four to five hours from the truth.
    expect(nextRunText("0 9 * * *", "", new Date("2026-09-14T12:00:00Z"))).toBeNull();
    expect(nextRunText("0 9 * * *", "   ", new Date("2026-09-14T12:00:00Z"))).toBeNull();
  });

  it("still answers for a zone that was given", () => {
    expect(nextRunText("0 9 * * *", "UTC", new Date("2026-09-14T12:00:00Z"))).toContain("UTC");
  });

  it("returns null for an expression the engine would refuse", () => {
    expect(nextRunText("* * * * * *", "UTC")).toBeNull();
    expect(nextRunText("@daily", "UTC")).toBeNull();
  });
});

describe("fieldForIssue — against verdicts the bridge really produces", () => {
  // Every path/message pair below was taken from a real run of
  // `_manifest_validation.validate` or read off
  // `robothor/templates/manifest_checks.py`. The previous test asserted the
  // map against its own declaration, which is how `check.*` — most real
  // refusals — went unmapped.
  const issue = (path: string, message: string) => ({ path, code: "x", message });

  it("keys the whole-check findings to the field they are about", () => {
    expect(fieldForIssue(issue("check.D", "unknown tool in tools_allowed: 'not_a_tool'"))).toBe(
      "tools"
    );
    expect(fieldForIssue(issue("check.F", "Invalid cron expression '0 99 * * *': ..."))).toBe(
      "cron"
    );
  });

  it("reads the field out of a structure finding that names one", () => {
    expect(
      fieldForIssue(issue("check.B", "delivery.mode=announce but no delivery.channel"))
    ).toBe("deliveryChannel");
    expect(fieldForIssue(issue("check.B", "delivery.mode=announce but no delivery.to"))).toBe(
      "deliveryTo"
    );
    expect(fieldForIssue(issue("check.B", "Invalid delivery.mode: sideways"))).toBe("deliveryMode");
    expect(
      fieldForIssue(
        issue(
          "check.B",
          "No model.primary specified, and docs/agents/_defaults.yaml supplies none either"
        )
      )
    ).toBe("model");
    expect(fieldForIssue(issue("check.A", "department 'ops' not in schema enum: [...]"))).toBe(
      "department"
    );
  });

  it("leaves a finding about a field this form does not own in the list", () => {
    expect(fieldForIssue(issue("check.B", "Invalid session_target: sideways"))).toBeNull();
    expect(fieldForIssue(issue("check.L", "hook 0 missing event_type"))).toBeNull();
    expect(fieldForIssue(issue("v2.sandbox.image", "no such image"))).toBeNull();
  });

  it("still maps the plain dotted paths the schema produces", () => {
    expect(fieldForIssue(issue("schedule.cron", "five fields, not three"))).toBe("cron");
    expect(fieldForIssue(issue("department", "department='ops' is not one of [...]"))).toBe(
      "department"
    );
  });
});

describe("DEPARTMENTS", () => {
  it("is the schema's closed enum, so the field cannot offer a value that 422s", () => {
    // robothor/engine/schema/agent_manifest.yaml
    expect(DEPARTMENTS).toEqual([
      "email",
      "calendar",
      "operations",
      "security",
      "communications",
      "crm",
      "briefings",
      "core",
      "examples",
      "system",
      "custom",
    ]);
  });
});
