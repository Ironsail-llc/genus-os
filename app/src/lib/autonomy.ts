/**
 * Does this instance offer personal automation? Server-side only.
 *
 * Mirrors `robothor/settings/model.py::AutonomySettings.enabled`
 * (`ROBOTHOR_AUTONOMY_ENABLED`), the one instance-level switch for the whole
 * feature. Off is the default, and off means ABSENT, not merely inert: no
 * `/account/autonomy` route, no link to it from `/account/security`, in the
 * same way the engine attaches no autonomy paragraph and no autonomy tool
 * wording. A page nobody can use is still a page an unenrolled operator has
 * to read, decide about, and explain to their auditor.
 *
 * This is a UX gate and nothing more. The bridge checks the caller's identity
 * on every `/api/autonomy/*` route, and the broker re-checks real authority on
 * every operation; turning the flag on authorizes nobody to do anything.
 */

/** Values an operator plausibly writes for "yes" in an env file. */
const TRUTHY = new Set(["1", "true", "yes", "on"]);

export function personalAutomationEnabled(
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  return TRUTHY.has((env.ROBOTHOR_AUTONOMY_ENABLED ?? "").trim().toLowerCase());
}
