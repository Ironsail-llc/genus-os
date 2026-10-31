"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Activity } from "lucide-react";
import { PageHeader } from "@/components/business/page-header";
import { EmptyState } from "@/components/business/empty-state";
import { MetricGrid } from "@/components/business/metric-grid";
import { StatusBadge, fromEngineStatus } from "@/components/business/status-badge";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  durationText,
  humanizeStatus,
  runDeliveredReading,
  toneClass,
  verdictTone,
} from "@/lib/automations/run-truth";
import { relativeTime } from "@/lib/inbox/pending";
import { RunDetailDrawer } from "./run-detail-drawer";
import { formatCost, formatTokens } from "./runs-format";

const BRIDGE_URL = "/api/bridge";

/** One page of the list; a full page is the only evidence there is another. */
const PAGE = 50;

/** Run statuses from `robothor/engine/models.py` `RunStatus`. */
const STATUSES = [
  "completed",
  "failed",
  "timeout",
  "cancelled",
  "skipped",
  "running",
  "pending",
  "awaiting_approval",
];

/** Trigger types from `robothor/engine/models.py` `TriggerType`. */
const TRIGGERS = [
  "cron",
  "hook",
  "event",
  "manual",
  "telegram",
  "webchat",
  "slack",
  "workflow",
  "sub_agent",
  "federation",
  "webhook",
  "ide",
  "channel_event",
  "channel",
];

/** Period presets, in hours. "all" sends no `since`. */
const PERIODS: { value: string; label: string }[] = [
  { value: "24", label: "Last 24 hours" },
  { value: "168", label: "Last 7 days" },
  { value: "720", label: "Last 30 days" },
  { value: "all", label: "All time" },
];
const DEFAULT_PERIOD = "168";

/*
  `delivery_status`, `delivered_at`, `delivery_channel` and `verified_status`
  arrive on every run row (the bridge's `_RUN_COLUMNS`). "Ran", "delivered" and
  "verified" are three different facts and the table gives each its own column;
  a run with no delivery recorded says nothing rather than claiming one failed.
*/
export type Run = {
  id: string;
  agent_id?: string;
  trigger_type?: string | null;
  status?: string | null;
  started_at?: string | null;
  duration_ms?: number | null;
  model_used?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_cost_usd?: number | null;
  delivery_status?: string | null;
  delivered_at?: string | null;
  delivery_channel?: string | null;
  verified_status?: string | null;
};

type Summary = {
  total: number;
  failed: number;
  delivered: number;
  cost: number;
  agents: string[];
};

type Filters = { status: string; agent: string; trigger: string; period: string };

/** `GET /api/runs/summary`, defensively: a malformed body is "no summary". */
function normalizeSummary(body: unknown): Summary | null {
  const b = body as {
    total?: unknown;
    by_status?: Record<string, unknown>;
    by_delivery?: Record<string, unknown>;
    by_agent?: { agent_id?: unknown }[];
    total_cost_usd?: unknown;
  } | null;
  if (!b || typeof b !== "object" || typeof b.total !== "number") return null;
  const n = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  return {
    total: b.total,
    failed: n(b.by_status?.failed) + n(b.by_status?.timeout),
    delivered: n(b.by_delivery?.delivered),
    cost: n(b.total_cost_usd),
    agents: Array.isArray(b.by_agent)
      ? b.by_agent.map((a) => a?.agent_id).filter((a): a is string => typeof a === "string")
      : [],
  };
}

function query(filters: Filters, extra: Record<string, number> = {}): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(extra)) params.set(k, String(v));
  if (filters.status) params.set("status", filters.status);
  if (filters.agent) params.set("agent", filters.agent);
  if (filters.trigger) params.set("trigger", filters.trigger);
  if (filters.period !== "all") params.set("since", filters.period);
  return params.toString();
}

function errorText(status: number): string {
  return status === 403 ? "Operator access is required to read runs." : `Error ${status}`;
}

