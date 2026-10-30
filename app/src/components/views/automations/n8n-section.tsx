"use client";

import { useCallback, useState } from "react";
import { ExternalLink } from "lucide-react";

import { StatusBadge, type Status } from "@/components/business/status-badge";
import { absoluteTime, relativeTime } from "@/lib/inbox/pending";
import { useBridgePoll } from "@/lib/bridge/use-bridge-poll";

/**
 * External: n8n — the operator's own n8n, as the bridge's poller last saw it.
 *
 * Below Workflows, with its own poll: an n8n listing that is down must not take
 * the automations or the workflows with it, same reasoning as
 * `workflows-section.tsx`.
 *
 * The bridge never calls n8n on this request; it reads what the poller stored,
 * so `status` already carries the honesty rules — `unknown` when the poller has
 * not succeeded recently, `not_configured` when the monitor is off. This
 * component only renders them.
 */

const URL = "/api/bridge/api/integrations/n8n/workflows";
const DOC_PATH = "docs/integrations/n8n.md";

export type N8nStatus = "ok" | "warn" | "unknown" | "not_configured";

export interface N8nWorkflow {
  workflow_id: string;
  name: string;
  active: boolean;
  tags: string[];
  failures_24h: number;
  failures_7d: number;
  last_failure_at: string | null;
  last_error: string | null;
  /** The newest failed execution in n8n, or `""`. */
  last_failure_url: string;
  url: string;
}

export interface N8nListing {
  status: N8nStatus;
  base_url: string;
  last_poll_at: string | null;
  last_ok_at: string | null;
  last_error: string | null;
  workflows: N8nWorkflow[];
}

const TONE: Record<Exclude<N8nStatus, "not_configured">, { status: Status; label: string }> = {
  ok: { status: "ok", label: "OK" },
  warn: { status: "degraded", label: "Warn" },
  unknown: { status: "idle", label: "Unknown" },
};

const STATUSES: readonly N8nStatus[] = ["ok", "warn", "unknown", "not_configured"];

function str(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

/** A defensive read of the body: a malformed field degrades, it never throws. */
export function normalizeN8nListing(body: unknown): N8nListing {
  const b = (body && typeof body === "object" ? body : {}) as Record<string, unknown>;
  const status = STATUSES.includes(b.status as N8nStatus) ? (b.status as N8nStatus) : "unknown";
  const workflows = Array.isArray(b.workflows) ? b.workflows : [];
  return {
    status,
    base_url: str(b.base_url) ?? "",
    last_poll_at: str(b.last_poll_at),
    last_ok_at: str(b.last_ok_at),
    last_error: str(b.last_error),
    workflows: workflows
      .filter((w): w is Record<string, unknown> => !!w && typeof w === "object")
      .filter((w) => typeof w.workflow_id === "string")
      .map((w) => ({
        workflow_id: w.workflow_id as string,
        name: str(w.name) ?? (w.workflow_id as string),
        active: w.active === true,
        tags: Array.isArray(w.tags) ? w.tags.filter((t): t is string => typeof t === "string") : [],
        failures_24h: num(w.failures_24h),
        failures_7d: num(w.failures_7d),
        last_failure_at: str(w.last_failure_at),
        last_error: str(w.last_error),
        last_failure_url: str(w.last_failure_url) ?? "",
        url: str(w.url) ?? "",
      })),
  };
}

export function N8nSection({ visible = true }: { visible?: boolean }) {
  const [listing, setListing] = useState<N8nListing | null>(null);
  const absorb = useCallback((body: unknown) => setListing(normalizeN8nListing(body)), []);
  const notConfigured = listing?.status === "not_configured";
  // A monitor that is switched off stays off until someone changes the
  // instance's settings, so there is nothing for the beat to find. Paused, the
  // hook still reads when the view comes back on screen.
  const { error, forbidden } = useBridgePoll({
    visible,
    url: URL,
    onData: absorb,
    paused: notConfigured,
  });

  const tone = listing && !notConfigured ? TONE[listing.status as keyof typeof TONE] : null;

  return (
    <section className="flex flex-col gap-2" data-testid="n8n-section">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium text-foreground">External: n8n</h3>
          {listing?.last_poll_at ? (
            <p className="text-xs text-muted-foreground" title={absoluteTime(listing.last_poll_at)}>
              Last checked {relativeTime(listing.last_poll_at)}
            </p>
          ) : null}
        </div>
        {tone && !forbidden && !error ? (
          <span data-testid="n8n-status">
            <StatusBadge status={tone.status} label={tone.label} />
          </span>
        ) : null}
      </div>

      {forbidden ? (
        <p className="text-xs text-muted-foreground" data-testid="n8n-forbidden">
          n8n monitoring is operator-only on this appliance, so there is nothing to show here. Ask
          an owner or admin on this instance.
        </p>
      ) : null}

      {error ? (
        <p className="text-sm text-destructive" data-testid="n8n-error">
          {error}
        </p>
      ) : null}

      {!forbidden && !error && notConfigured ? (
        <p className="text-xs text-muted-foreground" data-testid="n8n-not-configured">
          n8n monitoring is not configured. See <code>{DOC_PATH}</code> to set it up.
        </p>
      ) : null}

      {!forbidden && !error && listing?.last_error && !notConfigured ? (
        <p className="text-xs text-warning" data-testid="n8n-last-error">
          Last poll error: {listing.last_error}
        </p>
      ) : null}

      {!forbidden && !error && !notConfigured ? (
        <div className="flex flex-col gap-2">
          {(listing?.workflows ?? []).map((w) => {
            const failing = w.failures_24h > 0;
            return (
              <div
                key={w.workflow_id}
                data-testid="n8n-row"
                className={`flex flex-wrap items-center justify-between gap-2 rounded-lg border p-3 ${
                  failing ? "border-warning/40 bg-warning/5" : "border-border bg-card"
                }`}
              >
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-sm font-semibold text-foreground">{w.name}</span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                      w.active
                        ? "bg-success/10 text-success"
                        : "bg-muted text-muted-foreground"
                    }`}
                  >
                    {w.active ? "active" : "inactive"}
                  </span>
                </div>
                <div className="flex items-center gap-3 font-mono text-xs text-muted-foreground">
                  <span title="Failed executions in the last 24 hours / 7 days">
                    {w.failures_24h} / {w.failures_7d} failed
                  </span>
                  {w.last_failure_at && w.last_failure_url ? (
                    <a
                      data-testid="n8n-last-failure"
                      href={w.last_failure_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={absoluteTime(w.last_failure_at)}
                      className="underline decoration-dotted underline-offset-2 hover:text-foreground"
                    >
                      last {relativeTime(w.last_failure_at)}
                    </a>
                  ) : w.last_failure_at ? (
                    <span data-testid="n8n-last-failure" title={absoluteTime(w.last_failure_at)}>
                      last {relativeTime(w.last_failure_at)}
                    </span>
                  ) : null}
                  {w.url ? (
                    <a
                      href={w.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      aria-label={`Open ${w.name} in n8n`}
                      className="text-muted-foreground hover:text-foreground"
                    >
                      <ExternalLink aria-hidden className="size-3.5" />
                    </a>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}

export default N8nSection;
