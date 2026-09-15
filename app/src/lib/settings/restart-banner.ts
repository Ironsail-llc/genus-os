/**
 * What a save on the Config page left needing a restart — the one piece of
 * settings state the API deliberately does not keep.
 *
 * `PATCH /api/settings` answers with the units ITS change waits on, and
 * nothing on the bridge remembers that afterwards: `GET /api/settings`'s
 * identically-named `pending_restart` answers the other question ("is
 * config.yaml being ignored by this process right now"), is derived, and will
 * NOT list a save that had no environment variable competing with it. So the
 * only place this can live is the client, and it is a module store rather than
 * component state for one reason: `settings-view.tsx` unmounts an inactive
 * page, so a `useState` would drop the banner the moment the operator went to
 * look at another settings page — which is precisely when they would forget a
 * service still has to be restarted.
 *
 * Deliberately NOT persisted to storage. It is true for this browser session
 * only: an operator who restarts the unit and comes back tomorrow must not be
 * told by a page that cannot know it.
 */

import { useSyncExternalStore } from "react";

export interface RestartNotice {
  /** Units to restart, sorted and deduplicated. Never empty — see `noteRestartNeeded`. */
  units: string[];
  /** The settings that put them there, so the banner can say what it is about. */
  names: string[];
}

let notice: RestartNotice | null = null;
const listeners = new Set<() => void>();

function publish(next: RestartNotice | null): void {
  notice = next;
  for (const listener of listeners) listener();
}

function union(previous: string[] | undefined, added: string[]): string[] {
  return Array.from(new Set([...(previous ?? []), ...added])).sort();
}

/**
 * Record a `PATCH /api/settings` reply.
 *
 * An empty `units` is a save that needs no restart — a governed flag, or a
 * field declared hot — and records nothing: a banner that says "restart
 * nothing" is noise that teaches an operator to dismiss the one that matters.
 */
export function noteRestartNeeded(units: string[], names: string[]): void {
  const next = units.filter(Boolean);
  if (next.length === 0) return;
  publish({
    units: union(notice?.units, next),
    names: union(notice?.names, names.filter(Boolean)),
  });
}

/** The operator has read it. */
export function dismissRestartNotice(): void {
  if (notice === null) return;
  publish(null);
}

/** The current notice, or `null`. Stable between changes — `useSyncExternalStore` requires that. */
export function restartNotice(): RestartNotice | null {
  return notice;
}

/** Tests only: a module store outlives a test file's components. */
export function resetRestartNotice(): void {
  notice = null;
  listeners.clear();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The notice, re-rendering the caller when it changes. `null` on the server. */
export function useRestartNotice(): RestartNotice | null {
  return useSyncExternalStore(subscribe, restartNotice, () => null);
}
