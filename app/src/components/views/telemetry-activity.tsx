"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Activity, ExternalLink } from "lucide-react";

import { EmptyState } from "@/components/business/empty-state";

import {
  TELEMETRY_BRIDGE,
  TelemetryFrame,
  TelemetryNotice,
  num,
  readTelemetry,
  records,
  str,
  timeOf,
  type TelemetryState,
} from "./telemetry-shared";

/**
 * Observe › Logs › Datadog › Activity — what Genus noticed and recorded for
 * this organization (`GET /api/telemetry/signals`): Datadog monitors that
 * changed state, and the watch's error spikes and lost connections. Each one
 * says what happened, when, and what became of it, by the record's `mode`:
 * "Sent" (`delivered`, with the route), "Not sent again: already reported"
 * (`suppressed`), "Could not be sent" (`delivery_failed`), and "Recorded, not
 * sent" for `dry_run` records and any mode this page does not know. The reason
 * is said in plain words. Where a record has no `delivery` or `triage`, the
 * entry falls back to who Genus would have told and what it would have asked.
 */

export interface Signal {
  id: string;
  type: "alert" | "watch";
  timestamp: string;
  sent: boolean;
  mode: string;
  reason: string | null;
  recipients: string;
  deliveryRoute: string | null;
  // alerts
  title: string;
  transition: string;
  priority: string;
  link: string | null;
  level: string;
  wouldRun: { agent: string | null; prompt: string } | null;
  triage: { started: boolean; reason: string | null } | null;
  // watch findings
  finding: string;
  service: string | null;
  count: number | null;
  message: string;
}

function parseSignal(row: Record<string, unknown>, index: number): Signal {
  const run = row.would_run && typeof row.would_run === "object" ? (row.would_run as Record<string, unknown>) : null;
  const triage =
    row.triage && typeof row.triage === "object" && !Array.isArray(row.triage)
      ? (row.triage as Record<string, unknown>)
      : null;
  const link = str(row.link);
  return {
    id: row.id === undefined || row.id === null ? `signal-${index}` : String(row.id),
    type: row.type === "watch" ? "watch" : "alert",
    timestamp: str(row.timestamp),
    sent: row.sent === true && row.mode === "delivered",
    mode: str(row.mode) || "dry_run",
    reason: str(row.reason) || null,
    recipients: str(row.recipients),
    deliveryRoute: str(row.delivery_route) || null,
    title: str(row.title),
    transition: str(row.transition),
    priority: str(row.priority),
    link: link.startsWith("https://") ? link : null,
    level: str(row.level),
    wouldRun: run ? { agent: str(run.agent) || null, prompt: str(run.prompt) } : null,
    triage: triage ? { started: triage.started === true, reason: str(triage.reason) || null } : null,
    finding: str(row.finding),
    service: str(row.service) || null,
    count: num(row.count),
    message: str(row.message),
  };
}

const FINDINGS: Record<string, string> = {
  error_spike: "Error spike",
  connection_lost: "Datadog link lost",
  connection_restored: "Datadog link restored",
};

const REASONS: Record<string, string> = {
  episode_open: "an alert for this is already open",
  cooldown: "too soon after the last one",
  covered_by_webhook: "already reported by the Datadog alert",
  no_engine: "this organization's engine is not running",
  handoff_failed: "the hand-off failed",
};

/** A recorder's reason in plain words; an unknown one (an error kind) as its own words. */
export function reasonText(reason: string | null): string | null {
  if (!reason) return null;
  return REASONS[reason] ?? reason.replace(/_/g, " ");
}

type Outcome = { label: string; tone: "success" | "warning" | "muted" | "destructive"; why: string | null };

/** What became of a record, by its mode; an unknown mode is "Recorded, not sent". */
export function outcomeOf(signal: Pick<Signal, "mode" | "reason" | "sent">): Outcome {
  if (signal.sent) return { label: "Sent", tone: "success", why: null };
  if (signal.mode === "suppressed") {
    return { label: "Not sent again: already reported", tone: "muted", why: reasonText(signal.reason) };
  }
  if (signal.mode === "delivery_failed") {
    return { label: "Could not be sent", tone: "destructive", why: reasonText(signal.reason) };
  }
  return { label: "Recorded, not sent", tone: "warning", why: null };
}

const TONE: Record<Outcome["tone"], string> = {
  success: "border border-success/30 bg-success/10 text-success",
  warning: "border border-warning/30 bg-warning/10 text-warning",
  muted: "border border-border bg-muted text-muted-foreground",
  destructive: "border border-destructive/30 bg-destructive/10 text-destructive",
};

/** What happened, in a few words. */
function headline(signal: Signal): string {
  if (signal.type === "alert") {
    const what = signal.title || "A Datadog monitor";
    return signal.transition ? `${what} — ${signal.transition.replace(/_/g, " ")}` : what;
  }
  const kind = FINDINGS[signal.finding] ?? (signal.finding.replace(/_/g, " ") || "Watch finding");
  return signal.service ? `${kind}: ${signal.service}` : kind;
}

export interface TelemetryActivityProps {
  visible: boolean;
  switcher?: ReactNode;
  above?: ReactNode;
}

