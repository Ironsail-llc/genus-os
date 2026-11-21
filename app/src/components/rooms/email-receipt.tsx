"use client";

import { CheckCircle2, CircleAlert, CircleHelp, MailX } from "lucide-react";
import type { ReceiptStatus, RoomEmailReceipt } from "@/lib/rooms/email";

/** What the receipt says first, by outcome. */
const HEADLINES: Record<ReceiptStatus, string> = {
  sent: "Sent",
  failed: "Not sent: it failed",
  uncertain: "May have been sent: the mail service did not confirm it",
  not_sent: "Not sent",
  rejected: "Not sent: rejected",
  expired: "Not sent: nobody decided in time",
};

const TONES: Record<ReceiptStatus, string> = {
  sent: "border-success/30 bg-success/5",
  failed: "border-destructive/30 bg-destructive/5",
  uncertain: "border-warning/30 bg-warning/5",
  not_sent: "border-border bg-muted/40",
  rejected: "border-border bg-muted/40",
  expired: "border-border bg-muted/40",
};

function Icon({ status }: { status: ReceiptStatus }) {
  const className = "mt-px size-3.5 shrink-0";
  if (status === "sent") return <CheckCircle2 className={`${className} text-success`} aria-hidden />;
  if (status === "failed") return <CircleAlert className={`${className} text-destructive`} aria-hidden />;
  if (status === "uncertain") return <CircleHelp className={`${className} text-warning`} aria-hidden />;
  return <MailX className={`${className} text-muted-foreground`} aria-hidden />;
}

/**
 * The receipt of a workspace email (E3): what happened to the email a member
 * approved or rejected, said plainly, with who it went to and the subject.
 */
export function EmailReceipt({ receipt }: { receipt: RoomEmailReceipt }) {
  const to = receipt.to.join(", ");
  return (
    <div
      className={`mt-1 flex w-full max-w-md items-start gap-2 rounded-lg border px-3 py-2 text-xs ${TONES[receipt.status]}`}
      role="status"
      data-testid="room-email-receipt"
    >
      <Icon status={receipt.status} />
      <div className="min-w-0 flex-1 space-y-0.5">
        <p className="font-medium text-foreground">{HEADLINES[receipt.status]}</p>
        {receipt.subject && <p className="text-foreground wrap-anywhere">“{receipt.subject}”</p>}
        {to && <p className="text-muted-foreground wrap-anywhere">To {to}</p>}
        {receipt.decidedBy && (
          <p className="text-muted-foreground">
            {receipt.status === "rejected" ? "Rejected" : "Decided"} by {receipt.decidedBy}
          </p>
        )}
        {receipt.error && receipt.status !== "sent" && (
          <p className="text-destructive wrap-anywhere">{receipt.error}</p>
        )}
      </div>
    </div>
  );
}
