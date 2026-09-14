"use client";

import { Loader2, RefreshCw } from "lucide-react";

import { PageHeader } from "@/components/business/page-header";
import { Button } from "@/components/ui/button";
import { isOperatorRole } from "@/components/layout/nav-config";
import type { AnswerBody, AnswerOutcome, PendingItem, PendingKind } from "@/lib/inbox/pending";

import { InboxCard } from "./inbox/inbox-card";

/**
 * The Inbox: everything waiting on a person, in one list, answered in place.
 *
 * Approvals and agent questions interleaved by deadline, in the order the
 * route already sorted them — soonest first. They are re-sorted nowhere: the
 * ordering is a property of the queue, and a client that re-sorted would
 * disagree with the count beside it the moment a row changed.
 *
 * Review tasks (CRM tasks in REVIEW) are NOT here. The approvals route does
 * not carry them, and inventing a second fetch to join them in would make the
 * badge count two different things. The empty state says where they still
 * live rather than leaving the operator to guess.
 *
 * Presentational by design: the poll, the count and the POST all belong to
 * `useInbox`, which the shell mounts once so the badge and this list can never
 * disagree about what is waiting.
 */

export interface InboxViewProps {
  visible: boolean;
  items: PendingItem[];
  isLoading: boolean;
  error: string | null;
  /** The bridge refused the listing as operator-only — not a broken appliance. */
  refusedAsNonOperator: boolean;
  onRefresh: () => void;
  onAnswer: (id: string, kind: PendingKind, body: AnswerBody) => Promise<AnswerOutcome>;
  /** Takes the operator to the Runs view; there is no per-run deep link yet. */
  onOpenRuns: () => void;
  /** Session role. UX gate only — the bridge authorizes every route itself. */
  role?: string | null;
  /** The session has not resolved yet, so the role is unknown — not "denied". */
  roleLoading?: boolean;
}

export function InboxView({
  visible,
  items,
  isLoading,
  error,
  refusedAsNonOperator,
  onRefresh,
  onAnswer,
  onOpenRuns,
  role,
  roleLoading = false,
}: InboxViewProps) {
  // An unresolved session is not a refusal: an owner must not be shown a
  // read-only queue for the half second before their own role arrives.
  const canWrite = roleLoading || isOperatorRole(role);
  const readOnly = !roleLoading && !isOperatorRole(role) && !refusedAsNonOperator;

  return (
    <div
      className="h-full w-full flex-col overflow-y-auto"
      style={{ display: visible ? "flex" : "none" }}
      data-testid="inbox-view"
    >
      <div className="flex flex-col gap-3 p-4">
        <PageHeader
          title="Inbox"
          description="Approvals and agent questions, soonest deadline first."
        >
          <Button variant="outline" size="sm" data-testid="inbox-refresh" onClick={onRefresh}>
            <RefreshCw aria-hidden />
            Refresh
          </Button>
        </PageHeader>

        {readOnly ? (
          <p className="text-xs text-muted-foreground" data-testid="inbox-readonly">
            You can see what is waiting, but answering it is the operator&apos;s to do. Ask an
            owner or admin on this instance.
          </p>
        ) : null}

        {refusedAsNonOperator ? (
          <p className="text-xs text-muted-foreground" data-testid="inbox-restricted">
            The waiting queue is operator-only on this appliance, so there is nothing to show
            here. Ask an owner or admin on this instance.
          </p>
        ) : null}

        {error ? (
          <div
            className="flex flex-col gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-4"
            data-testid="inbox-error"
          >
            <p className="text-sm font-medium text-destructive">The waiting queue failed to load</p>
            <p className="text-xs text-muted-foreground">{error}</p>
            <div>
              <Button variant="outline" size="sm" data-testid="inbox-retry" onClick={onRefresh}>
                Try again
              </Button>
            </div>
          </div>
        ) : null}

        {isLoading && items.length === 0 && !error && !refusedAsNonOperator ? (
          <div
            className="flex items-center gap-2 rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground"
            data-testid="inbox-loading"
          >
            <Loader2 aria-hidden className="size-4 animate-spin" />
            Reading what is waiting on you…
          </div>
        ) : null}

        {!isLoading && !error && !refusedAsNonOperator && items.length === 0 ? (
          <div
            className="rounded-lg border border-dashed border-border bg-card/40 p-4"
            data-testid="inbox-empty"
          >
            <p className="text-sm font-medium text-foreground">Nothing is waiting on you.</p>
            <p className="mt-1 max-w-xl text-xs text-muted-foreground">
              Approvals and agent questions land here. Tasks in review are not in this queue yet —
              they are still on the Tasks screen.
            </p>
          </div>
        ) : null}

        {items.length > 0 ? (
          <div className="flex flex-col gap-2">
            {items.map((item) => (
              <InboxCard
                key={`${item.kind}-${item.id}`}
                item={item}
                canWrite={canWrite}
                onAnswer={onAnswer}
                onOpenRuns={onOpenRuns}
              />
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}
