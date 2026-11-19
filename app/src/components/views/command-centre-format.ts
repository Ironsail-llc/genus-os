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