export function TelemetryActivity({ visible, switcher, above }: TelemetryActivityProps) {
  const [reloads, setReloads] = useState(0);
  const searchKey = String(reloads);
  const [answered, setAnswered] = useState<{ key: string; state: TelemetryState } | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const generation = useRef(0);
  const sentKey = useRef<string | null>(null);

  useEffect(() => {
    if (!visible || sentKey.current === searchKey) return;
    sentKey.current = searchKey;
    const mine = ++generation.current;
    void readTelemetry(`${TELEMETRY_BRIDGE}/api/telemetry/signals?since=7d`).then((answer) => {
      if (generation.current !== mine) return;
      setAnswered({ key: searchKey, state: answer.state });
      setSignals(records(answer.body.signals).map(parseSignal));
    });
  }, [visible, searchKey]);

  const state: TelemetryState =
    answered && answered.key === searchKey ? answered.state : { kind: "loading" };

  if (!visible) return null;

  return (
    <TelemetryFrame
      description="What Genus noticed in this organization's Datadog data over the last 7 days."
      switcher={switcher}
      above={above}
      onRefresh={() => setReloads((n) => n + 1)}
      refreshing={state.kind === "loading"}
      refreshTestId="activity-refresh"
    >
      {state.kind !== "ok" ? (
        <TelemetryNotice
          state={state}
          prefix="activity"
          icon={Activity}
          loadingLabel="Loading what Genus recorded"
          noun="these records"
        />
      ) : signals.length === 0 ? (
        <EmptyState
          testId="activity-empty"
          icon={Activity}
          title="Nothing recorded in the last 7 days"
          description="No Datadog monitor changed state for this organization, and the watch saw no error spike."
        />
      ) : (
        <ul data-testid="activity-list" className="flex min-w-0 flex-col gap-2">
          {signals.map((signal) => {
            const outcome = outcomeOf(signal);
            return (
            <li
              key={`${signal.type}-${signal.id}`}
              data-testid={`activity-item-${signal.id}`}
              className="min-w-0 rounded-lg border border-border bg-card p-3 text-xs"
            >
              <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                  {signal.type === "alert" ? "Monitor" : "Watch"}
                </span>
                <span className="min-w-0 break-words font-medium text-foreground">{headline(signal)}</span>
                <span className="tabular-nums text-[11px] text-muted-foreground" title={signal.timestamp}>
                  {timeOf(signal.timestamp)}
                </span>
                <span
                  data-testid="activity-status"
                  className={`ml-auto rounded px-1.5 py-0.5 text-[10.5px] font-medium ${TONE[outcome.tone]}`}
                >
                  {outcome.label}
                </span>
              </div>
              {outcome.why ? (
                <p className="mt-1 text-[11px] text-muted-foreground">Because {outcome.why}.</p>
              ) : null}

              {signal.type === "alert" && (signal.priority || signal.level || signal.link) ? (
                <p className="mt-1 flex flex-wrap items-center gap-x-2 text-[11px] text-muted-foreground">
                  {signal.priority ? <span>Priority {signal.priority}</span> : null}
                  {signal.level ? <span>Level {signal.level}</span> : null}
                  {signal.link ? (
                    <a
                      href={signal.link}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="inline-flex items-center gap-1 text-primary underline-offset-2 hover:underline"
                    >
                      Open in Datadog
                      <ExternalLink aria-hidden className="size-3" />
                    </a>
                  ) : null}
                </p>
              ) : null}

              <dl className="mt-2 grid min-w-0 grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                <dt className="text-muted-foreground">{signal.sent ? "Sent to" : "Would have told"}</dt>
                <dd className="min-w-0 break-words text-foreground">
                  {signal.recipients || "—"}
                  {signal.sent && signal.deliveryRoute ? (
                    <span className="text-muted-foreground"> (by {signal.deliveryRoute})</span>
                  ) : null}
                </dd>
                {signal.message ? (
                  <>
                    <dt className="text-muted-foreground">{signal.sent ? "Message" : "Would have sent"}</dt>
                    <dd className="min-w-0 whitespace-pre-wrap break-words text-foreground">{signal.message}</dd>
                  </>
                ) : null}
                {signal.triage ? (
                  <>
                    <dt className="text-muted-foreground">First look</dt>
                    <dd className="min-w-0 break-words text-foreground">
                      {signal.triage.started
                        ? "First-look run started (read-only)."
                        : `No first-look run${signal.triage.reason ? `: ${reasonText(signal.triage.reason)}` : "."}`}
                    </dd>
                  </>
                ) : signal.wouldRun ? (
                  <>
                    <dt className="text-muted-foreground">Would have asked</dt>
                    <dd className="min-w-0 break-words text-foreground">
                      {signal.wouldRun.agent ? `${signal.wouldRun.agent}, read-only: ` : "A read-only run: "}
                      <span className="whitespace-pre-wrap">{signal.wouldRun.prompt}</span>
                    </dd>
                  </>
                ) : null}
              </dl>
            </li>
            );
          })}
        </ul>
      )}

      <p className="shrink-0 text-[10px] text-muted-foreground">
        These are Genus&rsquo;s own records from its audit log. Each says whether it was sent; one
        recorded in dry-run mode was delivered to nobody.
      </p>
    </TelemetryFrame>
  );
}

export default TelemetryActivity;
