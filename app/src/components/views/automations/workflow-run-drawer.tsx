"use client";

import { useState } from "react";
import { ListX } from "lucide-react";
import { EmptyState } from "@/components/business/empty-state";
import { StatusBadge, fromEngineStatus } from "@/components/business/status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { durationText } from "@/lib/automations/run-truth";
import {
  OPERATOR_ONLY,
  prettyJson,
  runUrl,
  stepTally,
  truncationOf,
  type WorkflowRunDetail,
  type WorkflowRunStep,
} from "@/lib/automations/workflow-runs";
import { useBridgePoll } from "@/lib/bridge/use-bridge-poll";
import { absoluteTime, relativeTime } from "@/lib/inbox/pending";

/**
 * One workflow run, step by step, in a sheet over the page.
 *
 * Each step shows its tool name, status and error, and its recorded input and
 * output as collapsible, pretty-printed JSON. A failed step's payloads start
 * open — they are what the operator came for — and the rest start closed. A
 * payload the engine truncated at its size cap is shown as its preview with a
 * note saying so, never as the marker object.
 *
 * The sheet is full width on a phone and wraps long values rather than
 * scrolling sideways, so a recorded payload cannot widen the page.
 */
export function WorkflowRunDrawer({
  workflowId,
  runId,
  open,
  onOpenChange,
}: {
  workflowId: string;
  runId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        data-testid="workflow-run-drawer"
        className="@container w-full gap-0 overflow-y-auto overflow-x-hidden sm:max-w-xl"
      >
        <SheetHeader className="gap-1 border-b border-border px-4 py-3 pr-10">
          <SheetTitle className="break-all text-sm">{workflowId}</SheetTitle>
          <SheetDescription className="break-all font-mono text-xs">Run {runId}</SheetDescription>
        </SheetHeader>
        {/* Keyed by run: another run starts from its own loading state rather
            than showing the last run's steps until the new ones arrive. The
            sheet mounts its content only while open, so a closed drawer reads
            nothing. */}
        {runId ? <RunDetail key={runId} workflowId={workflowId} runId={runId} /> : null}
      </SheetContent>
    </Sheet>
  );
}

function RunDetail({ workflowId, runId }: { workflowId: string; runId: string }) {
  const [detail, setDetail] = useState<WorkflowRunDetail | null>(null);
  // One read, no beat: a run the operator is reading should not reflow under them.
  const { loading, error, forbidden, status } = useBridgePoll({
    visible: true,
    paused: true,
    url: runUrl(workflowId, runId),
    onData: (body) => setDetail(body as WorkflowRunDetail),
  });
  const refusal = forbidden ? OPERATOR_ONLY : status === 404 ? "Run not found." : error;

  const steps = Array.isArray(detail?.steps) ? detail.steps : [];
  const tally = detail ? stepTally(detail) : null;
  const duration = durationText(detail?.duration_ms);

  return (
    <div className="flex flex-col gap-3 px-4 py-3">
      {detail ? (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            <StatusBadge
              status={fromEngineStatus(detail.status)}
              label={detail.status ?? undefined}
            />
            {detail.started_at ? (
              <time
                dateTime={detail.started_at}
                title={absoluteTime(detail.started_at)}
                className="text-muted-foreground"
              >
                {relativeTime(detail.started_at)}
              </time>
            ) : null}
            {duration ? <span className="font-mono text-muted-foreground">{duration}</span> : null}
            {tally ? <span className="font-mono text-muted-foreground">{tally}</span> : null}
          </div>
          {detail.error_message ? (
            <p
              data-testid="run-drawer-error-message"
              className="whitespace-pre-wrap break-words text-xs text-destructive"
            >
              {detail.error_message}
            </p>
          ) : null}
        </div>
      ) : null}

      {loading && (
        <div data-testid="run-drawer-loading" className="flex flex-col gap-2" aria-hidden>
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </div>
      )}

      {refusal && (
        <p data-testid="run-drawer-error" className="text-sm text-destructive">
          {refusal}
        </p>
      )}

      {detail && steps.length === 0 && (
        <EmptyState
          testId="run-drawer-empty"
          icon={ListX}
          title="No steps recorded"
          description="This run ended before any step was recorded."
        />
      )}

      {steps.length > 0 && (
        <ol className="flex flex-col gap-2">
          {steps.map((step) => (
            <StepCard key={step.step_id} step={step} />
          ))}
        </ol>
      )}
    </div>
  );
}

function StepCard({ step }: { step: WorkflowRunStep }) {
  const failed = fromEngineStatus(step.status) === "failing";
  const duration = durationText(step.duration_ms);
  const output = step.tool_output ?? step.output_text;
  return (
    <li
      data-testid={`run-step-${step.step_id}`}
      className={`min-w-0 rounded-md border p-3 ${
        failed ? "border-destructive/40 bg-destructive/5" : "border-border bg-card"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-col">
          <span className="break-all font-mono text-sm font-medium text-foreground">
            {step.tool_name ?? step.agent_id ?? step.step_id}
          </span>
          {step.tool_name !== step.step_id ? (
            <span className="break-all text-xs text-muted-foreground">
              step {step.step_id}
              {step.step_type ? ` · ${step.step_type}` : ""}
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2 text-xs">
          {duration ? <span className="font-mono text-muted-foreground">{duration}</span> : null}
          <StatusBadge status={fromEngineStatus(step.status)} label={step.status ?? undefined} />
        </div>
      </div>

      {step.error_message ? (
        <p
          data-testid={`run-step-error-${step.step_id}`}
          className="mt-2 whitespace-pre-wrap break-words text-xs text-destructive"
        >
          {step.error_message}
        </p>
      ) : null}

      <div className="mt-2 grid gap-2 @[36rem]:grid-cols-2">
        <Payload
          kind="input"
          stepId={step.step_id}
          value={step.tool_input}
          defaultOpen={failed}
        />
        <Payload kind="output" stepId={step.step_id} value={output} defaultOpen={failed} />
      </div>
    </li>
  );
}

function Payload({
  kind,
  stepId,
  value,
  defaultOpen,
}: {
  kind: "input" | "output";
  stepId: string;
  value: unknown;
  defaultOpen: boolean;
}) {
  if (value === null || value === undefined) {
    return <p className="text-xs text-muted-foreground">No {kind} recorded.</p>;
  }
  const truncated = truncationOf(value);
  const text = truncated ? truncated.preview : prettyJson(value);
  return (
    <details
      data-testid={`run-step-${kind}-${stepId}`}
      open={defaultOpen}
      className="min-w-0 rounded border border-border bg-muted/40"
    >
      <summary className="cursor-pointer select-none px-2 py-1 text-xs font-medium text-muted-foreground">
        {kind === "input" ? "Input" : "Output"}
      </summary>
      {truncated ? (
        <p
          data-testid={`run-step-${kind}-truncated-${stepId}`}
          className="px-2 pb-1 text-xs text-warning"
        >
          Truncated: {truncated.originalBytes.toLocaleString("en-US")} bytes recorded, showing the
          start.
        </p>
      ) : null}
      <pre className="max-h-80 overflow-y-auto whitespace-pre-wrap wrap-anywhere px-2 pb-2 font-mono text-[11px] leading-relaxed text-foreground">
        {text}
      </pre>
    </details>
  );
}
