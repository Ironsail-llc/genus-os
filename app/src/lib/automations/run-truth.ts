/**
 * The automation surface, as the browser sees it, and the three readings that
 * turn one run row into the three columns of a card.
 *
 * Shapes are the bridge's, read off `crm/bridge/routers/automations.py`
 * (`_compose`, `_last_run`) — not invented.
 *
 * Why three columns and not one status pill: because the pill was wrong. A run
 * whose `status` is `completed` may have had its answer fail to send
 * (`delivery_status = 'failed'`), and a run that both ran and delivered may
 * have been judged `unverified_claims` by the run verifier. Those are three
 * independent facts, the operator cares about all three, and collapsing them
 * into the first one is how a nightly briefing goes eight days without arriving
 * while its agent shows green.
 *
 * Every reader here is total: a missing field, an unknown status, an unparseable
 * timestamp all produce a sentence. "not delivered" and "we do not know" are
 * different answers and both are better than a blank cell.
 */

import { relativeTime } from "@/lib/inbox/pending";

/** The newest run of one automation — see `_last_run`. */
export interface AutomationRun {
  id: string;
  started_at: string | null;
  status: string | null;
  duration_ms: number | null;
  delivery_status: string | null;
  delivered_at: string | null;
  delivery_channel: string | null;
  delivery_mode: string | null;
  verified_status: string | null;
  outcome_assessment: string | null;
}

/** One row of `GET /api/automations` — see `_compose`. */
export interface Automation {
  /**
   * The JOB id, which is what `agent_schedules` is keyed by and what the
   * breaker trips on: `main`, `main:heartbeat`, `main:worker`. One manifest can
   * be several of these.
   */
  id: string;
  /** The MANIFEST id — what the agent-manifest routes are keyed by. */
  agent_id: string;
  /** `agent` | `heartbeat` | `worker`, from `schedule_reconcile`'s own kinds. */
  kind: string;
  /** Whether a manifest PATCH can change THIS job's schedule (only `agent` can). */
  editable: boolean;
  /** The loader could not parse the manifest; this card is the schedule row alone. */
  manifest_unreadable: boolean;
  name: string;
  description: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  next_run_at: string | null;
  last_run: AutomationRun | null;
  consecutive_errors: number;
  breaker_tripped: boolean;
  breaker_threshold: number;
  delivery: { mode: string; channel: string; to: string };
}

/**
 * How a cell reads, not what colour it is.
 *
 * `unknown` is its own tone and deliberately not `bad`: "the engine never
 * recorded a verdict" and "the verdict was no" look identical in red and are
 * fixed by completely different things.
 */
export type Tone = "good" | "bad" | "warn" | "unknown";

export interface Reading {
  text: string;
  tone: Tone;
}

/** Engine run statuses that mean the run finished the way it meant to. */
const GOOD_RUN = new Set(["completed"]);
/** …and the ones that mean it did not. `skipped` is the breaker's own word. */
const BAD_RUN = new Set(["failed", "timeout", "cancelled"]);

/** `delivery_status` values the delivery layer writes on success. */
export const DELIVERED = new Set(["delivered", "sent", "ok", "success"]);

/**
 * Run statuses that have not finished yet.
 *
 * A run still going has no delivery status because it has not got there, which
 * is not the same as a finished run that failed to send. Reading the first as
 * "not delivered" in warn colour put a red cell on every automation the
 * operator happened to be watching mid-fire.
 */
const IN_FLIGHT = new Set(["pending", "running"]);

/** `verified_status` values from `robothor/engine/run_verification.py`. */
const GOOD_VERDICT = new Set(["verified", "no_claims"]);

/** …and the self-rated `outcome_assessment` from the run finalizer. */
const GOOD_OUTCOME = new Set(["successful"]);
const BAD_OUTCOME = new Set(["incorrect", "abandoned"]);

/** A status token as a person reads it: `unverified_claims` → `unverified claims`. */
export function humanizeStatus(status: string): string {
  return status.trim().replace(/_/g, " ");
}

/**
 * Column one — **ran**. When it last started, and how that attempt ended.
 *
 * An automation with no run at all says so rather than showing an empty cell:
 * "this has never fired" is the single most actionable thing this view can tell
 * an operator about a schedule they just wrote.
 */
