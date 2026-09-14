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

/**
 * The department enum, verbatim from `robothor/engine/schema/agent_manifest.yaml`.
 *
 * A closed list, so the field that collects it is a select. As free text it
 * refused every natural answer an operator would type — ops, sales, finance,
 * marketing are all 422s — with no hint on screen that there was a list at all.
 */
export const DEPARTMENTS = [
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
] as const;

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
 * Would the ENGINE accept this cron expression?
 *
 * The engine schedules through `APScheduler.CronTrigger.from_crontab`, which
 * takes exactly five fields. `cron-parser` is not that parser and disagrees
 * with it in both directions:
 *
 * * it accepts six fields (it has a seconds column) and accepts `@daily`,
 *   neither of which `from_crontab` will take;
 * * it refuses a day-of-month/month pair that never occurs — `0 0 30 2 *` —
 *   which `from_crontab` accepts and schedules quite happily;
 * * it takes a day-of-week of 7, and the Quartz forms `L`, `W`, `?` and
 *   `5#3`, none of which APScheduler has an expression for.
 *
 * Both reached the screen. A six-field expression previewed green with a
 * next-run time and then failed the save at `schedule.cron`; and because the
 * fleet list renders this describer for every row, `0 0 30 2 *` showed a
 * healthy, running agent to the operator in destructive red.
 *
 * So: five fields, then whole-expression parse, and on failure each field
 * judged ON ITS OWN. If every field is individually in range, the only thing
 * `cron-parser` can still be objecting to is the combination — the one case
 * where APScheduler is the more permissive of the two. Probed to agree with
 * `from_crontab` on the fifteen expressions in this module's test.
 */
/** One field's atomic pieces: `1-6/2,L` → `1`, `6`, `2`, `L`. */
function tokensOf(field: string): string[] {
  return field.split(/[,\-/]/).filter(Boolean);
}

/**
 * Quartz forms `cron-parser` accepts and APScheduler has no expression for.
 *
 * Matched as FORMS, never as characters. "Reject any field containing L, W, #
 * or ?" is the obvious rule and it is wrong in three places that all schedule
 * happily today: `JUL` (July) has an L, `WED` has a W, and APScheduler's own
 * nth-weekday form is `mon#2`. Banning the characters would paint three
 * healthy agents red in the fleet list — which is the exact failure this
 * describer was fixed for in the first place.
 */
function usesUnsupportedForm(fields: string[]): boolean {
  // `?` is Quartz's "no specific value". APScheduler parses it in no field.
  if (fields.some((field) => field.includes("?"))) return true;

  // Day of month. APScheduler spells the last-day form `last`, and has no `W`
  // ("nearest weekday") at all — so a bare `L`/`W`, or `15W`, is refused while
  // the word `last` is not.
  for (const token of tokensOf(fields[2])) {
    if (/^[lw]$/i.test(token)) return true;
    if (/^\d+w$/i.test(token)) return true;
  }

  // Day of week. `mon#2` is APScheduler's; `5#3` is Quartz's and is refused.
  for (const token of tokensOf(fields[4])) {
    if (token.includes("#") && /^\d/.test(token)) return true;
  }

  return false;
}

/**
 * A day-of-week number APScheduler will not take.
 *
 * Its range is 0-6. `cron-parser` accepts 0-7, reading 7 as a second Sunday,
 * so `0 9 * * 7` — a perfectly ordinary thing to write — previewed green and
 * then failed the save.
 *
 * The occurrence in `mon#7` is stripped first: that 7 is "the seventh
 * Wednesday", not a day number, and APScheduler accepts it.
 */
