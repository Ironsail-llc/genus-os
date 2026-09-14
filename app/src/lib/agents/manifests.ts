/**
 * The agent manifest surface, as the browser sees it.
 *
 * Shapes are the bridge's, read off `crm/bridge/routers/agent_manifests.py`
 * (`_summary`, `_broken`, `CreateRequest`, `PatchRequest`) and
 * `crm/bridge/routers/_manifest_validation.py` (`issue`) — not invented.
 *
 * Three pure functions live here rather than in the view, because each is a
 * claim about agreeing with something on the other side of the wire:
 *
 * * `deriveAgentId` mirrors `_kebab`. The form shows the operator the id their
 *   agent is about to get, and an id that disagrees with the one the server
 *   derives is worse than showing none — it is the value they will later type
 *   to retire the agent.
 * * `describeCron` / `nextRunText` never throw. A half-typed expression is the
 *   normal state of a cron field, so "not a valid schedule" is a reading of the
 *   field, not an error condition.
 * * `fieldForPath` maps the dotted paths in a 422 verdict onto the inputs that
 *   produced them, so a refusal lands beside the thing to fix.
 */

import cronstrue from "cronstrue";
import { CronExpressionParser } from "cron-parser";

/** One row of `GET /api/agent-manifests` — see `_summary`. */
export interface ManifestSummary {
  id: string;
  name: string;
  description: string;
  version: string;
  department: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  delivery: string;
  model: string;
}

/** A manifest the engine's loader could not read — see `_broken`. */
export interface BrokenManifest {
  id: string;
  filename: string;
  error_type: string;
}

/** One finding — see `_manifest_validation.issue`. */
export interface ValidationIssue {
  path: string;
  code: string;
  message: string;
}

export interface ValidationVerdict {
  ok: boolean;
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
  /** Only on a PATCH response: faults this edit neither introduced nor fixed. */
  pre_existing?: ValidationIssue[];
}

/** `GET /api/agent-manifests/{id}`. `manifest` is null when it will not parse. */
export interface ManifestDetail {
  manifest: Record<string, unknown> | null;
  yaml: string;
  instructions: string;
  validation: ValidationVerdict;
}

/** `GET /api/models`, as the Providers page already reads it. */
export interface ModelEntry {
  id: string;
  provider: string;
  context_window: number | null;
  supports_thinking: boolean;
  supports_tools: boolean | null;
  source: string;
}

export const DELIVERY_MODES = ["none", "announce", "log"] as const;

export const NO_SCHEDULE = "no schedule — this agent only runs when triggered";

/**
 * The id the bridge will derive from this display name.
 *
 * Mirrors `agent_manifests._kebab`: lowercase, every run of non-alphanumerics
 * becomes one `-`, and the ends are trimmed. Shown, never posted — `id` is left
 * out of the create body unless the operator typed one, so the server's own
 * derivation stays the single authority.
 */
export function deriveAgentId(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "");
}

/**
 * What a cron expression means, in words. Never throws.
 *
 * Judged by `cron-parser` — the same field semantics the engine schedules on —
 * before `cronstrue` is asked to phrase it, so an expression that reads nicely
 * but will never fire is still reported as invalid.
 */
export function describeCron(expression: string): string {
  const cron = expression.trim();
  if (!cron) return NO_SCHEDULE;
  try {
    CronExpressionParser.parse(cron);
    return cronstrue.toString(cron, { verbose: false });
  } catch {
    return "not a valid schedule";
  }
}

/**
 * When this expression next fires, in the agent's own zone. `null` when the
 * expression or the zone is one nothing can be computed from.
 *
 * `now` is a parameter so a test can pin it; nothing in the app passes it.
 */
export function nextRunText(
  expression: string,
  timezone: string,
  now: Date = new Date()
): string | null {
  const cron = expression.trim();
  if (!cron) return null;
  const zone = timezone.trim() || "UTC";
  try {
    const iterator = CronExpressionParser.parse(cron, { tz: zone, currentDate: now });
    const next = iterator.next().toDate();
    // Intl is what refuses an unknown zone: cron-parser accepts one and then
    // schedules in UTC, which would put a confident wrong time on the screen.
    return new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
      // The clock is named, because the wall time alone is ambiguous: `0 9 * * *`
      // reads "09:00" in every zone on earth, and which 09:00 is the whole
      // question the operator is asking this line.
      timeZoneName: "short",
    }).format(next);
  } catch {
    return null;
  }
}

/**
 * The form field a dotted schema path belongs to, or `null` when this form does
 * not own that path.
 *
 * Keys are the subset of `FORM_OWNED_PATHS` this builder writes. A path the
 * form has no input for is deliberately unmapped rather than guessed at: those
 * render in a list of their own, where the operator can see the path they have
 * to go and fix in the YAML.
 */
const FIELD_BY_PATH: Record<string, string> = {
  id: "id",
  name: "name",
  description: "job",
  instructions: "instructions",
  instruction_file: "instructions",
  department: "department",
  "model.primary": "model",
  "model.fallbacks": "fallbacks",
  "schedule.cron": "cron",
  "schedule.timezone": "timezone",
  "schedule.enabled": "enabled",
  "delivery.mode": "deliveryMode",
  "delivery.channel": "deliveryChannel",
  "delivery.to": "deliveryTo",
  tools_allowed: "tools",
};

export function fieldForPath(path: string): string | null {
  // `tools_allowed[2]` is a finding about the tool list, and the tool list is
  // one field: an error keyed to an index would render against nothing.
  const bare = path.trim().replace(/\[\d+\]/g, "");
  return FIELD_BY_PATH[bare] ?? null;
}

/** The server's own sentence, whenever it gave one. */
export async function readError(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object") {
      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail.trim()) return detail;
      if (Array.isArray(detail)) {
        const messages = detail
          .map((item) => (item && typeof item === "object" ? (item as { msg?: string }).msg : null))
          .filter((message): message is string => Boolean(message));
        if (messages.length) return messages.join("; ");
      }
      const error = (body as { error?: unknown }).error;
      if (typeof error === "string" && error.trim()) return error;
    }
  } catch {
    // A non-JSON body is not a reason to lose the status code below.
  }
  return `The bridge refused the request (HTTP ${res.status}).`;
}

/**
 * The IANA zones this browser knows, with the operator's own first.
 *
 * `Intl.supportedValuesOf` is absent in a few runtimes (and in some test
 * environments); the fallback is the browser's own zone plus UTC, which is
 * enough for the field to remain usable rather than empty.
 */
export function timezoneChoices(): string[] {
  let local = "UTC";
  try {
    local = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    local = "UTC";
  }
  let all: string[] = [];
  try {
    const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] })
      .supportedValuesOf;
    all = typeof supported === "function" ? supported("timeZone") : [];
  } catch {
    all = [];
  }
  // `supportedValuesOf` omits "UTC" on several runtimes, and UTC is the zone a
  // scheduled agent is most often pinned to, so it is added rather than assumed.
  const seen = new Set<string>();
  return [local, "UTC", ...all].filter((zone) => {
    if (!zone || seen.has(zone)) return false;
    seen.add(zone);
    return true;
  });
}
