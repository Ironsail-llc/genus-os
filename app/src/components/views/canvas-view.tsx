"use client";

import { useEffect, useRef, useState } from "react";
import { SrcdocRenderer } from "@/components/canvas/srcdoc-renderer";
import { useCanvasBridge, type PendingProposal } from "@/components/canvas/use-canvas-bridge";
import { CANVAS_SHIM_SOURCE } from "@/lib/canvas-shim";
import { CANVAS_BINDER_SOURCE } from "@/lib/canvas-binder";
import { PageHeader } from "@/components/business/page-header";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

// Module-level stale-while-revalidate cache: the last welcome HTML paints
// instantly on every tab visit while a fresh copy is fetched in the
// background — the LLM-generated welcome takes seconds to produce, and a
// blank iframe on each visit made the tab feel broken.
let welcomeHtmlCache = "";

export function CanvasView({ visible = true }: { visible?: boolean }) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [code, setCode] = useState<string>(welcomeHtmlCache);
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
          if (typeof body?.html === "string") {
            welcomeHtmlCache = body.html;
            setCode(body.html);
          }
        }
      } catch {
        /* keep whatever is cached; the tab still renders */
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
  // Result of the last confirm attempt — null while nothing has been
  // confirmed yet (or after a fresh proposal / cancel resets it). Shown to
  // the operator so a failed write is never mistaken for a successful one.
  const [writeResult, setWriteResult] = useState<{ ok: boolean; error?: string } | null>(null);
  if (pendingProposal !== lastSeenProposal) {
    setLastSeenProposal(pendingProposal);
    if (pendingProposal) {
      setShownProposal(pendingProposal);
      setSubmitting(false);
      setWriteResult(null);
    }
  }

  // Only clear the dialog once the PATCH's outcome is known. On success it
  // closes as before (behavior-preserving happy path). On failure it stays
  // open with a visible failure indicator — the operator must see the write
  // did not land instead of the dialog silently vanishing as if it worked.
  const handleConfirm = () => {
    if (submitting) return;
    setSubmitting(true);
    setWriteResult(null);
    void confirmProposal().then((result) => {
      setWriteResult(result);
      setSubmitting(false);
      if (result.ok) setShownProposal(null);
    });
  };

  const handleCancel = () => {
    cancelProposal();
    setShownProposal(null);
    setWriteResult(null);
  };

  return (
    <div data-testid="canvas-view" className="flex-col gap-3 p-4" style={{ display: visible ? "flex" : "none" }}>
      <PageHeader
        title="Canvas"
        description="The operator's system, rendered live by the model. Reads are whitelisted; writes need your confirmation."
      />
      {visible && (
        <SrcdocRenderer
          ref={iframeRef}
          html={code}
          bootstrap={CANVAS_SHIM_SOURCE + "\n" + CANVAS_BINDER_SOURCE}
          testId="canvas-srcdoc-renderer"
        />
      )}

      {dropped.length > 0 && (
        <div data-testid="canvas-dropped" className="rounded-md border border-warning/25 bg-warning/10 p-2.5 text-xs text-warning">
          The canvas reached for {dropped.length} thing(s) it was not given:{" "}
          {dropped.map((d) => d.op).join(", ")}
        </div>
      )}

      <Dialog
        open={!!shownProposal}
        onOpenChange={(open) => {
          if (!open && !submitting) handleCancel();
        }}
      >
        <DialogContent data-testid="canvas-confirm" showCloseButton={false} className="max-w-md">
          <DialogHeader>
            <DialogTitle className="text-base">
              The canvas proposes: {shownProposal?.describe}
            </DialogTitle>
            <DialogDescription>
              This is an operator write. Confirm to apply it.
            </DialogDescription>
          </DialogHeader>
          {writeResult && (
            <p
              data-testid="canvas-write-result"
              className={writeResult.ok ? "text-xs text-success" : "text-xs text-warning"}
            >
              {writeResult.ok ? "Applied." : "Write failed — flag unchanged."}
            </p>
          )}
          <DialogFooter className="gap-2 sm:justify-start">
            <Button
              data-testid="canvas-confirm-accept"
              disabled={submitting}
              onClick={handleConfirm}
              size="sm"
            >
              Confirm
            </Button>
            <Button
              data-testid="canvas-confirm-cancel"
              disabled={submitting}
              onClick={handleCancel}
              variant="secondary"
              size="sm"
            >
              Cancel
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default CanvasView;
