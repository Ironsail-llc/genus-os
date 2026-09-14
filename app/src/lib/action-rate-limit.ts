/**
 * The action limiter, and what it is for.
 *
 * `/api/actions/execute` carries two kinds of traffic: the reads every Helm
 * page makes as it mounts (task list, agent status, tenants, inbox) and the
 * mutations a person triggers one at a time (approve, resolve, create). The
 * limiter exists for the second kind — a runaway client should not be able to
 * file a hundred tasks a minute through a browser session. It was applied to
 * both, and a tour of five pages produced eight 429s on the box (2026-09-14):
 * the task board and the agent list simply failed to load, with nothing on
 * screen to say why. A read is not an action.
 */

export const ACTION_RATE_LIMIT = 10;
export const ACTION_RATE_WINDOW_MS = 60_000;

/** Only a request that changes something counts against the limit. */
export function isMutation(method: string): boolean {
  return method.toUpperCase() !== "GET";
}

interface Entry {
  count: number;
  resetAt: number;
}

export class ActionRateLimiter {
  private readonly entries = new Map<string, Entry>();
  private lastCleanup: number;

  constructor(
    private readonly limit: number = ACTION_RATE_LIMIT,
    private readonly windowMs: number = ACTION_RATE_WINDOW_MS,
    private readonly now: () => number = Date.now,
  ) {
    this.lastCleanup = this.now();
  }

  /** Count one action for `key`; false once the window's budget is spent. */
  allow(key: string): boolean {
    const at = this.now();
    // Purge expired entries periodically (every 5 min) so a long-lived
    // process does not keep one entry per IP it has ever seen.
    if (at - this.lastCleanup > 5 * 60_000) {
      for (const [k, entry] of this.entries) {
        if (at > entry.resetAt) this.entries.delete(k);
      }
      this.lastCleanup = at;
    }
    const entry = this.entries.get(key);
    if (!entry || at > entry.resetAt) {
      this.entries.set(key, { count: 1, resetAt: at + this.windowMs });
      return true;
    }
    if (entry.count >= this.limit) return false;
    entry.count++;
    return true;
  }

  /** Whole seconds until `key` may act again; 0 when it may act now. */
  retryAfterSeconds(key: string): number {
    const entry = this.entries.get(key);
    if (!entry) return 0;
    return Math.max(0, Math.ceil((entry.resetAt - this.now()) / 1000));
  }
}
