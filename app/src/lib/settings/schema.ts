/**
 * The settings API's two payloads, read defensively into the shapes the Config
 * and Flags pages render.
 *
 * The routes are `crm/bridge/routers/settings.py`: `GET /api/settings/schema`
 * describes what CAN be configured (371 fields in 13 groups, ~164 KB, derived
 * from the typed settings model and stable across calls), and `GET
 * /api/settings` says what IS configured. They are read once per page mount
 * and are never polled — a form that re-reads under the operator's cursor
 * overwrites what they are typing.
 *
 * Why a normalizer rather than a cast:
 *
 * **A secret's value is not a value.** The route answers a secret with
 * `{configured, fingerprint}` and never the credential. `toDraft` refuses to
 * turn that object into editable text whatever else the page does, so the
 * "never render an input for a secret" rule holds in the module the input
 * would have to get its text from, not only in the component that draws it.
 *
 * **The enum is absent, not null, for an unbounded field.** Only governed
 * flags are bounded today, and the route's list is the exact tuple the PATCH
 * validates against. Carrying it through as `choices: null` means a page
 * cannot accidentally offer a rung the write would refuse.
 *
 * **A draft goes back as the declared type.** `int` "7" reaches the bridge as
 * `7`; `int` "not-a-number" reaches it unchanged, because the bridge's refusal
 * names the text the operator typed and `NaN` would reach it as JSON `null` —
 * a different setting entirely.
 */

/** Which layer supplied the value. The route's closed set, plus our own "we do not know". */
export type SettingSource = "default" | "config" | "env" | "db" | "unknown";

const SOURCES = new Set<SettingSource>(["default", "config", "env", "db"]);

export interface SettingField {
  /** The PATCH key, and the environment variable's name. */
  name: string;
  /** The same string today; kept separate because `name` is the key, `env` is the label. */
  env: string;
  group: string;
  /** `"str" | "int" | "float" | "bool"` today — anything else renders as text. */
  type: string;
  description: string;
  default: unknown;
  secret: boolean;
  governed: boolean;
  restartRequired: boolean;
  /** Units a change waits on. Always present on the wire; may be empty. */
  restartUnits: string[];
  since: string;
  /** No restart needed — a governed flag, or a field declared `restart_required: false`. */
  hot: boolean;
  /** The values the write path accepts, or `null` where the platform bounds nothing. */
  choices: string[] | null;
}

export interface SettingGroup {
  id: string;
  label: string;
  fields: SettingField[];
}

/** What a secret answers with. Never the credential. */
export interface SecretStatus {
  configured: boolean;
  /** `sha256:` + eight characters, or `null` when nothing is configured. */
  fingerprint: string | null;
}

export interface SettingValue {
  /** A `SecretStatus` for a secret; the effective value otherwise. */
  value: unknown;
  source: SettingSource;
  /** False for a secret, and for a field an environment variable supplies. */
  editable: boolean;
}

export interface SettingsValues {
  values: Record<string, SettingValue>;
  /**
   * Units whose running process is ignoring config.yaml RIGHT NOW because the
   * environment overrides a restart-required setting.
   *
   * NOT a memory of what this page saved: that list comes back in the PATCH's
   * own response and lives in `restart-banner.ts`. The route documents both
   * meanings under this one key, which is exactly how a page gets it wrong.
   */
  pendingRestart: string[];
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function toField(raw: unknown): SettingField | null {
  const row = record(raw);
  if (!row) return null;
  const name = text(row.name) || text(row.env);
  // No name, no row: it is the PATCH key, so a field without one is a control
  // that could be drawn and could never be saved.
  if (!name) return null;
  return {
    name,
    env: text(row.env) || name,
    group: text(row.group),
    type: text(row.type) || "str",
    description: text(row.description),
    default: row.default ?? null,
    secret: row.secret === true,
    governed: row.governed === true,
    restartRequired: row.restart_required === true,
    restartUnits: strings(row.restart_units),
    since: text(row.since) || "legacy",
    hot: row.hot === true,
    choices: Array.isArray(row.enum) ? strings(row.enum) : null,
  };
}

/** `GET /api/settings/schema`, in declaration order, with unreadable rows dropped. */
export function normalizeSchema(body: unknown): SettingGroup[] {
  const row = record(body);
  const groups = row?.groups;
  if (!Array.isArray(groups)) return [];
  const out: SettingGroup[] = [];
  for (const entry of groups) {
    const group = record(entry);
    if (!group) continue;
    const id = text(group.id);
    if (!id) continue;
    const fields = Array.isArray(group.fields)
      ? group.fields.map(toField).filter((f): f is SettingField => f !== null)
      : [];
    out.push({ id, label: text(group.label) || id, fields });
  }
  return out;
}

/** `GET /api/settings`. An unknown provenance label is reported as such, never as a default. */
export function normalizeValues(body: unknown): SettingsValues {
  const row = record(body);
  const values: Record<string, SettingValue> = {};
  const raw = record(row?.values);
  if (raw) {
    for (const [name, entry] of Object.entries(raw)) {
      const cell = record(entry);
      if (!cell) continue;
      const source = text(cell.source) as SettingSource;
      values[name] = {
        value: cell.value ?? null,
        source: SOURCES.has(source) ? source : "unknown",
        // Absent is not editable: a page that guessed "yes" would offer a box
        // whose save the bridge refuses.
        editable: cell.editable === true,
      };
    }
  }
  return { values, pendingRestart: strings(row?.pending_restart) };
}

/** The `{configured, fingerprint}` a secret answers with, or `null` for anything else. */
export function asSecretStatus(value: unknown): SecretStatus | null {
  const row = record(value);
  if (!row || typeof row.configured !== "boolean") return null;
  const fingerprint = row.fingerprint;
  return {
    configured: row.configured,
    fingerprint: typeof fingerprint === "string" && fingerprint ? fingerprint : null,
  };
}

/**
 * The text a control holds for this field's current value.
 *
 * A secret has no editable text by construction — the only thing the route
 * hands over is a status object, and turning it into a string is how a
 * fingerprint ends up in an input the operator can save back over a credential.
 */
export function toDraft(field: SettingField, value: unknown): string {
  if (field.secret) return "";
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return "";
}

/** The typed value to send for a draft. Unparseable numbers travel unchanged — see the header. */
export function fromDraft(field: SettingField, draft: string): unknown {
  if (field.type === "bool") return draft === "true";
  if (field.type === "int" || field.type === "float") {
    const trimmed = draft.trim();
    if (!trimmed) return draft;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : draft;
  }
  return draft;
}

/** Does the search box's text pick this field out? Name or description, either case. */
export function matchesQuery(field: SettingField, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return (
    field.name.toLowerCase().includes(needle) ||
    field.env.toLowerCase().includes(needle) ||
    field.description.toLowerCase().includes(needle)
  );
}
