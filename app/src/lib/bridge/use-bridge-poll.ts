/**
 * The one way a Helm view reads a bridge listing and keeps it fresh.
 *
 * There were three byte-near copies of this block — `automations-view.tsx`,
 * `settings/channels-page.tsx` and `settings/users-page.tsx` — down to the
 * sentence "The dashboard could not reach the bridge. Check that the service is
 * running." That is the same duplication `row-actions.ts` was written to end,
 * and its header names the reason: two copies of a sentence is how one of them
 * gets the fix. Three copies of a POLL is worse than three copies of a
 * sentence, because each one carries the same four mistakes to get wrong:
 *
 * * **poll only while visible.** `AppShell` mounts every view and hides it with
 *   `display: none`, so an ungated interval puts an operator-gated listing on
 *   every Helm page load whatever the operator is actually looking at.
 * * **the loader must not be the effect's dependency.** It is re-created on
 *   every render-relevant change; depending on it tears the interval down and
 *   rebuilds it on each tick, which is how a 60 s poll quietly becomes a much
 *   faster one. It is held in a ref instead.
 * * **403 is not an error.** It is the bridge saying the listing belongs to an
 *   operator, and a member must not be shown red for it.
 * * **a refusal is read with `readBridgeReply`**, never printed as a status code.
 *
 * What it does NOT own is what the body MEANS: the caller gets the parsed body
 * and normalizes it, because every listing has its own shape and its own
 * defence against one.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { readBridgeReply } from "@/lib/bridge/read-reply";

/** The one sentence for "the request never got there". */
export const BRIDGE_UNREACHABLE =
  "The dashboard could not reach the bridge. Check that the service is running.";

/** Every Helm listing polls on the same beat. */
export const POLL_MS = 60_000;

export interface BridgePollOptions {
  /** The view or sub-page is on screen. `false` means: touch nothing. */
  visible: boolean;
  /** Absolute path through the app's bridge proxy. */
  url: string;
  /** The parsed body of a 2xx. Re-created per render; held in a ref. */
  onData: (body: unknown) => void;
  intervalMs?: number;
}

export interface BridgePoll {
  /** True until the first attempt settles, however it settles. */
  loading: boolean;
  /** The server's own words, or `null`. Never set for a 403. */
  error: string | null;
  /** The bridge says this listing is not this caller's. Not a failure. */
  forbidden: boolean;
  /** Read it again now — for a Refresh button, or after a write. */
  reload: () => void;
}

export function useBridgePoll({
  visible,
  url,
  onData,
  intervalMs = POLL_MS,
}: BridgePollOptions): BridgePoll {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  // Both refs exist for the same reason: neither the caller's handler nor the
  // loader may become a dependency of the interval effect.
  const onDataRef = useRef(onData);
  onDataRef.current = onData;

  const loadRef = useRef<() => Promise<void>>(async () => {});
  loadRef.current = async () => {
    try {
      const res = await fetch(url);
      if (!res.ok) {
        if (res.status === 403) {
          setForbidden(true);
          setError(null);
          return;
        }
        setError(await readBridgeReply(res));
        return;
      }
      setForbidden(false);
      onDataRef.current(await res.json());
      setError(null);
    } catch {
      setError(BRIDGE_UNREACHABLE);
    } finally {
      setLoading(false);
    }
  };

  const reload = useCallback(() => {
    void loadRef.current();
  }, []);

  useEffect(() => {
    if (!visible) return;
    void loadRef.current();
    const timer = setInterval(() => void loadRef.current(), intervalMs);
    return () => clearInterval(timer);
  }, [visible, url, intervalMs]);

  return { loading, error, forbidden, reload };
}
