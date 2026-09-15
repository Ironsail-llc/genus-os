"use client";

import { useState } from "react";
import { Pause, Play, Rocket, ShieldAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cronIsValid, describeCron, nextRunText, NO_SCHEDULE } from "@/lib/agents/manifests";
import {
  completedReading,
  deliveredReading,
  durationText,
  nextRunClaim,
  ranReading,
  toneClass,
  type Automation,
} from "@/lib/automations/run-truth";
import { absoluteTime, relativeTime } from "@/lib/inbox/pending";

/**
 * One automation, and the three things an operator wants to know about it.
 *
 * **Ran / delivered / completed as three cells, never one pill.** A run can
 * finish, fail to send, and be judged unverified — three independent facts. The
 * fleet list before this showed the first and called it health, which is how a
 * nightly briefing went days without arriving behind a green badge.
 *
 * **The schedule is the manifest's.** Edit writes `PATCH
 * /api/agent-manifests/{id}`, so the cron shown is the one the operator can
 * change and the one the engine will load on its next reconcile. The
 * scheduler's own `next_run_at` is offered as the hover title rather than the
 * headline: where they disagree, the manifest is what the buttons on this card
 * act on.
 *
 * **The breaker chip is an action, not a warning.** The scheduler stops running
 * an agent after N consecutive failures and, until this card, the only way back
 * was an UPDATE typed into psql. Showing the state without the reset would have
 * been the same dead end with better typography.
 */

export interface AutomationActions {
  onToggle: (automation: Automation) => void | Promise<void>;
  onRunNow: (automation: Automation) => void | Promise<void>;
  onResetBreaker: (automation: Automation) => void | Promise<void>;
  onSaveSchedule: (
    automation: Automation,
    edit: { cron: string; timezone: string; change: string }
  ) => void | Promise<void>;
}

export interface AutomationCardProps extends AutomationActions {
  automation: Automation;
  /** UX gate only — the bridge authorizes every route on this card itself. */
  canWrite: boolean;
  busy: boolean;
  error?: string | null;
  note?: string | null;
  /** The caveat when the engine did not pick a write up — `reconcileNote`. */
  reconcile?: string | null;
  /** Manifest findings a write answered with, as one line — `warningLine`. */
  warnings?: string | null;
}

function Cell({
  id,
  label,
  reading,
}: {
  id: string;
  label: string;
  reading: { text: string; tone: "good" | "bad" | "warn" | "unknown" };
}) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="text-[11px] uppercase tracking-[0.08em] text-muted-foreground">{label}</span>
      <span data-testid={id} className={`text-xs ${toneClass(reading.tone)}`}>
        {reading.text}
      </span>
    </div>
  );
}

