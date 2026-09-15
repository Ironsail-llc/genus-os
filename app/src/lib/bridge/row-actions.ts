/**
 * The one way a Helm list performs an action on one of its rows.
 *
 * There were two copies of this: `views/agents/agent-manifests.tsx` and
 * `views/automations-view.tsx`, identical down to the sentences
 * ("Taken off its schedule.", "The dashboard could not reach the bridge to do
 * that."). That is not a tidiness point — the reason it is one module now is
 * that the first round of review found a bug in the copies' shared blind spot
 * (they both reported "saved" on a response that said the engine had NOT
 * reconciled), and two copies of a sentence is how one of them gets the fix.
 *
 * What it owns: the busy row, the per-row error, the per-row note, and the
 * single rule that a non-2xx is read with `readBridgeReply` rather than printed
 * as a status code.
 *
 * What it does not own: what the action MEANS. The caller passes the URL and
 * gets the parsed body back, so a route with its own reply shape (the manifest
 * PATCH's `reconcile`/`warnings`) is read by the caller that understands it.
 */

import { useCallback, useState } from "react";

import { readBridgeReply } from "@/lib/bridge/read-reply";

export interface RowActions {
  /** The row an action is in flight for, or `null`. One at a time, by design. */
  busyRow: string | null;
  rowErrors: Record<string, string>;
  rowNotes: Record<string, string>;
  setRowError: (id: string, message: string | null) => void;
  setRowNote: (id: string, message: string | null) => void;
  clearRow: (id: string) => void;
  /**
   * Perform one action against `url`. `onOk` receives the parsed body; a
   * refusal lands in `rowErrors[id]` in the bridge's own words.
   */
  act: (
    id: string,
    url: string,
    init: RequestInit,
    onOk: (body: unknown) => void
  ) => Promise<void>;
}

function withMessage(
  previous: Record<string, string>,
  id: string,
  message: string | null
): Record<string, string> {
  const next = { ...previous };
  if (message) next[id] = message;
  else delete next[id];
  return next;
}

export function useRowActions(): RowActions {
  const [busyRow, setBusyRow] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [rowNotes, setRowNotes] = useState<Record<string, string>>({});

  const setRowError = useCallback((id: string, message: string | null) => {
    setRowErrors((previous) => withMessage(previous, id, message));
  }, []);

  const setRowNote = useCallback((id: string, message: string | null) => {
    setRowNotes((previous) => withMessage(previous, id, message));
  }, []);

  const clearRow = useCallback(
    (id: string) => {
      setRowError(id, null);
      setRowNote(id, null);
    },
    [setRowError, setRowNote]
  );

  const act = useCallback(
    async (id: string, url: string, init: RequestInit, onOk: (body: unknown) => void) => {
      setBusyRow(id);
      clearRow(id);
      try {
        const res = await fetch(url, {
          headers: { "Content-Type": "application/json" },
          ...init,
        });
        if (!res.ok) {
          setRowError(id, await readBridgeReply(res));
          return;
        }
        onOk(await res.json());
      } catch {
        setRowError(id, "The dashboard could not reach the bridge to do that.");
      } finally {
        setBusyRow(null);
      }
    },
    [clearRow, setRowError]
  );

  return { busyRow, rowErrors, rowNotes, setRowError, setRowNote, clearRow, act };
}