export function ranReading(run: AutomationRun | null, now: number = Date.now()): Reading {
  if (!run) return { text: "never run", tone: "unknown" };
  const when = relativeTime(run.started_at, now);
  const status = (run.status ?? "").trim();
  const tone: Tone = GOOD_RUN.has(status) ? "good" : BAD_RUN.has(status) ? "bad" : "unknown";
  const words = [when || "at an unrecorded time", status ? humanizeStatus(status) : "no status"];
  return { text: words.join(" · "), tone };
}

/**
 * Column two — **delivered**. Did the answer leave the box, and by what route.
 *
 * `delivery.mode === "none"` is not a failure and must never read as one: most
 * worker agents are deliberately silent and communicate through CRM tasks. They
 * say "none expected", which is the difference between a fleet that is quiet
 * and a fleet that is broken.
 */
export function deliveredReading(
  automation: Pick<Automation, "delivery" | "last_run">,
  now: number = Date.now()
): Reading {
  const mode = (automation.delivery?.mode ?? "").trim();
  if (!mode || mode === "none") return { text: "none expected", tone: "unknown" };

  const run = automation.last_run;
  if (!run) return { text: "nothing delivered yet", tone: "unknown" };

  const status = (run.delivery_status ?? "").trim();
  if (!status) {
    return IN_FLIGHT.has((run.status ?? "").trim())
      ? { text: "delivery pending", tone: "unknown" }
      : { text: "not delivered", tone: "warn" };
  }

  const channel = (run.delivery_channel ?? "").trim() || (automation.delivery.channel ?? "").trim();
  const when = relativeTime(run.delivered_at, now);
  const words = [humanizeStatus(status)];
  if (channel) words.push(`via ${channel}`);
  if (when) words.push(when);
  return { text: words.join(" · "), tone: DELIVERED.has(status.toLowerCase()) ? "good" : "bad" };
}

/**
 * Column three — **completed**. Was the work any good.
 *
 * The verifier's verdict wins over the agent's own `outcome_assessment`,
 * because one of them is the agent grading itself. When there is no verdict at
 * all the cell says "not assessed" rather than borrowing the run status — a run
 * that exited zero is not a run that did the job, and that substitution is what
 * made the old single pill unusable.
 */
export function completedReading(run: AutomationRun | null): Reading {
  if (!run) return { text: "no run to assess", tone: "unknown" };

  const verdict = (run.verified_status ?? "").trim();
  if (verdict) {
    return {
      text: humanizeStatus(verdict),
      tone: GOOD_VERDICT.has(verdict) ? "good" : "warn",
    };
  }

  const outcome = (run.outcome_assessment ?? "").trim();
  if (outcome) {
    return {
      text: humanizeStatus(outcome),
      tone: GOOD_OUTCOME.has(outcome) ? "good" : BAD_OUTCOME.has(outcome) ? "bad" : "warn",
    };
  }

  return { text: "not assessed", tone: "unknown" };
}

/** Token classes per tone, matching the pills elsewhere in the Helm. */
export function toneClass(tone: Tone): string {
  if (tone === "good") return "text-success";
  if (tone === "bad") return "text-destructive";
  if (tone === "warn") return "text-warning";
  return "text-muted-foreground";
}

/** A duration as a person reads it. `null` when the engine recorded none. */
export function durationText(ms: number | null | undefined): string | null {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 90) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
  const minutes = seconds / 60;
  if (minutes < 90) return `${Math.round(minutes)} min`;
  return `${(minutes / 60).toFixed(1)} h`;
}

/**
 * What the "next run" line may honestly claim.
 *
 * `nextRunText` is pure cron arithmetic and knows nothing about whether the
 * engine will act on it. It will not: `scheduler.py` filters `spec.enabled`
 * when registering jobs and skips a tripped agent before it fires. Printing a
 * confident instant for either — down to the minute and the zone — is the
 * failure this whole view was built to remove, wearing the view's own clothes.
 */
export function nextRunClaim(
  automation: Pick<Automation, "enabled" | "breaker_tripped">,
  computed: string | null,
  heldRelative: string
): string {
  if (!automation.enabled) return "not scheduled — disabled";
  if (automation.breaker_tripped) return "held — reset the breaker to resume";
  if (computed) return computed;
  return heldRelative ? `${heldRelative} (per the scheduler)` : "not scheduled";
}

/** `GET /api/automations`, defensively: a malformed body renders as empty. */
export function normalizeAutomations(body: unknown): Automation[] {
  const rows = (body as { automations?: unknown })?.automations;
  if (!Array.isArray(rows)) return [];
  return rows.filter(
    (row): row is Automation =>
      Boolean(row) && typeof row === "object" && typeof (row as Automation).id === "string"
  );
}
