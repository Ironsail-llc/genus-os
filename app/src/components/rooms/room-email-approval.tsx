"use client";

import { useState } from "react";
import { Loader2, Mail } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { AnswerBody, AnswerOutcome, PendingItem, PendingKind } from "@/lib/inbox/pending";
import type { RoomEmail } from "@/lib/rooms/email";

function Row({ label, testId, children }: { label: string; testId: string; children: string }) {
  if (!children) return null;
  return (
    <div className="grid grid-cols-[4.5rem_1fr] gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-foreground wrap-anywhere" data-testid={testId}>
        {children}
      </dd>
    </div>
  );
}

/**
 * A workspace email waiting on its member (E3): exactly what would be sent,
 * field by field and the whole body, then Approve and send, or Reject.
 * What is approved is this exact email; nothing here edits it.
 */
export function RoomEmailApproval({
  item,
  email,
  onAnswer,
}: {
  item: PendingItem;
  email: RoomEmail;
  onAnswer: (id: string, kind: PendingKind, body: AnswerBody) => Promise<AnswerOutcome>;
}) {
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function decide(approved: boolean) {
    setBusy(approved ? "approve" : "reject");
    setMessage(null);
    try {
      const outcome = await onAnswer(item.id, item.kind, { approved });
      if (!outcome.settled) setMessage(outcome.message ?? "The decision was not taken. Nothing was sent.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <article
      className="flex flex-col gap-2 rounded-lg border border-border bg-card px-3 py-2.5 text-xs"
      aria-label={`Email to approve: ${email.subject}`}
      data-testid={`room-email-${item.id}`}
    >
      <p className="flex items-center gap-1.5 text-sm font-medium text-foreground">
        <Mail className="size-3.5 text-muted-foreground" aria-hidden />
        Email to approve{email.onBehalfOf ? `, for ${email.onBehalfOf}` : ""}
      </p>
      <dl className="flex flex-col gap-1">
        <Row label="From" testId="room-email-from">{email.from}</Row>
        <Row label="Reply-To" testId="room-email-reply-to">{email.replyTo}</Row>
        <Row label="To" testId="room-email-to">{email.to.join(", ")}</Row>
        <Row label="Cc" testId="room-email-cc">{email.cc.join(", ")}</Row>
        <Row label="Subject" testId="room-email-subject">{email.subject}</Row>
      </dl>
      <pre
        className="max-h-80 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-muted/40 px-2.5 py-2 font-sans text-xs text-foreground"
        data-testid="room-email-body"
      >{email.body}</pre>
      {message && (
        <p className="text-destructive" role="alert" data-testid={`room-email-message-${item.id}`}>
          {message}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button size="xs" variant="ghost" disabled={busy !== null} onClick={() => void decide(false)}>
          {busy === "reject" && <Loader2 className="animate-spin" aria-hidden />}
          Reject
        </Button>
        <Button size="xs" disabled={busy !== null} onClick={() => void decide(true)}>
          {busy === "approve" && <Loader2 className="animate-spin" aria-hidden />}
          Approve and send
        </Button>
      </div>
    </article>
  );
}
