"use client";

import type { ReactNode } from "react";
import { RefreshCw, type LucideIcon } from "lucide-react";

import { EmptyState } from "@/components/business/empty-state";
import { ListSkeleton } from "@/components/business/list-skeleton";
import { PageHeader } from "@/components/business/page-header";
import { Button } from "@/components/ui/button";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { BRIDGE_UNREACHABLE } from "@/lib/bridge/use-bridge-poll";

import { NOT_CONNECTED, unavailableText } from "./telemetry-logs";

/**
 * What the Datadog tabs of the Logs page share: one reader for the bridge's
 * telemetry answers, the states it can give in plain words, and the frame each
 * tab draws in (the page header with the source switch, then the tabs).
 *
 * The answers are `crm/bridge/routers/telemetry.py`'s: `ok`, `not_configured`
 * (one sentence; the root at home is also told which half is missing),
 * `unavailable` (a kind, never the vendor's text), `no_access` (this kind of
 * data is not shared with the organization), a 400 or 422 refusal in the
 * bridge's own words, a 403, and a 404 for a single thing that is not there.
 */

export const TELEMETRY_BRIDGE = "/api/bridge";

export type TelemetryState =
  | { kind: "loading" }
  | { kind: "ok" }
  | { kind: "not_configured"; message: string; reason: string | null }
  | { kind: "unavailable"; errorKind: string }
  | { kind: "no_access"; message: string }
  | { kind: "refused"; reason: string }
  | { kind: "not_found"; message: string }
  | { kind: "forbidden" }
  | { kind: "error"; message: string };

export interface TelemetryAnswer {
  state: TelemetryState;
  body: Record<string, unknown>;
}

export function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function records(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (entry): entry is Record<string, unknown> =>
      Boolean(entry) && typeof entry === "object" && !Array.isArray(entry),
  );
}

/** One answer of a telemetry route, as a state and (when ok) its body. */
export async function readTelemetry(url: string): Promise<TelemetryAnswer> {
  const fail = (state: TelemetryState): TelemetryAnswer => ({ state, body: {} });
  let res: Response;
  try {
    res = await fetch(url);
  } catch {
    return fail({ kind: "error", message: BRIDGE_UNREACHABLE });
  }
  if (res.status === 403) return fail({ kind: "forbidden" });
  if (!res.ok) {
    const reply = await readBridgeReply(res);
    if (res.status === 404) return fail({ kind: "not_found", message: reply });
    // 400 is the scope wall's refusal and 422 a value the route could not
    // read: both are the person's to fix, in the bridge's words.
    return fail(
      res.status === 400 || res.status === 422
        ? { kind: "refused", reason: reply }
        : { kind: "error", message: reply },
    );
  }
  let body: Record<string, unknown>;
  try {
    body = ((await res.json()) ?? {}) as Record<string, unknown>;
  } catch {
    return fail({ kind: "error", message: BRIDGE_UNREACHABLE });
  }
  switch (body.status) {
    case "not_configured":
      return fail({
        kind: "not_configured",
        message: str(body.message) || NOT_CONNECTED,
        reason: str(body.reason) || null,
      });
    case "unavailable":
      return fail({ kind: "unavailable", errorKind: str(body.kind) });
    case "no_access":
      return fail({ kind: "no_access", message: str(body.message) });
    default:
      return { state: { kind: "ok" }, body };
  }
}

/** The root operator is told which half is missing; everyone else is not. */
export function notConfiguredHint(reason: string | null): string {
  if (reason === "keys") return "The Datadog API and application keys are not set on this instance.";
  if (reason === "scope") {
    return "This organization has no entry in the Datadog scope map (ROBOTHOR_DATADOG_SCOPES).";
  }
  return "An operator of this instance connects it. Nothing here is broken.";
}

/**
 * Every state that is not `ok`, in plain words, under `<prefix>-<state>` test
 * ids. Returns null for `ok`, so the caller draws its own content then.
 */
export function TelemetryNotice({
  state,
  prefix,
  icon,
  loadingLabel,
  noun,
}: {
  state: TelemetryState;
  prefix: string;
  icon: LucideIcon;
  loadingLabel: string;
  /** What the tab reads, for the forbidden sentence ("these traces"). */
  noun: string;
}) {
  switch (state.kind) {
    case "ok":
      return null;
    case "loading":
      return <ListSkeleton testId={`${prefix}-loading`} label={loadingLabel} />;
    case "forbidden":
      return (
        <p className="text-xs text-muted-foreground" data-testid={`${prefix}-forbidden`}>
          The bridge says {noun} are not yours to read. The page is for owners, admins and auditors
          of this organization.
        </p>
      );
    case "not_configured":
      return (
        <EmptyState
          testId={`${prefix}-not-configured`}
          icon={icon}
          title={state.message}
          description={notConfiguredHint(state.reason)}
        />
      );
    case "no_access":
      return (
        <EmptyState
          testId={`${prefix}-no-access`}
          icon={icon}
          title={state.message || "This kind of Datadog data is not shared with this organization."}
          description="An operator of this instance decides what each organization can see. Nothing here is broken."
        />
      );
    case "unavailable":
      return (
        <div
          data-testid={`${prefix}-unavailable`}
          role="status"
          className="max-w-3xl rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning"
        >
          <p className="font-medium">Datadog is unavailable right now.</p>
          <p className="mt-0.5">{unavailableText(state.errorKind)}</p>
        </div>
      );
    case "refused":
      return (
        <p
          data-testid={`${prefix}-refused`}
          role="alert"
          className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
        >
          That search was refused: {state.reason}
        </p>
      );
    case "not_found":
      return (
        <p
          data-testid={`${prefix}-not-found`}
          className="max-w-3xl rounded-lg border border-border bg-muted p-3 text-xs text-muted-foreground"
        >
          {state.message}
        </p>
      );
    case "error":
      return (
        <p
          data-testid={`${prefix}-error`}
          className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
        >
          {state.message}
        </p>
      );
  }
}

/**
 * The frame every Datadog tab draws in, the same as the Logs tab's: the page
 * header (with the source switch and a Refresh), then whatever the page puts
 * above the tab's content (the organizations summary and the tabs).
 */
export function TelemetryFrame({
  description,
  switcher,
  above,
  onRefresh,
  refreshing,
  refreshTestId,
  children,
}: {
  description: string;
  switcher?: ReactNode;
  above?: ReactNode;
  onRefresh: () => void;
  refreshing: boolean;
  refreshTestId: string;
  children: ReactNode;
}) {
  return (
    <div className="flex h-full min-w-0 flex-col gap-3 overflow-y-auto p-4" data-testid="logs-view">
      <PageHeader title="Logs" description={description} className="flex-wrap">
        {switcher}
        <Button variant="outline" size="sm" data-testid={refreshTestId} onClick={onRefresh}>
          <RefreshCw aria-hidden className={refreshing ? "animate-spin" : undefined} />
          Refresh
        </Button>
      </PageHeader>
      {above}
      {children}
    </div>
  );
}

/** A timestamp as the Logs table shows one. */
export function timeOf(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/** A span's duration from nanoseconds: `0.8 ms`, `9 ms`, `2.3 s`. */
export function durationOf(ns: number | null): string {
  if (ns === null || ns < 0) return "—";
  const ms = ns / 1_000_000;
  if (ms < 1) return `${Math.max(0.1, Math.round(ms * 10) / 10)} ms`;
  if (ms < 1000) return `${Math.round(ms * 10) / 10} ms`;
  return `${Math.round(ms / 100) / 10} s`;
}
