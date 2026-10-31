"use client";

import { Fragment, useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { StatusBadge, fromEngineStatus } from "@/components/business/status-badge";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import {
  IN_FLIGHT,
  completedReading,
  durationText,
  humanizeStatus,
  runDeliveredReading,
  toneClass,
  verdictTone,
} from "@/lib/automations/run-truth";
import { relativeTime } from "@/lib/inbox/pending";
import { formatCost, formatTokens } from "./runs-format";

const BRIDGE_URL = "/api/bridge";

/** `MAX_TOOL_OUTPUT_CHARS` in `robothor/engine/tracking.py`: outputs are capped when recorded. */
const OUTPUT_CAP = 4000;
/** `robothor/engine/retention.py`: steps are pruned after this many days. */
const STEP_RETENTION_DAYS = 30;


type DetailRun = {
  id: string;
  agent_id?: string;
  trigger_type?: string | null;
  trigger_detail?: string | null;
  status?: string | null;
  started_at?: string | null;
  duration_ms?: number | null;
  model_used?: string | null;
  models_attempted?: string[] | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_creation_tokens?: number | null;
  cache_read_tokens?: number | null;
  total_cost_usd?: number | null;
  error_message?: string | null;
  delivery_status?: string | null;
  delivered_at?: string | null;
  delivery_channel?: string | null;
  verified_status?: string | null;
  outcome_assessment?: string | null;
  outcome_notes?: string | null;
  task_text?: string | null;
};

type Step = {
  step_number: number;
  step_type?: string;
  tool_name?: string | null;
  model?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  duration_ms?: number | null;
  error_message?: string | null;
  tool_input?: unknown;
  tool_output?: unknown;
};

type GEvent = {
  step_number?: number;
  guardrail_name?: string;
  action?: string;
  tool_name?: string;
  reason?: string | null;
};

type Detail = { run: DetailRun; steps: Step[]; guardrail_events: GEvent[] };

/*
  A small JSON/truncation helper of this view's own. The Fleet work has a
  similar one in flight; the two are to be reconciled when both have landed.
*/
export function prettyJson(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

/** The engine's truncation envelope (`_truncate_json`), or null when the output is whole. */
export function truncation(output: unknown): { total: number | null; content: unknown } | null {
  if (output && typeof output === "object" && (output as { _truncated?: unknown })._truncated === true) {
    const o = output as { total_chars?: unknown; content?: unknown };
    return { total: typeof o.total_chars === "number" ? o.total_chars : null, content: o.content };
  }
  if (typeof output === "string" && /\[\.\.\. truncated \d+ chars \.\.\.\]/.test(output)) {
    return { total: null, content: output };
  }
  return null;
}

const fmt = formatTokens;
const present = (v: unknown) => v !== null && v !== undefined && !(typeof v === "object" && Object.keys(v as object).length === 0);

function Field({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="min-w-0" data-testid={testId}>
      <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="break-words text-foreground">{children}</dd>
    </div>
  );
}

function Header({ run }: { run: DetailRun }) {
  const delivery = runDeliveredReading({
    status: run.status ?? null,
    delivery_status: run.delivery_status ?? null,
    delivery_channel: run.delivery_channel ?? null,
    delivered_at: run.delivered_at ?? null,
  });
  const attempted = (run.models_attempted ?? []).filter(Boolean);
  const tokens = [
    `${fmt(run.input_tokens ?? 0)} in`,
    `${fmt(run.output_tokens ?? 0)} out`,
  ];
  if (run.cache_read_tokens) tokens.push(`${fmt(run.cache_read_tokens)} cache read`);
  if (run.cache_creation_tokens) tokens.push(`${fmt(run.cache_creation_tokens)} cache write`);
  const outcome = completedReading({
    id: run.id,
    started_at: null,
    status: null,
    duration_ms: null,
    delivery_status: null,
    delivered_at: null,
    delivery_channel: null,
    delivery_mode: null,
    verified_status: null,
    outcome_assessment: run.outcome_assessment ?? null,
  });
  const started = run.started_at ? new Date(run.started_at) : null;

  return (
    <div data-testid="run-detail-header" className="flex min-w-0 flex-col gap-3 text-xs">
      <dl className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
        <Field label="Agent">{run.agent_id ?? "—"}</Field>
        <Field label="Trigger">
          {run.trigger_type ? humanizeStatus(run.trigger_type) : "—"}
          {run.trigger_detail ? (
            <span className="ml-1 font-mono text-[11px] text-muted-foreground">· {run.trigger_detail}</span>
          ) : null}
        </Field>
        <Field label="Started">
          {started && Number.isFinite(started.getTime()) ? (
            <span title={run.started_at ?? undefined}>
              {started.toLocaleString()} <span className="text-muted-foreground">({relativeTime(run.started_at)})</span>
            </span>
          ) : (
            "—"
          )}
        </Field>
        <Field label="Duration">{durationText(run.duration_ms) ?? "—"}</Field>
        <Field label="Model" testId="run-detail-models">
          <span className="font-mono text-[11px]">{run.model_used || "—"}</span>
          {attempted.length > 1 || (attempted.length === 1 && attempted[0] !== run.model_used) ? (
            <span className="block text-muted-foreground">tried {attempted.join(" → ")}</span>
          ) : null}
        </Field>
        <Field label="Tokens" testId="run-detail-tokens">
          <span className="font-mono text-[11px]">{tokens.join(" · ")}</span>
        </Field>
        <Field label="Cost">
          <span className="font-mono text-[11px]">
            {formatCost(run.total_cost_usd) ?? "—"}
          </span>
        </Field>
        <Field label="Delivery">
          <span data-testid={`run-delivered-detail-${run.id}`} className={toneClass(delivery.tone)}>
            {delivery.text}
          </span>
        </Field>
        <Field label="Verification">
          {run.verified_status ? (
            <span
              data-testid={`run-verified-${run.id}`}
              className={toneClass(verdictTone(run.verified_status))}
            >
              {humanizeStatus(run.verified_status)}
            </span>
          ) : (
            <span className="text-muted-foreground">not verified</span>
          )}
        </Field>
      </dl>
      <div data-testid="run-detail-outcome" className="min-w-0">
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Outcome (self-assessed)</div>
        <div className={`break-words ${toneClass(outcome.tone)}`}>{outcome.text}</div>
        {run.outcome_notes ? (
          <p className="mt-0.5 whitespace-pre-wrap break-words text-muted-foreground">{run.outcome_notes}</p>
        ) : null}
      </div>
      {run.error_message ? (
        <p className="whitespace-pre-wrap break-words rounded-md border border-destructive/30 bg-destructive/10 p-2 text-destructive">
          {run.error_message}
        </p>
      ) : null}
      {run.task_text ? (
        <div data-testid="run-detail-task" className="min-w-0">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Task</div>
          <p className="max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded-md bg-muted/50 p-2 text-foreground">
            {run.task_text}
          </p>
        </div>
      ) : null}
    </div>
  );
}

function StepRows({ step, index }: { step: Step; index: number }) {
  const [open, setOpen] = useState(false);
  const hasIo = present(step.tool_input) || present(step.tool_output);
  const cut = truncation(step.tool_output);
  const tokens = (step.input_tokens ?? 0) + (step.output_tokens ?? 0);
  const failed = Boolean(step.error_message);
  const retried = step.step_type === "llm_call" && failed;
  return (
    <Fragment>
      <tr data-testid={`run-step-${index}`} className="border-t border-border align-top">
        <td className="px-2 py-1.5 font-mono text-muted-foreground">{step.step_number}</td>
        <td className="whitespace-nowrap px-2 py-1.5 text-muted-foreground">
          {step.step_type ? humanizeStatus(step.step_type) : "—"}
        </td>
        <td className="min-w-0 px-2 py-1.5 font-mono text-[11px] text-foreground">
          {hasIo ? (
            <button
              type="button"
              data-testid={`step-io-toggle-${index}`}
              aria-expanded={open}
              onClick={() => setOpen((o) => !o)}
              className="inline-flex items-center gap-1 text-left hover:underline focus-visible:outline-2 focus-visible:outline-ring"
            >
              {open ? <ChevronDown aria-hidden className="size-3" /> : <ChevronRight aria-hidden className="size-3" />}
              <span className="break-all">{step.tool_name || step.model || "input and output"}</span>
            </button>
          ) : (
            <span className="break-all">{step.tool_name || step.model || "—"}</span>
          )}
        </td>
        <td className="whitespace-nowrap px-2 py-1.5 text-muted-foreground">{durationText(step.duration_ms) ?? "—"}</td>
        <td className="whitespace-nowrap px-2 py-1.5 font-mono text-[11px] text-muted-foreground">
          {tokens > 0 ? fmt(tokens) : "—"}
          {step.cache_read_tokens ? <span className="block">{fmt(step.cache_read_tokens)} cached</span> : null}
        </td>
        <td className="px-2 py-1.5">
          {failed ? <span className="text-destructive">error</span> : <span className="text-muted-foreground">ok</span>}
          {/* An llm_call row carrying an error_message is a RETRIED attempt,
              not a second turn: the engine records one row per provider
              attempt. Without the marker two identical `llm_call` lines read
              as duplicate telemetry. */}
          {retried ? (
            <span data-testid={`attempt-${step.step_number}`} className="ml-1 text-warning">
              · attempt failed ({String(step.error_message).split(":")[0]})
            </span>
          ) : null}
          {failed ? (
            <span data-testid={`step-error-${index}`} className="block whitespace-pre-wrap break-words text-destructive">
              {step.error_message}
            </span>
          ) : null}
        </td>
      </tr>
      {open && hasIo ? (
        <tr data-testid={`step-io-${index}`} className="bg-muted/30">
          <td colSpan={6} className="px-2 py-2">
            <div className="flex min-w-0 flex-col gap-2">
              <div className="min-w-0">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Input (redacted when recorded)</div>
                <pre data-testid={`step-input-${index}`} className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2 font-mono text-[11px]">
                  {present(step.tool_input) ? prettyJson(step.tool_input) : "none recorded"}
                </pre>
              </div>
              <div className="min-w-0">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Output</div>
                {cut ? (
                  <p data-testid={`step-truncated-${index}`} className="text-[11px] text-warning">
                    Output capped at {fmt(OUTPUT_CAP)} characters when it was recorded
                    {cut.total ? ` (the full output was ${fmt(cut.total)})` : ""}; the middle is cut.
                  </p>
                ) : null}
                <pre data-testid={`step-output-${index}`} className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2 font-mono text-[11px]">
                  {present(step.tool_output) ? prettyJson(cut ? cut.content : step.tool_output) : "none recorded"}
                </pre>
              </div>
            </div>
          </td>
        </tr>
      ) : null}
    </Fragment>
  );
}

function StepsTable({ steps, label }: { steps: { step: Step; index: number }[]; label: string }) {
  return (
    <div className="min-w-0 overflow-x-auto rounded-lg border border-border">
      <table aria-label={label} className="w-full min-w-[520px] text-left text-xs">
        <thead className="bg-muted/50 text-[10px] uppercase tracking-wide text-muted-foreground">
          <tr>
            {["#", "Kind", "Tool or model", "Duration", "Tokens", "Status"].map((h) => (
              <th key={h} scope="col" className="px-2 py-1.5 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {steps.map(({ step, index }) => (
            <StepRows key={index} step={step} index={index} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Body({ detail }: { detail: Detail }) {
  const { run, steps, guardrail_events: events } = detail;
  const indexed = steps.map((step, index) => ({ step, index }));
  const warmup = indexed.filter(({ step }) => step.step_type === "warmup_phase");
  const main = indexed.filter(({ step }) => step.step_type !== "warmup_phase");
  const finishedEmpty = steps.length === 0 && !IN_FLIGHT.has((run.status ?? "").trim());
  // "Pruned" is only the explanation for a run older than the retention.
  const startedAt = Date.parse(run.started_at ?? "");
  const pastRetention =
    Number.isFinite(startedAt) && Date.now() - startedAt > STEP_RETENTION_DAYS * 864e5;
  const pruned = finishedEmpty && pastRetention;
  const noneRecorded = finishedEmpty && !pastRetention;

  return (
    <div data-testid="run-detail" className="flex min-w-0 flex-col gap-4 break-words px-4 pb-6 text-sm">
      <Header run={run} />

      <section className="flex min-w-0 flex-col gap-2" aria-label="Steps">
        <h3 className="text-xs font-semibold text-foreground">Steps</h3>
        {pruned ? (
          <p data-testid="run-steps-pruned" className="rounded-md bg-muted p-2 text-xs text-muted-foreground">
            Steps were pruned after {STEP_RETENTION_DAYS} days. This run keeps its totals and cost
            here, but not its steps.
          </p>
        ) : null}
        {noneRecorded ? (
          <p data-testid="run-steps-none" className="rounded-md bg-muted p-2 text-xs text-muted-foreground">
            No steps were recorded for this run.
          </p>
        ) : null}
        {warmup.length > 0 ? (
          <details data-testid="run-warmup" className="min-w-0 rounded-lg border border-border">
            <summary className="cursor-pointer px-2 py-1.5 text-xs text-muted-foreground">
              Warm-up ({warmup.length} {warmup.length === 1 ? "step" : "steps"})
            </summary>
            <div className="p-2">
              <StepsTable steps={warmup} label="Warm-up steps" />
            </div>
          </details>
        ) : null}
        {main.length > 0 ? <StepsTable steps={main} label="Run steps" /> : null}
      </section>

      {events.length > 0 ? (
        <section className="flex min-w-0 flex-col gap-1" aria-label="Guardrail events">
          <h3 className="text-xs font-semibold text-foreground">Guardrails</h3>
          {events.map((e, i) => (
            <div
              key={i}
              data-testid={`guardrail-event-${i}`}
              className={`break-words rounded-md px-2.5 py-1.5 text-xs ${
                e.action === "blocked"
                  ? "border border-warning/25 bg-warning/10 text-warning"
                  : "bg-muted text-muted-foreground"
              }`}
            >
              {e.guardrail_name} {e.action} {e.tool_name}
              {typeof e.step_number === "number" ? ` at step ${e.step_number}` : ""}
              {e.reason ? <span className="block text-muted-foreground">{e.reason}</span> : null}
            </div>
          ))}
        </section>
      ) : null}
    </div>
  );
}

/**
 * One run, explained: what it was asked, what it did step by step, what it
 * cost, whether the answer arrived and whether it held up.
 */
export function RunDetailDrawer({ runId, onClose }: { runId: string | null; onClose: () => void }) {
  // Keyed by run id, so a newly chosen run never shows the last one's answer
  // and nothing has to be reset when the choice changes.
  const [loaded, setLoaded] = useState<{ id: string; detail?: Detail; error?: string } | null>(null);
  const current = loaded && loaded.id === runId ? loaded : null;
  const detail = current?.detail ?? null;
  const error = current?.error ?? null;

  useEffect(() => {
    if (!runId) return;
    let active = true;
    (async () => {
      try {
        const res = await fetch(`${BRIDGE_URL}/api/runs/${encodeURIComponent(runId)}`);
        if (!active) return;
        const setError = (message: string) => setLoaded({ id: runId, error: message });
        if (!res.ok) {
          setError(
            res.status === 403
              ? "Operator access is required to read a run."
              : res.status === 404
                ? "This run is gone: it is no longer in the run history."
                : `The bridge answered ${res.status}.`
          );
          return;
        }
        const body = (await res.json()) as Partial<Detail>;
        if (!active) return;
        if (!body?.run) {
          setError("The bridge sent a run this page cannot read.");
          return;
        }
        setLoaded({
          id: runId,
          detail: {
            run: body.run,
            steps: Array.isArray(body.steps) ? body.steps : [],
            guardrail_events: Array.isArray(body.guardrail_events) ? body.guardrail_events : [],
          },
        });
      } catch {
        if (active) setLoaded({ id: runId, error: "Could not reach the bridge." });
      }
    })();
    return () => {
      active = false;
    };
  }, [runId]);

  const run = detail?.run;
  return (
    <Sheet open={runId !== null} onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <SheetContent
        data-testid="run-detail-drawer"
        className="w-full gap-0 overflow-y-auto sm:max-w-2xl"
      >
        <SheetHeader className="pr-10">
          <SheetTitle className="flex min-w-0 flex-wrap items-center gap-2 break-words">
            <span>{run?.agent_id ?? "Run"}</span>
            {run ? <StatusBadge status={fromEngineStatus(run.status)} label={run.status ?? undefined} /> : null}
          </SheetTitle>
          <SheetDescription className="break-all font-mono text-[11px]">{runId ?? ""}</SheetDescription>
        </SheetHeader>
        {error ? (
          <p data-testid="run-detail-error" className="mx-4 rounded-md border border-border bg-muted p-3 text-sm text-muted-foreground">
            {error}
          </p>
        ) : null}
        {!error && !detail && runId ? (
          <div className="flex flex-col gap-2 px-4" aria-hidden>
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        ) : null}
        {detail ? <Body detail={detail} /> : null}
      </SheetContent>
    </Sheet>
  );
}

export default RunDetailDrawer;
