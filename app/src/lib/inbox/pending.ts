/**
 * What is waiting on a person, as the browser sees it.
 *
 * The shapes are the bridge's, read off `crm/bridge/routers/approvals.py`
 * (`_pending`, `AnswerRequest`, `_refuse`) — not invented.
 *
 * Two of them are load-bearing enough to live in a module of their own rather
 * than inside the view:
 *
 * * `normalizePending` is the only place that decides which rows this screen
 *   may render. The route today lists `workflow` and `question`; `escalation`
 *   lives in the engine's RAM and CANNOT be settled by writing a row. If one
 *   ever appeared in that listing, a card offering Approve/Reject would answer
 *   nothing while showing a green tick, and the agent would wait for its own
 *   timeout to deny it. So the filter is positive — these two kinds, nothing
 *   else — instead of a blocklist that a third kind would walk straight past.
 * * It drops a row with no `id` and KEEPS a row with no `question`. The first
 *   has nothing to POST an answer to; the second has something to answer and
 *   merely nothing to print, and `workflow_approvals.prompt` is `TEXT NOT NULL`
 *   — which permits `''`. Erasing such a row hid it from the list AND the
 *   badge, and the run behind it then waited for its own timeout with no
 *   visible cause. The card prints a placeholder instead.
 *
 * Refusals are read by `readBridgeReply` in `lib/bridge/read-reply.ts`, which
 * is shared with the agents and providers pages.
 */

/** The kinds this screen may render and answer. */
export type PendingKind = "workflow" | "question";

const ANSWERABLE_KINDS: readonly string[] = ["workflow", "question"];

/** One row of `GET /api/approvals` — see `_pending`. */
export interface PendingItem {
  kind: PendingKind;
  id: string;
  run_id: string;
  /** Empty for every `workflow` row: the approval belongs to a run, not an agent. */
  agent_id: string;
  question: string;
  detail: string;
  options: string[];
  expires_at: string | null;
  created_at: string | null;
}

/** The body of `POST /api/approvals/{kind}/{id}` — see `AnswerRequest`. */
export interface AnswerBody {
  answer?: string;
  approved?: boolean;
  note?: string;
}

/** What the caller learns from one answer: did it settle, and what was said. */
export interface AnswerOutcome {
  settled: boolean;
  message: string | null;
}

/** How the operator reads each kind. "workflow" is plumbing; "Approval" is the act. */
export function kindLabel(kind: PendingKind): string {
  return kind === "workflow" ? "Approval" : "Question";
}

/** Token classes per kind. Colour carries the kind; the label still says it. */
export function kindClass(kind: PendingKind): string {
  return kind === "workflow"
    ? "border-warning/30 bg-warning/10 text-warning"
    : "border-primary/30 bg-primary/10 text-primary";
}

/** Who is waiting. A workflow approval has no agent, and saying so beats a blank. */
export function whoRaised(item: Pick<PendingItem, "agent_id">): string {
  return item.agent_id || "workflow";
}

/** The run id as the Runs view abbreviates it. */
export function shortRunId(runId: string): string {
  return (runId || "").slice(0, 8);
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * An instant read as a distance from now — an age behind, a deadline ahead.
 *
 * Never throws and never renders "Invalid Date": `expires_at` is nullable on
 * both tables the route composes, so a missing instant is a normal row and not
 * an error condition. It answers with an empty string, and the caller renders
 * nothing rather than a label with a hole in it.
 */
export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  const at = Date.parse(iso ?? "");
  if (!Number.isFinite(at)) return "";

  const delta = at - now;
  const abs = Math.abs(delta);
  if (abs < MINUTE) return delta >= 0 ? "in under a minute" : "just now";

  const [value, unit] =
    abs < HOUR
      ? [Math.round(abs / MINUTE), "minute"]
      : abs < DAY
        ? [Math.round(abs / HOUR), "hour"]
        : [Math.round(abs / DAY), "day"];

  const spelled = `${value} ${unit}${value === 1 ? "" : "s"}`;
  return delta >= 0 ? `in ${spelled}` : `${spelled} ago`;
}

/** The same instant spelled out, for the hover title beside the relative one. */
export function absoluteTime(iso: string | null | undefined): string {
  const at = Date.parse(iso ?? "");
  if (!Number.isFinite(at)) return "";
  return new Date(at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

/**
 * The route's answer, defended rather than trusted.
 *
 * A row with no `id` is dropped: there is no address to POST an answer to, so
 * the card could only lie. A row with no `question` is KEPT — see the header.
 */
export function normalizePending(body: unknown): PendingItem[] {
  const rows = (body as { pending?: unknown } | null)?.pending;
  if (!Array.isArray(rows)) return [];

  const out: PendingItem[] = [];
  for (const raw of rows) {
    if (!raw || typeof raw !== "object") continue;
    const row = raw as Record<string, unknown>;
    const kind = text(row.kind);
    if (!ANSWERABLE_KINDS.includes(kind)) continue;

    const id = text(row.id);
    if (!id) continue;

    out.push({
      kind: kind as PendingKind,
      id,
      run_id: text(row.run_id),
      agent_id: text(row.agent_id),
      question: text(row.question),
      detail: text(row.detail),
      // A blank label would be an unlabelled button that POSTs an empty
      // answer, which the route refuses with "answer is required".
      options: Array.isArray(row.options)
        ? row.options.filter((o): o is string => typeof o === "string" && Boolean(o.trim()))
        : [],
      expires_at: optionalText(row.expires_at),
      created_at: optionalText(row.created_at),
    });
  }
  return out;
}

/**
 * How many people the ROUTE says are waiting, which is the number the badge owes.
 *
 * Deliberately not `normalizePending(body).length`. The route counts rows this
 * screen may have had to drop — an id-less row today, a third `kind` tomorrow —
 * and a badge derived from what rendered would make every such row vanish
 * twice: once from the list and once from the number that is supposed to admit
 * the list is incomplete.
 */
export function routeCount(body: unknown, fallback: number): number {
  const raw = (body as { count?: unknown } | null)?.count;
  if (typeof raw !== "number" || !Number.isFinite(raw) || raw < 0) return fallback;
  return Math.floor(raw);
}
