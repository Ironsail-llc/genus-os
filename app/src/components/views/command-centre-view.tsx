"use client";

/**
 * Command centre: one card per company for a parent tenant's owner or admin.
 *
 * Reads `GET /api/org/overview` through the BFF proxy. The bridge decides who
 * may read it (an owner or admin at home) and which companies are in it (the
 * home tenant and its descendants); this view only lays the answer out, with
 * the companies needing attention first.
 *
 * A card opens its company through the same switch the tenant picker uses,
 * and only when the bridge says switching is on; otherwise it is not a link.
 *
 * Every read of the overview writes an audit row in each child company, so
 * the page reads it when opened (if its last answer is over a minute old) and
 * when Refresh is clicked: never on tab focus, reconnect or a timer. The as-of time says how old it is.
 *
 * Beside it, the page reads `GET /api/org/connections` the same way: each child
 * company's plugins, their connection checks and its remote tools, shown as a
 * Connected block on that company's card. It is extra: when it is refused or
 * fails (a bridge without the route answers 404), the cards read exactly as
 * they would without it, with no error. The first load waits for both, so a
 * card never grows a block after it is drawn.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Building2, ChevronRight, RefreshCw } from "lucide-react";

import { EmptyState } from "@/components/business/empty-state";
import { PageHeader } from "@/components/business/page-header";
import { StatusBadge, type Status } from "@/components/business/status-badge";
import { useTenantSwitch } from "@/components/layout/tenant-switcher";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAppQueryClient } from "@/lib/query/query-client";
import { reloadPage } from "@/lib/reload-page";

import {
  companyStatus,
  connectionsByTenant,
  countRemoteTools,
  failureReasonLabel,
  formatCost,
  formatRate,
  sortCompanies,
  statusLabel,
  summariseChecks,
  type CompanyConnections,
  type CompanyEntry,
  type OrgConnections,
  type OrgOverview,
} from "./command-centre-format";

const OVERVIEW_URL = "/api/bridge/api/org/overview";
const OVERVIEW_KEY = ["bridge", "org", "overview"] as const;
/**
 * Re-opening the page within a minute shows the answer already read. Nothing
 * else re-reads it but Refresh, because each read is audited in every company.
 */
const OVERVIEW_STALE_MS = 60_000;
const CONNECTIONS_URL = "/api/bridge/api/org/connections";
const CONNECTIONS_KEY = ["bridge", "org", "connections"] as const;

class OverviewRefused extends Error {}

async function fetchOverview(signal: AbortSignal): Promise<OrgOverview> {
  let res: Response;
  try {
    res = await fetch(OVERVIEW_URL, { signal });
  } catch (err) {
    if (signal.aborted) throw err;
    throw new OverviewRefused("Could not reach the bridge.");
  }
  if (!res.ok) {
    throw new OverviewRefused(
      res.status === 403
        ? "The command centre is for an owner or admin of a parent tenant, signed in to their own tenant. If you are acting in a company, switch back first."
        : `Error ${res.status}`,
    );
  }
  let body: unknown;
  try {
    body = await res.json();
  } catch (err) {
    if (signal.aborted) throw err;
    throw new OverviewRefused("Could not reach the bridge.");
  }
  const reply = (body ?? {}) as Partial<OrgOverview>;
  return {
    as_of: typeof reply.as_of === "string" ? reply.as_of : "",
    home_tenant_id: reply.home_tenant_id,
    has_children: reply.has_children === true,
    switching_enabled: reply.switching_enabled === true,
    tenants: Array.isArray(reply.tenants) ? reply.tenants : [],
  };
}

/**
 * The connections, or `null` when they cannot be had: refused, missing on an
 * older bridge, unreachable or malformed. Never an error, so it never shows one.
 */
async function fetchConnections(signal: AbortSignal): Promise<OrgConnections | null> {
  try {
    const res = await fetch(CONNECTIONS_URL, { signal });
    if (!res.ok) return null;
    const reply = ((await res.json()) ?? {}) as Partial<OrgConnections>;
    if (!Array.isArray(reply.tenants)) return null;
    return {
      as_of: typeof reply.as_of === "string" ? reply.as_of : "",
      home_tenant_id: reply.home_tenant_id,
      tenants: reply.tenants.filter((t) => t && typeof t.tenant_id === "string"),
    };
  } catch (err) {
    if (signal.aborted) throw err;
    return null;
  }
}