export function RunsView({ visible = true }: { visible?: boolean }) {
  const [filters, setFilters] = useState<Filters>({
    status: "",
    agent: "",
    trigger: "",
    period: DEFAULT_PERIOD,
  });
  const [runs, setRuns] = useState<Run[]>([]);
  const [pageFull, setPageFull] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [agents, setAgents] = useState<string[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  // Only the newest request may write: a slow answer to an old filter must
  // not overwrite the list for the filter the operator is looking at.
  const generation = useRef(0);

  useEffect(() => {
    if (!visible) return;
    const mine = ++generation.current;
    const current = () => generation.current === mine;
    setLoading(true);
    (async () => {
      try {
        const res = await fetch(`${BRIDGE_URL}/api/runs?${query(filters, { limit: PAGE, offset: 0 })}`);
        if (!current()) return;
        if (!res.ok) {
          setError(errorText(res.status));
          setRuns([]);
          return;
        }
        const data = await res.json();
        if (!current()) return;
        const rows = Array.isArray(data) ? (data as Run[]) : [];
        setRuns(rows);
        setPageFull(rows.length === PAGE);
        setError(null);
      } catch {
        if (current()) setError("Could not reach the bridge.");
      } finally {
        if (current()) setLoading(false);
      }
    })();
    (async () => {
      try {
        const res = await fetch(`${BRIDGE_URL}/api/runs/summary?${query(filters)}`);
        if (!current()) return;
        const next = res.ok ? normalizeSummary(await res.json()) : null;
        if (!current()) return;
        setSummary(next);
        if (next) {
          setAgents((known) => Array.from(new Set([...known, ...next.agents])).sort());
        }
      } catch {
        if (current()) setSummary(null);
      }
    })();
  }, [visible, filters]);

  const loadMore = useCallback(async () => {
    const mine = generation.current;
    setLoadingMore(true);
    try {
      const res = await fetch(
        `${BRIDGE_URL}/api/runs?${query(filters, { limit: PAGE, offset: runs.length })}`
      );
      if (generation.current !== mine) return;
      if (!res.ok) {
        setError(errorText(res.status));
        return;
      }
      const data = await res.json();
      if (generation.current !== mine) return;
      const rows = Array.isArray(data) ? (data as Run[]) : [];
      // Runs that started since the first page shift the window, so an offset
      // page can repeat rows already shown; keep the first copy of each.
      setRuns((prev) => {
        const seen = new Set(prev.map((r) => r.id));
        return [...prev, ...rows.filter((r) => !seen.has(r.id))];
      });
      setPageFull(rows.length === PAGE);
    } catch {
      if (generation.current === mine) setError("Could not reach the bridge.");
    } finally {
      setLoadingMore(false);
    }
  }, [filters, runs.length]);

  const setFilter = (key: keyof Filters) => (event: React.ChangeEvent<HTMLSelectElement>) =>
    setFilters((f) => ({ ...f, [key]: event.target.value }));

  const periodLabel = PERIODS.find((p) => p.value === filters.period)?.label.toLowerCase();
  const dash = "—";
  const agentOptions =
    filters.agent && !agents.includes(filters.agent) ? [...agents, filters.agent] : agents;

  return (
    <div
      data-testid="runs-view"
      className="h-full min-w-0 flex-col gap-4 overflow-y-auto p-4"
      style={{ display: visible ? "flex" : "none" }}
    >
      <PageHeader
        title="Runs"
        description="Every agent run: what it cost, and whether its answer arrived."
      />

      <section data-testid="runs-summary" aria-label={`Runs, ${periodLabel}`} className="min-w-0">
        <MetricGrid
          metrics={[
            { title: "Runs", value: summary ? summary.total : dash, description: periodLabel },
            { title: "Failed", value: summary ? summary.failed : dash, description: "failed or timed out" },
            { title: "Delivered", value: summary ? summary.delivered : dash, description: "answers that arrived" },
            { title: "Cost", value: summary ? (formatCost(summary.cost, 2) ?? dash) : dash, description: periodLabel },
          ]}
        />
      </section>

      <div className="flex min-w-0 flex-wrap items-center gap-2" data-testid="runs-filters">
        <NativeSelect
          data-testid="runs-filter-status"
          aria-label="Status"
          value={filters.status}
          onChange={setFilter("status")}
        >
          <option value="">Any status</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {humanizeStatus(s)}
            </option>
          ))}
        </NativeSelect>
        <NativeSelect
          data-testid="runs-filter-agent"
          aria-label="Agent"
          value={filters.agent}
          onChange={setFilter("agent")}
        >
          <option value="">Any agent</option>
          {agentOptions.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </NativeSelect>
        <NativeSelect
          data-testid="runs-filter-trigger"
          aria-label="Trigger"
          value={filters.trigger}
          onChange={setFilter("trigger")}
        >
          <option value="">Any trigger</option>
          {TRIGGERS.map((t) => (
            <option key={t} value={t}>
              {humanizeStatus(t)}
            </option>
          ))}
        </NativeSelect>
        <NativeSelect
          data-testid="runs-filter-period"
          aria-label="Period"
          value={filters.period}
          onChange={setFilter("period")}
        >
          {PERIODS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </NativeSelect>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {loading && runs.length === 0 && !error && (
        <div data-testid="runs-loading" className="flex w-full flex-col gap-2" aria-hidden>
          {[0, 1, 2].map((i) => (
            <div key={i} className="rounded-lg border border-border bg-card p-3">
              <Skeleton className="h-4 w-2/3" />
            </div>
          ))}
        </div>
      )}

      {!loading && !error && runs.length === 0 && (
        <EmptyState
          testId="runs-empty"
          icon={Activity}
          title="No runs match"
          description="Runs appear here as agents execute. Widen the period or clear a filter to see more."
        />
      )}

      {runs.length > 0 && (
        <div className="min-w-0 overflow-x-auto rounded-lg border border-border">
          <table data-testid="runs-table" className="w-full min-w-[960px] text-left text-xs">
            <thead className="bg-muted/50 text-[10px] uppercase tracking-wide text-muted-foreground">
              <tr>
                {[
                  "Agent",
                  "Trigger",
                  "Status",
                  "Started",
                  "Duration",
                  "Model",
                  "Tokens",
                  "Cost",
                  "Delivery",
                  "Verification",
                ].map((h) => (
                  <th key={h} scope="col" className="px-2 py-1.5 font-medium">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => {
                const delivery = r.delivery_status ? runDeliveredReading({
                  status: r.status ?? null,
                  delivery_status: r.delivery_status,
                  delivery_channel: r.delivery_channel ?? null,
                  delivered_at: r.delivered_at ?? null,
                }) : null;
                const tokens = (r.input_tokens ?? 0) + (r.output_tokens ?? 0);
                return (
                  <tr
                    key={r.id}
                    data-testid={`run-row-${r.id}`}
                    tabIndex={0}
                    aria-label={`Open run ${r.id}`}
                    onClick={() => setOpenId(r.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setOpenId(r.id);
                      }
                    }}
                    className="cursor-pointer border-t border-border align-top transition-colors hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring"
                  >
                    <td className="px-2 py-1.5 font-medium text-foreground">{r.agent_id ?? dash}</td>
                    <td className="px-2 py-1.5 text-muted-foreground">
                      {r.trigger_type ? humanizeStatus(r.trigger_type) : dash}
                    </td>
                    <td className="px-2 py-1.5">
                      <StatusBadge status={fromEngineStatus(r.status)} label={r.status ?? undefined} />
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-muted-foreground" title={r.started_at ?? undefined}>
                      {relativeTime(r.started_at) || dash}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 text-muted-foreground">
                      {durationText(r.duration_ms) ?? dash}
                    </td>
                    <td className="max-w-[12rem] truncate px-2 py-1.5 font-mono text-[11px] text-muted-foreground" title={r.model_used ?? undefined}>
                      {r.model_used || dash}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 font-mono text-[11px] text-muted-foreground">
                      {tokens > 0 ? formatTokens(tokens) : dash}
                    </td>
                    <td className="whitespace-nowrap px-2 py-1.5 font-mono text-[11px] text-muted-foreground">
                      {formatCost(r.total_cost_usd) ?? dash}
                    </td>
                    <td className="px-2 py-1.5">
                      {delivery ? (
                        <span data-testid={`run-delivered-${r.id}`} className={toneClass(delivery.tone)}>
                          {delivery.text}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">{dash}</span>
                      )}
                    </td>
                    <td className="px-2 py-1.5">
                      {r.verified_status ? (
                        <span
                          data-testid={`run-verification-${r.id}`}
                          className={toneClass(verdictTone(r.verified_status))}
                        >
                          {humanizeStatus(r.verified_status)}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">{dash}</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {pageFull && (
        <div className="flex justify-center pb-2">
          <Button
            variant="outline"
            size="sm"
            data-testid="runs-more"
            disabled={loadingMore}
            onClick={() => void loadMore()}
          >
            {loadingMore ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}

      <RunDetailDrawer runId={visible ? openId : null} onClose={() => setOpenId(null)} />
    </div>
  );
}

export default RunsView;
