"use client";

import { useState } from "react";
import { Activity, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  absoluteTime,
  kindClass,
  kindLabel,
  relativeTime,
  shortRunId,
  whoRaised,
  type AnswerBody,
  type AnswerOutcome,
  type PendingItem,
  type PendingKind,
} from "@/lib/inbox/pending";

/**
 * One thing waiting on a person, answered where it is read.
 *
 * The two kinds are answered with different fields and the card never blurs
 * them: a `question` takes `answer` (an option the operator clicked, or free
 * text), a `workflow` takes `approved` plus an optional `note`. Sending the
 * wrong field is a 400 from the router, so the shape is decided here by the
 * row's own kind rather than by which control happens to be on screen.
 *
 * Nothing here calls `window.confirm`. A verdict that matters is worth a
 * visible button with a name on it, and a browser dialog is unstyleable,
 * unreachable on a phone keyboard, and invisible to every test.
 *
 * What the card CANNOT do is decide whether the operator may answer:
 * `canWrite` hides the controls, and the bridge calls `require_operator` on
 * the route regardless.
 */

/** Roughly three lines of the detail's type size in a narrow card. */
const CLAMP_AT_CHARS = 180;

interface InboxCardProps {
  item: PendingItem;
  /** Whether to render the answer controls at all. UX gate only. */
  canWrite: boolean;
  onAnswer: (id: string, kind: PendingKind, body: AnswerBody) => Promise<AnswerOutcome>;
  /** Takes the operator to the Runs view — there is no per-run deep link yet. */
  onOpenRuns: () => void;
}

function Stamp({
  testId,
  label,
  iso,
}: {
  testId: string;
  label: string;
  iso: string | null;
}) {
  const relative = relativeTime(iso);
  if (!relative) return null;
  return (
    <span data-testid={testId} title={absoluteTime(iso)} className="whitespace-nowrap">
      {label} {relative}
    </span>
  );
}

export function InboxCard({ item, canWrite, onAnswer, onOpenRuns }: InboxCardProps) {
  const [typed, setTyped] = useState("");
  const [note, setNote] = useState("");
  const [detailOpen, setDetailOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  /** The note, only if there is one. An empty string is a note nobody wrote. */
  function typedNote(): { note?: string } {
    const trimmed = note.trim();
    return trimmed ? { note: trimmed } : {};
  }

  async function send(body: AnswerBody) {
    setBusy(true);
    setMessage(null);
    try {
      const outcome = await onAnswer(item.id, item.kind, body);
      // A settled row leaves the list, so this card is about to unmount and
      // has nothing to say. Only a refusal needs words.
      if (!outcome.settled) setMessage(outcome.message ?? "The bridge did not settle that.");
    } finally {
      setBusy(false);
    }
  }

  const runId = shortRunId(item.run_id);

  // Whether the clamp would actually hide anything. A "more" that reveals
  // nothing is noise on every card with a one-line detail, which is most of
  // them; the threshold is a reading of the text, not a measurement, because
  // the rendered line count is not knowable before paint.
  const clampable = item.detail.length > CLAMP_AT_CHARS || item.detail.includes("\n");

  return (
    <article
      data-testid={`inbox-card-${item.id}`}
      className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge
          variant="outline"
          data-testid={`inbox-kind-${item.id}`}
          className={kindClass(item.kind)}
        >
          {kindLabel(item.kind)}
        </Badge>
        <span
          data-testid={`inbox-who-${item.id}`}
          className="font-mono text-[11px] text-muted-foreground"
        >
          {whoRaised(item)}
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          <Stamp testId={`inbox-raised-${item.id}`} label="raised" iso={item.created_at} />
          <Stamp testId={`inbox-expires-${item.id}`} label="expires" iso={item.expires_at} />
        </div>
      </div>

      <p
        data-testid={`inbox-question-${item.id}`}
        className={`text-sm font-medium ${item.question ? "text-foreground" : "text-muted-foreground italic"}`}
      >
        {item.question || "(no prompt recorded)"}
      </p>

      {item.detail ? (
        <div className="flex flex-col items-start gap-1">
          <p
            data-testid={`inbox-detail-${item.id}`}
            className={`text-xs text-muted-foreground ${clampable && !detailOpen ? "line-clamp-3" : ""}`}
          >
            {item.detail}
          </p>
          {clampable ? (
            <button
              type="button"
              data-testid={`inbox-detail-toggle-${item.id}`}
              onClick={() => setDetailOpen((prev) => !prev)}
              className="text-[11px] text-primary hover:underline"
            >
              {detailOpen ? "less" : "more"}
            </button>
          ) : null}
        </div>
      ) : null}

      {runId ? (
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          <button
            type="button"
            data-testid={`inbox-run-${item.id}`}
            onClick={onOpenRuns}
            className="inline-flex items-center gap-1 text-primary hover:underline"
          >
            <Activity aria-hidden className="size-3" />
            Runs
          </button>
          <span data-testid={`inbox-run-id-${item.id}`} className="font-mono">
            {runId}
          </span>
        </div>
      ) : null}

      {canWrite && item.kind === "question" ? (
        <div className="flex flex-col gap-2">
          {item.options.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {item.options.map((option, index) => (
                <Button
                  key={`${option}-${index}`}
                  variant="outline"
                  size="xs"
                  disabled={busy}
                  data-testid={`inbox-option-${item.id}-${index}`}
                  onClick={() => void send({ answer: option })}
                >
                  {option}
                </Button>
              ))}
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-1.5">
            <Input
              aria-label="Your answer"
              data-testid={`inbox-answer-${item.id}`}
              value={typed}
              disabled={busy}
              autoComplete="off"
              placeholder={
                item.options.length > 0 ? "…or answer in your own words" : "Your answer"
              }
              onChange={(event) => setTyped(event.target.value)}
              className="min-w-0 flex-1"
            />
            <Button
              size="xs"
              disabled={busy || !typed.trim()}
              data-testid={`inbox-send-${item.id}`}
              onClick={() => void send({ answer: typed.trim() })}
            >
              Send
            </Button>
          </div>
        </div>
      ) : null}

      {canWrite && item.kind === "workflow" ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <Input
            aria-label="Note (optional)"
            data-testid={`inbox-note-${item.id}`}
            value={note}
            disabled={busy}
            autoComplete="off"
            placeholder="Note (optional)"
            onChange={(event) => setNote(event.target.value)}
            className="min-w-0 flex-1"
          />
          <Button
            size="xs"
            disabled={busy}
            data-testid={`inbox-approve-${item.id}`}
            onClick={() => void send({ approved: true, ...typedNote() })}
          >
            Approve
          </Button>
          <Button
            variant="outline"
            size="xs"
            disabled={busy}
            data-testid={`inbox-reject-${item.id}`}
            onClick={() => void send({ approved: false, ...typedNote() })}
          >
            Reject
          </Button>
        </div>
      ) : null}

      {busy ? (
        <span
          aria-live="polite"
          data-testid={`inbox-pending-${item.id}`}
          className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground"
        >
          <Loader2 aria-hidden className="size-3 animate-spin" />
          Sending your answer…
        </span>
      ) : null}

      {message ? (
        <span
          aria-live="polite"
          data-testid={`inbox-message-${item.id}`}
          className="text-xs text-warning"
        >
          {message}
        </span>
      ) : null}
    </article>
  );
}
