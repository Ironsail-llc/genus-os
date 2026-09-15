/**
 * Reading the settings API's two payloads without trusting either of them.
 *
 * Every shape here is `crm/bridge/routers/settings.py`'s own — the schema
 * groups, the values map, the `{configured, fingerprint}` a secret answers
 * with. Nothing is invented: a fixture carrying a field the route does not
 * answer with is how a page ships against an API that does not exist.
 *
 * The claims that matter are the ones about NOT lying:
 *
 * * a secret's value is a STATUS, never a string the form could put in a box;
 * * a field the schema does not bound has no choices, so the page cannot
 *   offer a rung the PATCH would refuse;
 * * a draft is turned back into the type the field declares, so `3` reaches
 *   the bridge as a number and `not-a-number` reaches it unchanged, to be
 *   refused in the bridge's own words rather than silently become `NaN`.
 */
import { describe, expect, it } from "vitest";

import {
  asSecretStatus,
  fromDraft,
  matchesQuery,
  normalizeSchema,
  normalizeValues,
  toDraft,
  type SettingField,
} from "@/lib/settings/schema";

const SCHEMA = {
  groups: [
    {
      id: "engine",
      label: "Engine",
      fields: [
        {
          name: "ROBOTHOR_MAX_CONCURRENT_AGENTS",
          env: "ROBOTHOR_MAX_CONCURRENT_AGENTS",
          field: "engine.max_concurrent_agents",
          group: "engine",
          aliases: [],
          type: "int",
          description: "How many agent runs may execute at once.",
          default: 3,
          secret: false,
          governed: false,
          restart_required: true,
          restart_units: ["robothor-engine"],
          since: "legacy",
          hot: false,
        },
      ],
    },
    {
      id: "flags",
      label: "Flags",
      fields: [
        {
          name: "ROBOTHOR_RBAC_MODE",
          env: "ROBOTHOR_RBAC_MODE",
          field: "flags.rbac_mode",
          group: "flags",
          aliases: [],
          type: "str",
          description: "RBAC ladder position.",
          default: "observe",
          secret: false,
          governed: true,
          restart_required: false,
          restart_units: ["robothor-engine"],
          since: "legacy",
          hot: true,
          enum: ["off", "observe", "alert", "enforce"],
        },
      ],
    },
  ],
};

function field(overrides: Partial<SettingField> = {}): SettingField {
  return {
    name: "ROBOTHOR_X",
    env: "ROBOTHOR_X",
    group: "engine",
    type: "str",
    description: "",
    default: null,
    secret: false,
    governed: false,
    restartRequired: true,
    restartUnits: [],
    since: "legacy",
    hot: false,
    choices: null,
    ...overrides,
  };
}

describe("normalizeSchema", () => {
  it("keeps the group and field order the route answered with", () => {
    const groups = normalizeSchema(SCHEMA);
    expect(groups.map((g) => g.id)).toEqual(["engine", "flags"]);
    expect(groups[0].fields[0].name).toBe("ROBOTHOR_MAX_CONCURRENT_AGENTS");
    expect(groups[0].fields[0].restartUnits).toEqual(["robothor-engine"]);
  });

  it("carries the enum only where the route bounds the field", () => {
    const [engine, flags] = normalizeSchema(SCHEMA);
    expect(engine.fields[0].choices).toBeNull();
    expect(flags.fields[0].choices).toEqual(["off", "observe", "alert", "enforce"]);
  });

  it("drops a field with no name rather than rendering a row nothing can save", () => {
    const groups = normalizeSchema({
      groups: [
        { id: "engine", label: "Engine", fields: [{ type: "str" }, SCHEMA.groups[0].fields[0]] },
      ],
    });
    expect(groups[0].fields).toHaveLength(1);
  });

  it("answers an unreadable body with no groups, never a throw", () => {
    expect(normalizeSchema(null)).toEqual([]);
    expect(normalizeSchema({ groups: "nope" })).toEqual([]);
  });
});

