/**
 * How the Helm reads the ENGINE half of a manifest write.
 *
 * `agent_manifests._apply_patch` answers two different things in one body: the
 * file was written (`saved`), and the engine did or did not pick the change up
 * (`reconcile.applied`). Its own docstring names the failure mode — "what must
 * not happen is the operator being told the agent is live when it is not" — and
 * an engine that is down, restarting or wedged answers
 * `{"reconcile": {"applied": false, "error": …}}` with a perfectly ordinary 200.
 *
 * B8's agent panel read that field; the Automations view did not, and printed
 * "Schedule saved. The engine re-derived its jobs." over a save that had
 * re-derived nothing. One reader now, used by both, because the sentence is the
 * thing that has to stay identical.
 */

import type { ValidationIssue } from "@/lib/agents/manifests";

/**
 * The caveat to append to a success sentence, or `null` when the engine did
 * take the change.
 *
 * `null` for a body with no `reconcile` at all: a route that does not reconcile
 * (the breaker reset) has nothing to caveat, and inventing a warning for it
 * would train the operator to ignore the real one.
 */
export function reconcileNote(body: unknown): string | null {
  const reconcile = (body as { reconcile?: { applied?: boolean; error?: string } } | null)
    ?.reconcile;
  if (!reconcile || reconcile.applied !== false) return null;
  return (
    "The manifest was written, but the engine did not pick up the schedule change " +
    `(${reconcile.error ?? "the engine did not reconcile"}). ` +
    "It will be reconciled on the next watchdog pass."
  );
}

/** Every finding on a write response — the ones it answered and the pre-existing ones. */
export function warningsOf(body: unknown): ValidationIssue[] {
  const record = (body ?? {}) as Record<string, unknown>;
  const answered = Array.isArray(record.warnings) ? (record.warnings as ValidationIssue[]) : [];
  const pre = Array.isArray(record.pre_existing) ? (record.pre_existing as ValidationIssue[]) : [];
  return [...answered, ...pre];
}

/** One line per finding, for a list row that has no room for a warnings panel. */
export function warningLine(issues: ValidationIssue[]): string | null {
  if (!issues.length) return null;
  return issues
    .map((issue) => `${issue.path || "(manifest)"} — ${issue.message}`)
    .join("; ");
}