export function AutomationCard({
  automation,
  canWrite,
  busy,
  error,
  note,
  reconcile,
  warnings,
  onToggle,
  onRunNow,
  onResetBreaker,
  onSaveSchedule,
}: AutomationCardProps) {
  const [edit, setEdit] = useState<{ cron: string; timezone: string; change: string } | null>(null);

  const human = describeCron(automation.cron);
  // The manifest's cron, phrased by B8's describer, because that is what the
  // Edit form below writes and what the engine will hold after its next
  // reconcile. `next_run_at` is what it holds NOW, and it is the hover title.
  //
  // `nextRunClaim` is what stops the arithmetic being printed as a promise: a
  // disabled or breaker-tripped job has a perfectly valid cron and will not
  // fire on it.
  const computedNext = nextRunText(automation.cron, automation.timezone);
  const heldNext = relativeTime(automation.next_run_at);
  const nextText = nextRunClaim(automation, computedNext, heldNext);

  const ran = ranReading(automation.last_run);
  const delivered = deliveredReading(automation);
  const completed = completedReading(automation.last_run);
  const took = durationText(automation.last_run?.duration_ms);

  const editPreview = edit ? describeCron(edit.cron) : "";
  const editable = Boolean(edit && cronIsValid(edit.cron));

  return (
    <div
      data-testid={`automation-card-${automation.id}`}
      className={`flex flex-col gap-3 rounded-lg border p-3 transition-colors ${
        automation.breaker_tripped
          ? "border-destructive/40 bg-destructive/5"
          : "border-border bg-card hover:border-ring/25"
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <span
            data-testid={`automation-name-${automation.id}`}
            className="text-sm font-semibold text-foreground"
          >
            {automation.name || automation.id}
          </span>
          <span className="font-mono text-[11px] text-muted-foreground">{automation.id}</span>
          {automation.description ? (
            <span className="max-w-prose text-xs text-muted-foreground">
              {automation.description}
            </span>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge
            variant="outline"
            data-testid={`automation-enabled-${automation.id}`}
            className={
              automation.enabled
                ? "border-success/30 bg-success/10 text-success"
                : "border-border bg-muted text-muted-foreground"
            }
          >
            {automation.enabled ? "Enabled" : "Disabled"}
          </Badge>
          {automation.kind && automation.kind !== "agent" ? (
            <Badge
              variant="outline"
              data-testid={`automation-kind-${automation.id}`}
              className="border-ring/30 bg-muted text-muted-foreground"
            >
              {automation.kind}
            </Badge>
          ) : null}
          <Badge variant="outline" className="text-muted-foreground">
            {automation.delivery.mode || "none"}
            {automation.delivery.to ? ` → ${automation.delivery.to}` : ""}
          </Badge>
        </div>
      </div>

      {automation.manifest_unreadable ? (
        <p
          data-testid={`automation-broken-${automation.id}`}
          className="rounded-md border border-warning/30 bg-warning/5 p-2 text-xs text-warning"
        >
          This automation&apos;s manifest will not load, so everything above comes from the
          scheduler&apos;s own row. The engine keeps firing the job it already holds — fix the YAML
          and the card fills in on the next scan.
        </p>
      ) : null}

      <div className="flex flex-col gap-0.5">
        <span
          data-testid={`automation-cron-${automation.id}`}
          className="text-xs text-foreground"
        >
          {human === NO_SCHEDULE ? human : `${human}${automation.timezone ? ` · ${automation.timezone}` : ""}`}
        </span>
        <span
          data-testid={`automation-next-${automation.id}`}
          className="text-[11px] text-muted-foreground"
          title={
            automation.next_run_at
              ? `The engine currently holds ${absoluteTime(automation.next_run_at)}`
              : undefined
          }
        >
          Next {nextText}
        </span>
      </div>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        <Cell id={`automation-ran-${automation.id}`} label="Ran" reading={ran} />
        <Cell id={`automation-delivered-${automation.id}`} label="Delivered" reading={delivered} />
        <Cell id={`automation-completed-${automation.id}`} label="Completed" reading={completed} />
      </div>

      {took ? (
        <span className="font-mono text-[11px] text-muted-foreground/70">took {took}</span>
      ) : null}

      {automation.breaker_tripped ? (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-2">
          <ShieldAlert aria-hidden className="size-4 text-destructive" />
          <span
            data-testid={`automation-breaker-${automation.id}`}
            className="text-xs text-destructive"
          >
            Stopped after {automation.consecutive_errors} consecutive failures — the scheduler
            skips it at {automation.breaker_threshold}.
          </span>
          {canWrite ? (
            <Button
              variant="outline"
              size="xs"
              disabled={busy}
              data-testid={`automation-reset-${automation.id}`}
              onClick={() => void onResetBreaker(automation)}
            >
              Reset breaker
            </Button>
          ) : null}
        </div>
      ) : null}

      {canWrite ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            variant="outline"
            size="xs"
            disabled={busy}
            data-testid={`automation-toggle-${automation.id}`}
            onClick={() => void onToggle(automation)}
          >
            {automation.enabled ? <Pause aria-hidden /> : <Play aria-hidden />}
            {automation.enabled ? "Disable" : "Enable"}
          </Button>
          <Button
            variant="outline"
            size="xs"
            disabled={busy}
            data-testid={`automation-run-${automation.id}`}
            onClick={() => void onRunNow(automation)}
          >
            <Rocket aria-hidden />
            Run now
          </Button>
          {automation.editable ? (
            <Button
              variant="ghost"
              size="xs"
              data-testid={`automation-edit-${automation.id}`}
              onClick={() =>
                setEdit(
                  edit
                    ? null
                    : { cron: automation.cron, timezone: automation.timezone, change: "" }
                )
              }
            >
              Edit schedule
            </Button>
          ) : (
            /*
              FORM_OWNED_PATHS covers schedule.cron and schedule.timezone and
              nothing else, so a PATCH cannot move a heartbeat or worker cron —
              and a manifest that will not parse has nothing to edit at all. A
              form that posts and changes nothing is worse than no form.
            */
            <span
              data-testid={`automation-uneditable-${automation.id}`}
              className="text-[11px] text-muted-foreground"
            >
              {automation.manifest_unreadable
                ? "Fix the manifest before editing this schedule."
                : `This cron lives in the manifest's ${automation.kind} block — edit it there.`}
            </span>
          )}
        </div>
      ) : null}

      {edit ? (
        <div className="flex flex-col gap-1.5 rounded-md border border-border bg-background p-2.5">
          <label className="text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
            Cron
            <Input
              className="mt-1"
              aria-label={`Cron expression for ${automation.id}`}
              data-testid={`automation-cron-input-${automation.id}`}
              value={edit.cron}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setEdit({ ...edit, cron: event.target.value })}
            />
          </label>
          <label className="text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
            Timezone
            <Input
              className="mt-1"
              aria-label={`Timezone for ${automation.id}`}
              data-testid={`automation-timezone-input-${automation.id}`}
              value={edit.timezone}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setEdit({ ...edit, timezone: event.target.value })}
            />
          </label>
          <label className="text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
            What changed
            <Input
              className="mt-1"
              aria-label={`Why the schedule for ${automation.id} changed`}
              data-testid={`automation-change-input-${automation.id}`}
              value={edit.change}
              onChange={(event) => setEdit({ ...edit, change: event.target.value })}
            />
          </label>
          {/*
            The preview is the engine's own verdict (cronIsValid), not
            cronstrue's: an expression this reads back happily and the scheduler
            refuses is the save that fails with the form already closed.
          */}
          <span
            data-testid={`automation-edit-preview-${automation.id}`}
            className={`text-xs ${editable ? "text-muted-foreground" : "text-destructive"}`}
          >
            {editPreview}
            {editable && nextRunText(edit.cron, edit.timezone)
              ? ` — next ${nextRunText(edit.cron, edit.timezone)}`
              : ""}
          </span>
          <div className="flex flex-wrap items-center gap-1.5">
            <Button
              size="xs"
              disabled={!editable || busy}
              data-testid={`automation-save-${automation.id}`}
              onClick={() => {
                void onSaveSchedule(automation, edit);
                setEdit(null);
              }}
            >
              Save schedule
            </Button>
            <Button
              variant="ghost"
              size="xs"
              data-testid={`automation-cancel-${automation.id}`}
              onClick={() => setEdit(null)}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : null}

      {note ? (
        <span
          aria-live="polite"
          className="text-xs text-muted-foreground"
          data-testid={`automation-note-${automation.id}`}
        >
          {note}
        </span>
      ) : null}
      {/*
        The engine's half of a manifest write, kept apart from the note above
        and in warning colour. A save that reports success over an engine that
        did not reconcile is the exact failure `agent_manifests` says must not
        happen; B8's panel and this card now read the same field through
        `lib/agents/reconcile.ts`.
      */}
      {reconcile ? (
        <span
          className="text-xs text-warning"
          data-testid={`automation-reconcile-${automation.id}`}
        >
          {reconcile}
        </span>
      ) : null}
      {warnings ? (
        <span
          className="text-xs text-warning"
          data-testid={`automation-warnings-${automation.id}`}
        >
          {warnings}
        </span>
      ) : null}
      {error ? (
        <span className="text-xs text-destructive" data-testid={`automation-error-${automation.id}`}>
          {error}
        </span>
      ) : null}
    </div>
  );
}

export default AutomationCard;
