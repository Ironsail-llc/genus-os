"use client";

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { resolveReadOp, resolveProposeAction, isCanvasMessage } from "@/lib/canvas-bridge";

const BRIDGE_URL = "/api/bridge";

export type PendingProposal = { describe: string; method: "PATCH"; path: string; body: unknown };
export type DroppedOp = { reqId: string; op: string; at: number };

export function useCanvasBridge(iframeRef: RefObject<HTMLIFrameElement | null>) {
  const [pendingProposal, setPendingProposal] = useState<PendingProposal | null>(null);
  const [dropped, setDropped] = useState<DroppedOp[]>([]);
  // Guards confirmProposal against re-entrant double-submit — e.g. a fast
  // double-click that lands before React re-renders the disabled Confirm
  // button. A ref (not state) so the check is synchronous and cannot itself
  // race.
  const confirmInFlight = useRef(false);

  // `iframeRef` is itself a stable ref object (from the caller's useRef); only its
  // `.current` mutates. Reading `.current` inside effect/callback closures always
  // sees the latest iframe — no second ref layer needed, and it avoids mutating a
  // ref during render.
  const postResult = useCallback((reqId: string, ok: boolean, payload: unknown) => {
    const win = iframeRef.current?.contentWindow;
    if (!win) return;
    win.postMessage(
      ok ? { __robothor: true, kind: "read-result", reqId, ok: true, data: payload }
         : { __robothor: true, kind: "read-result", reqId, ok: false, error: String(payload) },
      "*",  // sandboxed opaque-origin iframe; posted only to this specific window; carries no credential
    );
  }, [iframeRef]);

  useEffect(() => {
    const onMessage = async (event: MessageEvent) => {
      // Exactly the renderer's validation: opaque origin AND our iframe as the source.
      const win = iframeRef.current?.contentWindow;
      if (event.origin !== "null" || !win || event.source !== win) return;
      if (!isCanvasMessage(event.data)) return;  // ignores srcdoc-height / robothor:error / anything untagged
      const msg = event.data;

      if (msg.kind === "read") {
        const resolved = resolveReadOp(msg.op, msg.args ?? {});
        if (!resolved) {
          setDropped((d) => [...d, { reqId: msg.reqId, op: msg.op, at: Date.now() }]);
          postResult(msg.reqId, false, `unknown op: ${msg.op}`);
          return;
        }
        try {
          const res = await fetch(`${BRIDGE_URL}${resolved.path}`, { headers: { accept: "application/json" } });
          if (!res.ok) { postResult(msg.reqId, false, `error ${res.status}`); return; }
          postResult(msg.reqId, true, await res.json());
        } catch {
          postResult(msg.reqId, false, "bridge unreachable");
        }
        return;
      }

      // propose: never executes here — build the confirm from PARENT data only.
      const action = resolveProposeAction(msg.action, msg.args);
      if (!action) {
        setDropped((d) => [...d, { reqId: msg.reqId, op: `propose:${msg.action}`, at: Date.now() }]);
        return;
      }
      setPendingProposal({ describe: action.describe, method: action.method, path: action.path, body: action.body });
    };

    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [postResult, iframeRef]);

  const confirmProposal = useCallback(async () => {
    const p = pendingProposal;
    if (!p || confirmInFlight.current) return;
    confirmInFlight.current = true;
    setPendingProposal(null);
    try {
      await fetch(`${BRIDGE_URL}${p.path}`, {
        method: p.method,
        headers: { "content-type": "application/json" },
        body: JSON.stringify(p.body),
      });
    } finally {
      confirmInFlight.current = false;
    }
  }, [pendingProposal]);

  const cancelProposal = useCallback(() => setPendingProposal(null), []);

  return { pendingProposal, confirmProposal, cancelProposal, dropped };
}
