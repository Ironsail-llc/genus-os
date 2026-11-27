/**
 * The command centre's reply (`GET /api/org/overview`) and the few rules the
 * page applies to it: which companies come first, and how a status reads.
 *
 * The figures are the bridge's — `robothor/tenancy/rollup.py`, the same rollup
 * the `org_overview` agent tool answers with — and are shown as given.
 */

export interface FailureCluster {
  agent_id: string;
  error_type?: string;
  count?: number;
  last_occurrence?: string | null;
}

export interface StuckAgent {
  agent_id: string;
  consecutive_errors: number;
  last_status?: string | null;
}

export interface OverdueSchedule {
  agent_id: string;
  next_run_at: string;
  overdue_minutes: number;
}

export interface OverdueTask {
  id: string | null;
  title: string | null;
  status?: string | null;
  assignedToAgent?: string | null;
  dueAt?: string | null;
  overdueDays: number | null;
  ageDays: number | null;
}

export interface CompanyEntry {
  tenant_id: string;
  display_name: string;
  parent_id: string | null;
  status: string;
  /** Set when the bridge could not read this company; no figures come with it. */
  error?: string;
  runs_24h?: number;
  failures_24h?: number;
  success_rate_24h?: number | null;
  cost_usd_24h?: number;
  runs_7d?: number;
  failures_7d?: number;
  success_rate_7d?: number | null;
  cost_usd_7d?: number;
  top_failures?: FailureCluster[];
  agents_with_consecutive_errors?: StuckAgent[];
  overdue_schedules?: OverdueSchedule[];
  tasks?: { open: number; overdue: number; oldest_overdue: OverdueTask[] };
  last_activity_at?: string | null;
}

export interface OrgOverview {
  as_of: string;
  home_tenant_id?: string;
  has_children: boolean;
  switching_enabled: boolean;
  tenants: CompanyEntry[];
}

const STATUS_LABELS: Record<string, string> = {
  ok: "OK",
  attention: "Needs attention",
  no_activity: "No activity",
  error: "Could not read",
};

/** A status in words: the card's colour is never the only signal. */
export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

/** An entry carrying `error` is unreadable, whatever else it says. */
export function companyStatus(entry: CompanyEntry): string {
  return entry.error ? "error" : entry.status;
}

const STATUS_ORDER: Record<string, number> = { attention: 0, error: 1, no_activity: 2, ok: 3 };

/**
 * Companies needing attention first, then the ones that could not be read,
 * then the quiet ones, then the healthy ones. A copy; stable within a status,
 * so the bridge's order (home first) holds among equals.
 */
export function sortCompanies(entries: readonly CompanyEntry[]): CompanyEntry[] {
  const rank = (e: CompanyEntry) => STATUS_ORDER[companyStatus(e)] ?? 1;
  return [...entries].sort((a, b) => rank(a) - rank(b));
}

export function formatRate(rate: number | null | undefined): string {
  return typeof rate === "number" ? `${Math.round(rate * 100)}%` : "—";
}

export function formatCost(usd: number | null | undefined): string {
  return `$${(usd ?? 0).toFixed(2)}`;
}

/** The analytics classifier's reason (`rate_limit`, …) in words. */
export function failureReasonLabel(errorType: string | null | undefined): string {
  return errorType ? errorType.replace(/_/g, " ") : "unknown";
}

/**
 * What each child company has connected (`GET /api/org/connections`): its
 * plugins, their connection checks, and the tools it lets a parent's agent use.
 * The bridge asks each company's engine; one that does not answer is an entry
 * with `status: "error"` and nothing else.
 */
export interface ConnectionPlugin {
  name: string;
  version: string;
  enabled: boolean;
}

export interface RemoteTool {
  name: string;
  plugin: string;
  access: "read" | "write" | string;
  attestations: string[];
  description: string;
}

export interface ConnectionCheck {
  name: string;
  status: "ok" | "warn" | "fail" | "skip" | string;
  detail: string;
}

export interface CompanyConnections {
  tenant_id: string;
  display_name: string;
  status: "ok" | "error" | string;
  error: string | null;
  plugins: ConnectionPlugin[];
  tools: RemoteTool[];
  checks: ConnectionCheck[];
}

export interface OrgConnections {
  as_of: string;
  home_tenant_id?: string;
  mode: string;
  tenants: CompanyConnections[];
}

const REMOTE_MODE_LABELS: Record<string, string> = {
  off: "off",
  read: "read only",
  dry_run: "dry run",
  full: "full",
};

/** The remote-tools mode in words; an unknown one as itself, never as `full`. */
export function remoteToolsModeLabel(mode: string): string {
  return REMOTE_MODE_LABELS[mode] ?? mode.replace(/_/g, " ");
}

export interface CheckSummary {
  ok: number;
  warn: number;
  fail: number;
  skip: number;
  /** Names of the failing checks, in the order given. */
  failing: string[];
  /** Names of the checks that warn (or report a status this page does not know). */
  warning: string[];
}

/** Checks counted by status. A status this page does not know counts as a warning. */
export function summariseChecks(checks: readonly ConnectionCheck[]): CheckSummary {
  const summary: CheckSummary = { ok: 0, warn: 0, fail: 0, skip: 0, failing: [], warning: [] };
  for (const check of checks) {
    if (check.status === "ok") summary.ok += 1;
    else if (check.status === "skip") summary.skip += 1;
    else if (check.status === "fail") {
      summary.fail += 1;
      summary.failing.push(check.name);
    } else {
      summary.warn += 1;
      summary.warning.push(check.name);
    }
  }
  return summary;
}

/** Remote tools split into reads and writes; anything not a read counts as a write. */
export function countRemoteTools(tools: readonly RemoteTool[]): { read: number; write: number } {
  const read = tools.filter((t) => t.access === "read").length;
  return { read, write: tools.length - read };
}

export function connectionsByTenant(reply: OrgConnections | null | undefined): Map<string, CompanyConnections> {
  return new Map((reply?.tenants ?? []).map((t) => [t.tenant_id, t]));
}