/**
 * After a switch, reload onto the company's own home screen: every view must
 * refetch as the new tenant, and this page is refused while acting.
 */
function openHome() {
  window.history.replaceState(null, "", "/");
  reloadPage();
}

const BADGE_STATUS: Record<string, Status> = {
  ok: "ok",
  attention: "degraded",
  no_activity: "idle",
  error: "failing",
};

function formatTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function days(n: number | null | undefined): string {
  if (n === null || n === undefined) return "? days";
  return `${n} day${n === 1 ? "" : "s"}`;
}

function late(minutes: number): string {
  return minutes >= 120 ? `${Math.floor(minutes / 60)} h late` : `${minutes} min late`;
}

function Metric({ testId, label, value, hint }: { testId: string; label: string; value: string; hint?: string }) {
  return (
    <div data-testid={testId} className="min-w-0 rounded-md border border-border bg-background/40 px-2.5 py-1.5">
      <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="font-mono text-sm text-foreground">
        {value}
        {hint && <span className="block text-[11px] text-muted-foreground">{hint}</span>}
      </div>
    </div>
  );
}

function Section({ testId, title, children }: { testId: string; title: string; children: React.ReactNode }) {
  return (
    <div data-testid={testId} className="flex flex-col gap-1">
      <div className="text-[11px] font-medium text-muted-foreground">{title}</div>
      <ul className="flex flex-col gap-0.5 text-xs text-foreground">{children}</ul>
    </div>
  );
}

