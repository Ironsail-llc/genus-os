"use client";

import { useEffect, useRef, useState } from "react";
import { SrcdocRenderer } from "@/components/canvas/srcdoc-renderer";
import { useCanvasBridge, type PendingProposal } from "@/components/canvas/use-canvas-bridge";
import { CANVAS_SHIM_SOURCE } from "@/lib/canvas-shim";

export function CanvasView({ visible = true }: { visible?: boolean }) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [code, setCode] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const { pendingProposal, confirmProposal, cancelProposal, dropped } = useCanvasBridge(iframeRef);

  useEffect(() => {
    if (!visible) return;
    let active = true;
    (async () => {
      try {
        const res = await fetch("/api/dashboard/welcome", { method: "POST" });
        if (!active) return;
        if (res.ok) {
          const body = await res.json();
          if (!active) return;
          setCode(typeof body?.html === "string" ? body.html : "");
        }
      } catch {
        /* leave code empty; the tab still renders */
      }
    })();
    return () => {
      active = false;
    };
  }, [visible]);

  // `confirmProposal` clears the hook's `pendingProposal` immediately on
  // click (optimistic — it doesn't wait for the fetch to settle), so the
  // dialog's own visibility is tracked separately here. That lets the
  // Confirm button stay on screen — visibly disabled — for the lifetime of
  // the request, instead of vanishing the instant it's clicked (which would
  // make the disabled state unobservable and pointless as a double-submit
  // guard).
  //
  // Derived during render (not an effect) per React's "adjusting state when
  // a prop changes" pattern: comparing against the last-seen value and
  // calling setState conditionally in the render body avoids the extra
  // commit+re-render an effect would cost, and keeps `react-hooks/set-state-in-effect` clean.
  const [shownProposal, setShownProposal] = useState<PendingProposal | null>(null);
  const [lastSeenProposal, setLastSeenProposal] = useState<PendingProposal | null>(null);
  if (pendingProposal !== lastSeenProposal) {
    setLastSeenProposal(pendingProposal);
    if (pendingProposal) {
      setShownProposal(pendingProposal);
      setSubmitting(false);
    }
  }

  const handleConfirm = () => {
    if (submitting) return;
    setSubmitting(true);
    void confirmProposal().finally(() => setShownProposal(null));
  };

  const handleCancel = () => {
    cancelProposal();
    setShownProposal(null);
  };

  return (
    <div data-testid="canvas-view" className="flex-col gap-3 p-4" style={{ display: visible ? "flex" : "none" }}>
      <h2 className="text-lg font-semibold text-zinc-100">Canvas</h2>
      <p className="text-xs text-zinc-500">
        Sandboxed canvas. Live-data binding is gated pending a sanitization-posture decision.
      </p>
      <SrcdocRenderer ref={iframeRef} html={code} bootstrap={CANVAS_SHIM_SOURCE} />

      {dropped.length > 0 && (
        <div data-testid="canvas-dropped" className="rounded border border-amber-500/50 bg-amber-500/5 p-2 text-xs text-amber-300">
          The canvas reached for {dropped.length} thing(s) it was not given:{" "}
          {dropped.map((d) => d.op).join(", ")}
        </div>
      )}

      {shownProposal && (
        <div data-testid="canvas-confirm" className="fixed inset-x-0 bottom-4 mx-auto w-max rounded-lg border border-zinc-600 bg-zinc-900 p-4 shadow-xl">
          <p className="text-sm text-zinc-100">The canvas proposes: <strong>{shownProposal.describe}</strong></p>
          <p className="mt-1 text-xs text-zinc-400">This is an operator write. Confirm to apply it.</p>
          <div className="mt-3 flex gap-2">
            <button data-testid="canvas-confirm-accept" disabled={submitting} onClick={handleConfirm}
              className="rounded bg-emerald-600 px-3 py-1 text-sm text-white hover:bg-emerald-500 disabled:opacity-50">Confirm</button>
            <button data-testid="canvas-confirm-cancel" disabled={submitting} onClick={handleCancel}
              className="rounded bg-zinc-700 px-3 py-1 text-sm text-zinc-100 hover:bg-zinc-600 disabled:opacity-50">Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

export default CanvasView;
