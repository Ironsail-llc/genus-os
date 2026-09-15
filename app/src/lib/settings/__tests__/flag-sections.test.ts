/**
 * How the Flags page decides which heading a governed flag sits under.
 *
 * The rule is DERIVED from what the schema already says about the flag, not
 * from a list of names kept here. This repo has paid three times for a
 * hand-maintained name list that drifted from what is actually registered
 * (`hardcoded-names-drift`, `robothor/flags/store.py::governed_flags`'s own
 * header, `nav-config`'s derived sub-nav), and the failure is always the same
 * shape: a flag ships, nobody adds it to the list, and it renders nowhere at
 * all. So the assertions that matter here are not "RBAC is a guardrail" but:
 *
 * * every governed flag lands in exactly one section, including one this
 *   file has never heard of;
 * * a section is never invented for a flag that is not governed.
 */
import { describe, expect, it } from "vitest";

import { flagSections } from "@/lib/settings/flag-sections";
import type { SettingField } from "@/lib/settings/schema";

function flag(name: string, choices: string[] | null, governed = true): SettingField {
  return {
    name,
    env: name,
    type: choices?.includes("true") ? "bool" : "str",
    description: `what ${name} does`,
    default: null,
    secret: false,
    governed,
    restartRequired: false,
    restartUnits: ["robothor-engine"],
    hot: true,
    choices,
  };
}

const LADDER = ["off", "observe", "alert", "enforce"];
const BOOL = ["true", "false"];

describe("flagSections", () => {
  it("puts a ladder that can enforce under Guardrails", () => {
    const sections = flagSections([flag("ROBOTHOR_RBAC_MODE", LADDER)]);
    expect(sections.map((s) => s.id)).toEqual(["guardrails"]);
    expect(sections[0].fields.map((f) => f.name)).toEqual(["ROBOTHOR_RBAC_MODE"]);
  });

  it("warns about promotion under every heading that can hold an enforce rung", () => {
    // ROBOTHOR_RIP_7_MODE and ROBOTHOR_RIP_13_MODE have an enforce rung and
    // sit under Engine ladders, because the family is matched first. A warning
    // that lived only in the Guardrails blurb would therefore be missing from
    // the heading holding two flags that block work when promoted.
    const sections = flagSections([
      flag("ROBOTHOR_RBAC_MODE", LADDER),
      flag("ROBOTHOR_RIP_13_MODE", ["observe", "enforce"]),
    ]);
    for (const section of sections) {
      const holdsEnforce = section.fields.some((f) => f.choices?.includes("enforce"));
      if (holdsEnforce) expect(section.blurb).toMatch(/never fired/i);
    }
  });

  it("puts the RIP family under Engine ladders, whatever shape their values take", () => {
    const sections = flagSections([
      flag("ROBOTHOR_RIP_1_ENABLED", BOOL),
      flag("ROBOTHOR_RIP_7_MODE", LADDER),
      flag("ROBOTHOR_RIP_13_MODE", ["observe", "enforce"]),
    ]);
    expect(sections.map((s) => s.id)).toEqual(["ladders"]);
    expect(sections[0].fields).toHaveLength(3);
  });

  it("puts a switch that cannot enforce under Tuning", () => {
    const sections = flagSections([flag("ROBOTHOR_JUDGE_ENABLED", BOOL)]);
    expect(sections.map((s) => s.id)).toEqual(["tuning"]);
  });

  it("gives a flag it has never seen a home rather than dropping it", () => {
    // The whole point of the rule. A governed flag added next release renders
    // under Tuning until somebody decides it is a guardrail — never nowhere.
    const sections = flagSections([flag("ROBOTHOR_SOMETHING_NEW", ["a", "b"])]);
    expect(sections.flatMap((s) => s.fields.map((f) => f.name))).toEqual([
      "ROBOTHOR_SOMETHING_NEW",
    ]);
  });

  it("puts every flag in exactly one section", () => {
    const fields = [
      flag("ROBOTHOR_RBAC_MODE", LADDER),
      flag("ROBOTHOR_RIP_1_ENABLED", BOOL),
      flag("ROBOTHOR_JUDGE_ENABLED", BOOL),
      flag("ROBOTHOR_SOMETHING_NEW", null),
    ];
    const placed = flagSections(fields).flatMap((s) => s.fields.map((f) => f.name));
    expect(placed.sort()).toEqual(fields.map((f) => f.name).sort());
    expect(new Set(placed).size).toBe(placed.length);
  });

  it("keeps the declaration order inside a section", () => {
    const sections = flagSections([
      flag("ROBOTHOR_Z_MODE", LADDER),
      flag("ROBOTHOR_A_MODE", LADDER),
    ]);
    expect(sections[0].fields.map((f) => f.name)).toEqual(["ROBOTHOR_Z_MODE", "ROBOTHOR_A_MODE"]);
  });

  it("ignores a field that is not governed — this page does not write those", () => {
    expect(flagSections([flag("ROBOTHOR_LOG_DIR", null, false)])).toEqual([]);
  });

  it("returns no empty sections", () => {
    const sections = flagSections([flag("ROBOTHOR_RBAC_MODE", LADDER)]);
    expect(sections.every((s) => s.fields.length > 0)).toBe(true);
  });

  it("orders the sections guardrails, ladders, tuning whatever order the fields arrive in", () => {
    const sections = flagSections([
      flag("ROBOTHOR_JUDGE_ENABLED", BOOL),
      flag("ROBOTHOR_RIP_1_ENABLED", BOOL),
      flag("ROBOTHOR_RBAC_MODE", LADDER),
    ]);
    expect(sections.map((s) => s.id)).toEqual(["guardrails", "ladders", "tuning"]);
  });
});