function CompanyFigures({ entry }: { entry: CompanyEntry }) {
  const failures = entry.top_failures ?? [];
  const stuck = entry.agents_with_consecutive_errors ?? [];
  const overdue = entry.overdue_schedules ?? [];
  const tasks = entry.tasks ?? { open: 0, overdue: 0, oldest_overdue: [] };
  return (
    <>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-2 2xl:grid-cols-4">
        <Metric testId="runs-24h" label="Runs 24h" value={String(entry.runs_24h ?? 0)} />
        <Metric testId="failures-24h" label="Failures 24h" value={String(entry.failures_24h ?? 0)} />
        <Metric testId="success-7d" label="Success 7d" value={formatRate(entry.success_rate_7d)} />
        <Metric
          testId="cost"
          label="Cost 7d"
          value={formatCost(entry.cost_usd_7d)}
          hint={`${formatCost(entry.cost_usd_24h)} in 24h`}
        />
      </div>

      {failures.length > 0 && (
        <Section testId="top-failures" title="Failing agents (24h)">
          {failures.map((f) => (
            <li key={`${f.agent_id}-${f.error_type}`} className="flex flex-wrap gap-x-1.5">
              <span className="font-mono">{f.agent_id}</span>
              <span className="text-muted-foreground">{failureReasonLabel(f.error_type)}</span>
              <span className="text-muted-foreground">× {f.count ?? 0}</span>
            </li>
          ))}
        </Section>
      )}

      {stuck.length > 0 && (
        <Section testId="consecutive-errors" title="Agents failing in a row">
          {stuck.map((s) => (
            <li key={s.agent_id} className="flex flex-wrap gap-x-1.5">
              <span className="font-mono">{s.agent_id}</span>
              <span className="text-muted-foreground">{s.consecutive_errors} errors in a row</span>
            </li>
          ))}
        </Section>
      )}

      {overdue.length > 0 && (
        <Section testId="overdue-schedules" title="Overdue schedules">
          {overdue.map((s) => (
            <li key={s.agent_id} className="flex flex-wrap gap-x-1.5">
              <span className="font-mono">{s.agent_id}</span>
              <span className="text-muted-foreground">{late(s.overdue_minutes)}</span>
            </li>
          ))}
        </Section>
      )}

      <div className="flex flex-col gap-1">
        <div data-testid="tasks" className="text-xs text-foreground">
          Tasks: {tasks.open} open · {tasks.overdue} overdue
        </div>
        {tasks.oldest_overdue.length > 0 && (
          <ul data-testid="oldest-overdue" className="flex flex-col gap-0.5 text-xs">
            {tasks.oldest_overdue.map((t, i) => (
              <li key={t.id ?? i} className="flex flex-wrap gap-x-1.5">
                <span className="min-w-0 break-words text-foreground">{t.title ?? "Untitled task"}</span>
                <span className="text-muted-foreground">
                  {days(t.overdueDays)} overdue · {days(t.ageDays)} old
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div data-testid="last-activity" className="text-[11px] text-muted-foreground">
        Last activity{" "}
        {entry.last_activity_at ? (
          <time dateTime={entry.last_activity_at}>{formatTime(entry.last_activity_at)}</time>
        ) : (
          "never"
        )}
      </div>
    </>
  );
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** What one company has connected: plugins, their checks, its remote tools. */
function ConnectedBlock({ connections }: { connections: CompanyConnections }) {
  const headingId = `connected-${connections.tenant_id}`;
  const heading = (
    <h4 id={headingId} className="text-[11px] font-medium text-muted-foreground">
      Connected
    </h4>
  );
  if (connections.status === "error") {
    return (
      <section data-testid="connected" aria-labelledby={headingId} className="flex flex-col gap-1 border-t border-border pt-2">
        {heading}
        <p data-testid="connected-error" className="text-xs text-muted-foreground">
          This company&apos;s connections could not be read.
        </p>
      </section>
    );
  }
  const plugins = connections.plugins ?? [];
  const checks = summariseChecks(connections.checks ?? []);
  const checkCount = checks.ok + checks.warn + checks.fail + checks.skip;
  const tools = countRemoteTools(connections.tools ?? []);
  return (
    <section data-testid="connected" aria-labelledby={headingId} className="flex flex-col gap-1.5 border-t border-border pt-2">
      {heading}
      {plugins.length > 0 ? (
        <ul data-testid="connected-plugins" aria-label="Plugins" className="flex flex-wrap gap-1">
          {plugins.map((p) => (
            <li
              key={p.name}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[11px] text-foreground"
            >
              <span className="min-w-0 break-all">{p.name}</span>
              <span className="text-muted-foreground">{p.version}</span>
              {p.enabled === false && <span className="text-muted-foreground">(off)</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p data-testid="connected-plugins" className="text-xs text-muted-foreground">
          No plugins installed
        </p>
      )}
      <div data-testid="connected-checks" className="text-xs text-foreground">
        Checks:{" "}
        {checkCount === 0
          ? "none"
          : `${checks.ok} ok · ${checks.warn} warn · ${checks.fail} fail${checks.skip ? ` · ${checks.skip} skipped` : ""}`}
      </div>
      {checks.failing.length > 0 && (
        <div data-testid="connected-failing" className="flex flex-wrap gap-x-1.5 text-xs text-destructive">
          <span>Failing:</span>
          <span className="min-w-0 break-all font-mono">{checks.failing.join(", ")}</span>
        </div>
      )}
      {checks.warning.length > 0 && (
        <div data-testid="connected-warning" className="flex flex-wrap gap-x-1.5 text-xs text-muted-foreground">
          <span>Warning:</span>
          <span className="min-w-0 break-all font-mono">{checks.warning.join(", ")}</span>
        </div>
      )}
      <div data-testid="connected-tools" className="text-xs text-foreground">
        Remote tools:{" "}
        {tools.read + tools.write === 0 ? "none" : `${plural(tools.read, "read")} · ${plural(tools.write, "write")}`}
      </div>
    </section>
  );
}

function CompanyCard({
  entry,
  connections,
  switchable,
  pending,
  error,
  onOpen,
}: {
  entry: CompanyEntry;
  connections?: CompanyConnections;
  switchable: boolean;
  pending: boolean;
  error: string | null;
  onOpen: () => void;
}) {
  const status = companyStatus(entry);
  const needsLook = status === "attention" || status === "error";
  return (
    <article
      data-testid={`company-card-${entry.tenant_id}`}
      data-status={status}
      className={`relative flex min-w-0 flex-col gap-3 rounded-lg border p-3 transition-colors ${
        needsLook ? "border-warning/40 bg-warning/5" : "border-border bg-card"
      } ${switchable ? "hover:border-ring/40" : ""}`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h3 className="min-w-0 break-words text-sm font-semibold text-foreground">{entry.display_name}</h3>
        <StatusBadge status={BADGE_STATUS[status] ?? "idle"} label={statusLabel(status)} />
        {switchable && (
          // Stretched over the whole card, so the card is the link.
          <button
            type="button"
            onClick={onOpen}
            disabled={pending}
            aria-label={`Open ${entry.display_name}`}
            className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground after:absolute after:inset-0 after:rounded-lg after:content-[''] hover:text-foreground focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-ring disabled:opacity-50"
          >
            Open
            <ChevronRight aria-hidden className="size-3.5" />
          </button>
        )}
      </div>

      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}

      {entry.error ? (
        <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-xs text-destructive">
          <AlertTriangle aria-hidden className="mt-0.5 size-3.5 shrink-0" />
          <span>
            This company&apos;s figures could not be read: {entry.error}. No figures are shown for it
            rather than zeros; try again shortly.
          </span>
        </div>
      ) : (
        <CompanyFigures entry={entry} />
      )}

      {connections && <ConnectedBlock connections={connections} />}
    </article>
  );
}

export function CommandCentreView({
  visible = true,
  onSwitched = openHome,
}: {
  visible?: boolean;
  /** After a successful switch into a company. Defaults to that company's home screen. */
  onSwitched?: () => void;
}) {
  const client = useAppQueryClient();
  const overview = useQuery(
    {
      queryKey: OVERVIEW_KEY,
      queryFn: ({ signal }) => fetchOverview(signal),
      enabled: visible,
      staleTime: OVERVIEW_STALE_MS,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      refetchInterval: false,
    },
    client,
  );
  const connections = useQuery(
    {
      queryKey: CONNECTIONS_KEY,
      queryFn: ({ signal }) => fetchConnections(signal),
      enabled: visible,
      staleTime: OVERVIEW_STALE_MS,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      refetchInterval: false,
    },
    client,
  );
  const { pending, error: switchError, switchTo } = useTenantSwitch(onSwitched);
  const [opening, setOpening] = useState<string | null>(null);

  const data = overview.data;
  // The first load waits for the connections too, so the cards are drawn once.
  const loading = (overview.isFetching && !data) || (visible && connections.isPending);
  const reading = overview.isFetching || connections.isFetching;
  const connected = connectionsByTenant(connections.data);
  const error = overview.error instanceof OverviewRefused ? overview.error.message : null;
  const companies = data ? sortCompanies(data.tenants) : [];
  const home = data?.home_tenant_id;
  const attention = companies.filter((c) => {
    const s = companyStatus(c);
    return s === "attention" || s === "error";
  }).length;

  function open(tenantId: string) {
    setOpening(tenantId);
    void switchTo(tenantId);
  }

  return (
    <div
      data-testid="command-centre-view"
      className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}
    >
      <PageHeader
        title="Command centre"
        description={
          data && data.has_children
            ? `${companies.length} companies${attention ? ` · ${attention} need a look` : ""}`
            : undefined
        }
      >
        {data?.as_of && (
          <span className="text-xs text-muted-foreground">
            As of{" "}
            <time data-testid="org-as-of" dateTime={data.as_of}>
              {formatTime(data.as_of)}
            </time>
          </span>
        )}
        <Button
          variant="outline"
          size="sm"
          data-testid="org-refresh"
          disabled={reading}
          aria-busy={reading}
          onClick={() => {
            void overview.refetch({ cancelRefetch: false });
            void connections.refetch({ cancelRefetch: false });
          }}
        >
          <RefreshCw aria-hidden className={reading ? "animate-spin" : undefined} />
          Refresh
        </Button>
      </PageHeader>

      {error && (
        <p data-testid="org-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {loading && !error && (
        <div data-testid="org-loading" className="grid gap-3 lg:grid-cols-2" aria-hidden>
          {[0, 1, 2].map((i) => (
            <div key={i} className="rounded-lg border border-border bg-card p-3">
              <Skeleton className="h-4 w-1/3" />
              <Skeleton className="mt-3 h-10 w-full" />
              <Skeleton className="mt-2 h-3 w-2/3" />
            </div>
          ))}
        </div>
      )}

      {data && !loading && !error && !data.has_children && (
        <EmptyState
          testId="org-empty"
          icon={Building2}
          title="No companies under this tenant"
          description="The command centre lists a parent tenant's companies. This tenant has no child tenants."
        />
      )}

      {data && !loading && !error && data.has_children && (
        <div className="grid items-start gap-3 lg:grid-cols-2">
          {companies.map((entry) => (
            <CompanyCard
              key={entry.tenant_id}
              entry={entry}
              connections={connected.get(entry.tenant_id)}
              switchable={data.switching_enabled && entry.tenant_id !== home}
              pending={pending}
              error={opening === entry.tenant_id ? switchError : null}
              onOpen={() => open(entry.tenant_id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default CommandCentreView;