function dayOfWeekOutOfRange(field: string): boolean {
  const withoutOccurrence = field.replace(/#\d+/g, "");
  for (const digits of withoutOccurrence.match(/\d+/g) ?? []) {
    if (Number(digits) > 6) return true;
  }
  return false;
}

/**
 * APScheduler's week, which begins on Monday.
 *
 * `cron-parser`'s begins on Sunday, so the two libraries disagree about every
 * named range that touches the weekend: `SUN-SAT` is the whole week to one and
 * backwards to the other, and `SAT-SUN` is the reverse. Neither library's
 * answer can be borrowed for this field — only this table's.
 */
const DAY_OF_WEEK_ORDER: Record<string, number> = {
  mon: 0,
  tue: 1,
  wed: 2,
  thu: 3,
  fri: 4,
  sat: 5,
  sun: 6,
};

/**
 * A day-of-week field written entirely in day NAMES, judged on APScheduler's
 * ordering: `true` valid, `false` refused, `null` "not all names — let
 * `cron-parser` judge it".
 *
 * APScheduler refuses a range whose start sorts after its end, and its sort is
 * the table above.
 */
function namedDayOfWeekVerdict(field: string): boolean | null {
  let sawName = false;
  for (const part of field.split(",")) {
    const ends = part.split("/")[0].split("-");
    if (ends.length > 2) return null;
    const indices = ends.map((end) => DAY_OF_WEEK_ORDER[end.trim().toLowerCase()]);
    // A number, or anything else this table does not know: not our question.
    if (indices.some((index) => index === undefined)) return null;
    sawName = true;
    if (indices.length === 2 && indices[0] > indices[1]) return false;
  }
  return sawName ? true : null;
}

/**
 * Fields whose form only APScheduler knows, so `cron-parser` must not be asked
 * about them. Both are real, both are refused by `cron-parser`, and both were
 * being called invalid before this round.
 */
function isEngineOnlyField(field: string, index: number): boolean {
  if (index === 2) return /^last$/i.test(field.trim());
  if (index === 4) return /^[a-z]{3}#\d+$/i.test(field.trim());
  return false;
}

export function cronIsValid(expression: string): boolean {
  const cron = expression.trim();
  if (!cron) return false;
  const fields = cron.split(/\s+/);
  // Exactly five. This is the whole of the six-field and `@daily` answer.
  if (fields.length !== 5) return false;

  // Judged BEFORE cron-parser is asked anything, because cron-parser accepts
  // every one of these and would answer a confident yes.
  if (usesUnsupportedForm(fields)) return false;
  if (dayOfWeekOutOfRange(fields[4])) return false;

  // Named days are settled here, in both directions: `cron-parser` calls
  // `SUN-SAT` a whole week (it is a refusal) and `SAT-SUN` backwards (it is
  // not), so neither its yes nor its no may be used for this field.
  const namedDays = namedDayOfWeekVerdict(fields[4]);
  if (namedDays === false) return false;

  try {
    CronExpressionParser.parse(cron);
    return true;
  } catch {
    // Wildcarding one field and keeping the rest would hide an out-of-range
    // value in the field that was wildcarded; each field is checked alone.
    for (let index = 0; index < 5; index += 1) {
      if (isEngineOnlyField(fields[index], index)) continue;
      if (index === 4 && namedDays === true) continue;
      const probe = ["*", "*", "*", "*", "*"];
      probe[index] = fields[index];
      try {
        CronExpressionParser.parse(probe.join(" "));
      } catch {
        return false;
      }
    }
    return true;
  }
}

/**
 * What a cron expression means, in words. Never throws.
 *
 * Validity is `cronIsValid`'s answer — the engine's — and `cronstrue` is asked
 * only to phrase an expression already judged schedulable. Where the two
 * disagree the validity check wins: a describer that reads nicely is worth
 * nothing next to one that agrees with the scheduler.
 */
export function describeCron(expression: string): string {
  const cron = expression.trim();
  if (!cron) return NO_SCHEDULE;
  if (!cronIsValid(cron)) return "not a valid schedule";
  try {
    return cronstrue.toString(cron, { verbose: false });
  } catch {
    // Schedulable, but this phrasing library will not say it out loud. The
    // expression itself is a better answer than calling it invalid.
    return cron;
  }
}

/**
 * When this expression next fires, in the agent's own zone. `null` when the
 * expression, or the zone, is one nothing can be computed from.
 *
 * An absent zone answers `null` and never falls back to UTC. The engine's own
 * default is `America/New_York` (`robothor/engine/config.py`), so substituting
 * UTC printed a fully-specified instant, zone spelled out to three letters,
 * four to five hours away from when the agent would actually run — and the
 * unset zone is the state every manifest without an explicit
 * `schedule.timezone` loads in, which is to say the common one.
 *
 * `now` is a parameter so a test can pin it; nothing in the app passes it.
 */
export function nextRunText(
  expression: string,
  timezone: string,
  now: Date = new Date()
): string | null {
  const cron = expression.trim();
  if (!cronIsValid(cron)) return null;
  const zone = timezone.trim();
  if (!zone) return null;
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

/**
 * Checks whose every finding is about one field, whatever it says.
 *
 * `check_issues` keys each A–M check finding to `check.<id>`
 * (`crm/bridge/routers/_manifest_validation.py`), so the path alone says which
 * CHECK complained and not which field — and these are most of the refusals an
 * operator will actually hit from the Advanced drawer.
 */
const FIELD_BY_CHECK: Record<string, string> = {
  // D: tools_allowed entries not in the registry. K: missing basic I/O tools.
  "check.D": "tools",
  "check.K": "tools",
  // F: CronTrigger.from_crontab refused the expression.
  "check.F": "cron",
};

/**
 * Dotted paths as they appear inside a check's MESSAGE, most specific first.
 *
 * Order is load-bearing. "delivery.mode=announce but no delivery.channel"
 * names two paths and is about the second one: the mode is the premise, the
 * missing channel is the fault. Matching `delivery.channel` and `delivery.to`
 * before `delivery.mode` lands each of the three real check-B messages on the
 * input that fixes it.
 */
const PATHS_IN_MESSAGE: Array<[string, string]> = [
  ["delivery.channel", "deliveryChannel"],
  ["delivery.to", "deliveryTo"],
  ["delivery.mode", "deliveryMode"],
  ["model.fallbacks", "fallbacks"],
  ["model.primary", "model"],
  ["schedule.cron", "cron"],
  ["schedule.timezone", "timezone"],
  ["tools_allowed", "tools"],
  ["department", "department"],
];

/**
 * The form field one verdict finding belongs under, or `null` to leave it in
 * the list of things this form cannot fix.
 *
 * Path first, because a schema finding names its field exactly. Then the check
 * id, for the checks that are about one field entire. Then the message, for
 * `check.B` — "manifest structure" — which is five different faults sharing an
 * id and is the only one that has to be read rather than looked up.
 */
export function fieldForIssue(issue: ValidationIssue): string | null {
  const direct = fieldForPath(issue.path);
  if (direct) return direct;

  const path = issue.path.trim();
  if (FIELD_BY_CHECK[path]) return FIELD_BY_CHECK[path];
  if (!path.startsWith("check.")) return null;

  const message = issue.message ?? "";
  for (const [token, field] of PATHS_IN_MESSAGE) {
    if (message.includes(token)) return field;
  }
  return null;
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