describe("normalizeValues", () => {
  it("reads the value, the source and the editable flag", () => {
    const { values, pendingRestart } = normalizeValues({
      values: {
        ROBOTHOR_MAX_CONCURRENT_AGENTS: { value: 7, source: "config", editable: true },
        ROBOTHOR_ENGINE_HOST: { value: "0.0.0.0", source: "env", editable: false },
      },
      pending_restart: ["robothor-bridge", "robothor-engine"],
    });
    expect(values.ROBOTHOR_MAX_CONCURRENT_AGENTS).toEqual({
      value: 7,
      source: "config",
      editable: true,
    });
    expect(values.ROBOTHOR_ENGINE_HOST.editable).toBe(false);
    expect(pendingRestart).toEqual(["robothor-bridge", "robothor-engine"]);
  });

  it("treats a source it does not know as unknown rather than as a default", () => {
    const { values } = normalizeValues({
      values: { ROBOTHOR_X: { value: "a", source: "somewhere-new", editable: true } },
    });
    expect(values.ROBOTHOR_X.source).toBe("unknown");
  });

  it("answers an unreadable body with nothing, never a throw", () => {
    expect(normalizeValues(undefined)).toEqual({ values: {}, pendingRestart: [] });
  });
});

describe("asSecretStatus", () => {
  it("reads the status object a secret answers with", () => {
    expect(asSecretStatus({ configured: true, fingerprint: "sha256:ab12cd34" })).toEqual({
      configured: true,
      fingerprint: "sha256:ab12cd34",
    });
  });

  it("reads an unconfigured secret without inventing a fingerprint", () => {
    expect(asSecretStatus({ configured: false, fingerprint: null })).toEqual({
      configured: false,
      fingerprint: null,
    });
  });

  it("is null for anything that is not that object", () => {
    expect(asSecretStatus("a-string-that-is-not-a-status")).toBeNull();
    expect(asSecretStatus(null)).toBeNull();
  });
});

describe("toDraft / fromDraft", () => {
  it("round-trips a boolean as the two strings a switch can hold", () => {
    const f = field({ type: "bool" });
    expect(toDraft(f, true)).toBe("true");
    expect(toDraft(f, false)).toBe("false");
    expect(fromDraft(f, "true")).toBe(true);
    expect(fromDraft(f, "false")).toBe(false);
  });

  it("sends a number as a number", () => {
    expect(fromDraft(field({ type: "int" }), "7")).toBe(7);
    expect(fromDraft(field({ type: "float" }), "0.5")).toBe(0.5);
  });

  it("sends an unparseable number UNCHANGED, so the bridge writes the refusal", () => {
    // Coercing here would either send NaN (which JSON renders as null, a
    // different setting entirely) or silently drop the operator's edit. The
    // bridge already answers "'not-a-number' is not a valid value (...)" and
    // that sentence names the value they typed.
    expect(fromDraft(field({ type: "int" }), "not-a-number")).toBe("not-a-number");
    expect(fromDraft(field({ type: "int" }), "")).toBe("");
  });

  it("renders an absent value as empty text rather than the word null", () => {
    expect(toDraft(field(), null)).toBe("");
    expect(toDraft(field(), undefined)).toBe("");
  });

  it("never renders a secret's status as editable text", () => {
    expect(
      toDraft(field({ secret: true }), { configured: true, fingerprint: "sha256:ab12cd34" })
    ).toBe("");
  });
});

describe("matchesQuery", () => {
  const f = field({
    name: "ROBOTHOR_MAX_CONCURRENT_AGENTS",
    description: "How many agent runs may execute at once.",
  });

  it("matches the name, case-insensitively and on a fragment", () => {
    expect(matchesQuery(f, "concurrent")).toBe(true);
    expect(matchesQuery(f, "MAX_CONC")).toBe(true);
  });

  it("matches the description", () => {
    expect(matchesQuery(f, "execute at once")).toBe(true);
  });

  it("matches everything on an empty query", () => {
    expect(matchesQuery(f, "   ")).toBe(true);
  });

  it("does not match something in neither", () => {
    expect(matchesQuery(f, "telegram")).toBe(false);
  });
});
