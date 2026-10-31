"use client";

import { useEffect, useState } from "react";
import { KeyRound, Loader2, Lock, RefreshCw } from "lucide-react";

import { PageHeader } from "@/components/business/page-header";
import { Button } from "@/components/ui/button";
import { isOperatorRole } from "@/components/layout/nav-config";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { BRIDGE_UNREACHABLE } from "@/lib/bridge/use-bridge-poll";

/**
 * Settings › Secrets — which credentials this instance holds, which store each
 * sits in, when it last changed and which services read it.
 *
 * One route, `GET /api/secrets`, which answers with names, sources, timestamps
 * and readers and nothing value-shaped: no value, no fingerprint, no vault
 * key. The page has no way to show a credential because it is never sent one,
 * and it has no write either — a credential is set with `genus vault set`,
 * the one path that records the change.
 *
 * Not under `/api/vault/`: the BFF proxy refuses `/api/vault/get|list`
 * outright (`lib/bridge-proxy-policy.ts`), and this route is a status, not a
 * read of the store.
 */

const BRIDGE = "/api/bridge";

/**
 * A credential last changed longer ago than this is flagged for rotation.
 *
 * A named constant, stated on the page, so an operator reading "rotation
 * overdue" can see the rule behind it rather than guess at it.
 */
export const ROTATION_DAYS = 90;

const DAY_MS = 24 * 60 * 60 * 1000;

type SecretSource = "env" | "vault" | "both" | "none";

interface SecretRow {
  name: string;
  configured: boolean;
  source: SecretSource;
  updatedAt: string | null;
  readers: string[];
  shadowed: boolean;
  shadowedBy: string | null;
  envName: string;
  /** False when the vault could not be read: absent from it is then unknown, not "no". */
  vaultReadable: boolean;
}

const SOURCE_LABEL: Record<SecretSource, string> = {
  env: "environment",
  vault: "vault",
  both: "vault + environment",
  none: "nowhere",
};

function normalize(body: unknown): SecretRow[] {
  const list = (body as { secrets?: unknown } | null)?.secrets;
  if (!Array.isArray(list)) return [];
  const rows: SecretRow[] = [];
  for (const entry of list) {
    if (!entry || typeof entry !== "object") continue;
    const row = entry as Record<string, unknown>;
    if (typeof row.name !== "string" || !row.name) continue;
    const source = row.source;
    rows.push({
      name: row.name,
      configured: row.configured === true,
      source:
        source === "env" || source === "vault" || source === "both" ? source : "none",
      updatedAt: typeof row.updated_at === "string" ? row.updated_at : null,
      readers: Array.isArray(row.readers)
        ? row.readers.filter((r): r is string => typeof r === "string")
        : [],
      shadowed: row.shadowed === true,
      shadowedBy: typeof row.shadowed_by === "string" ? row.shadowed_by : null,
      envName: typeof row.env_name === "string" && row.env_name ? row.env_name : row.name,
      vaultReadable: row.vault_readable !== false,
    });
  }
  return rows;
}

/**
 * Whole days since `iso`, or null when there is no usable timestamp.
 *
 * `updated_at` is a Postgres `timestamptz` serialized with `isoformat()`, so it
 * always carries its offset and `Date` parses it as an instant, not as the
 * browser's local time.
 */
function ageInDays(iso: string | null, now: number): number | null {
  if (!iso) return null;
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return null;
  return Math.floor((now - at) / DAY_MS);
}

function formatWhen(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

type Load =
  | { state: "loading" }
  | { state: "forbidden" }
  | { state: "error"; message: string }
  | { state: "ready"; rows: SecretRow[]; readAt: number };

/** One read of `GET /api/secrets`, as the state the page should show. */
async function readSecrets(): Promise<Load> {
  try {
    const res = await fetch(`${BRIDGE}/api/secrets`);
    if (res.status === 403) return { state: "forbidden" };
    if (!res.ok) return { state: "error", message: await readBridgeReply(res) };
    // Each row's age is measured from when the list was read, not from each
    // render, so an open page changes only when it is refreshed.
    return { state: "ready", rows: normalize(await res.json()), readAt: Date.now() };
  } catch {
    return { state: "error", message: BRIDGE_UNREACHABLE };
  }
}

export interface SecretsPageProps {
  /** The Settings container unmounts an inactive page; this is the load gate. */
  visible?: boolean;
  /** Session role. UX only — the bridge authorizes `GET /api/secrets` itself. */
  role?: string | null;
}

export function SecretsPage({ visible = true, role }: SecretsPageProps) {
  const operator = isOperatorRole(role);
  const [load, setLoad] = useState<Load>({ state: "loading" });

  // Refresh is a counter the effect depends on, so every read -- the first one
  // and each refresh -- goes through the one effect, and a reply that lands
  // after the page went away is dropped rather than set on an unmounted page.
  const [refreshes, setRefreshes] = useState(0);

  useEffect(() => {
    if (!visible || !operator) return;
    let cancelled = false;
    void readSecrets().then((next) => {
      if (!cancelled) setLoad(next);
    });
    return () => {
      cancelled = true;
    };
  }, [visible, operator, refreshes]);

  const forbidden = !operator || load.state === "forbidden";

  return (
    <div className="flex min-w-0 flex-col gap-3 p-4" data-testid="settings-page-secrets">
      <PageHeader title="Secrets">
        {!forbidden && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setLoad({ state: "loading" });
              setRefreshes((n) => n + 1);
            }}
            disabled={load.state === "loading"}
            data-testid="secrets-refresh"
          >
            <RefreshCw aria-hidden className="size-3.5" />
            Refresh
          </Button>
        )}
      </PageHeader>
      <p className="max-w-2xl text-xs text-muted-foreground">
        Every credential this instance declares or holds: which store it sits in, when it last
        changed and which services read it. Values are never sent to this page. A credential last
        changed more than {ROTATION_DAYS} days ago is flagged for rotation. To set or rotate one,
        run <code className="rounded bg-muted px-1 font-mono">genus vault set &lt;key&gt;</code> on
        the instance, then restart the services listed as its readers.
      </p>

      {forbidden ? (
        <div
          className="flex max-w-2xl items-start gap-2 rounded-lg border border-border bg-card p-3"
          data-testid="secrets-forbidden"
        >
          <Lock aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          <div>
            <p className="text-sm font-medium text-foreground">Operator access required</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Only an owner or admin of this instance can see which secrets it holds.
            </p>
          </div>
        </div>
      ) : load.state === "loading" ? (
        <div
          className="flex items-center gap-2 text-xs text-muted-foreground"
          data-testid="secrets-loading"
        >
          <Loader2 aria-hidden className="size-4 animate-spin" />
          Reading the secret store…
        </div>
      ) : load.state === "error" ? (
        <div
          role="alert"
          className="max-w-2xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
          data-testid="secrets-error"
        >
          {load.message}
        </div>
      ) : load.rows.length === 0 ? (
        <div
          className="max-w-2xl rounded-lg border border-dashed border-border bg-card/40 p-4"
          data-testid="secrets-empty"
        >
          <p className="text-sm font-medium text-foreground">No secrets yet</p>
          <p className="mt-1 text-xs text-muted-foreground">
            This instance neither declares nor holds a credential. Add one with{" "}
            <code className="font-mono">genus vault set &lt;key&gt;</code>.
          </p>
        </div>
      ) : (
        <>
          {load.rows.some((row) => !row.vaultReadable) && (
            <div
              role="alert"
              className="max-w-2xl rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning"
              data-testid="secrets-vault-unreadable"
            >
              The vault could not be read, so vault-held credentials that have no environment
              value show as unknown. Check the
              vault with <code className="font-mono">genus doctor</code>, then refresh.
            </div>
          )}
          <SecretsTable rows={load.rows} now={load.readAt} />
        </>
      )}
    </div>
  );
}

function SecretsTable({ rows, now }: { rows: SecretRow[]; now: number }) {
  return (
    <table
      className="w-full min-w-0 table-fixed border-collapse text-left text-xs"
      data-testid="secrets-table"
    >
      <thead className="hidden text-[11px] uppercase tracking-wide text-muted-foreground md:table-header-group">
        <tr className="border-b border-border">
          <th className="w-[30%] py-2 pr-3 font-medium">Name</th>
          <th className="py-2 pr-3 font-medium">Configured</th>
          <th className="py-2 pr-3 font-medium">Source</th>
          <th className="py-2 pr-3 font-medium">Last change</th>
          <th className="py-2 pr-3 font-medium">Readers</th>
          <th className="py-2 font-medium">Rotation</th>
        </tr>
      </thead>
      <tbody className="block md:table-row-group">
        {rows.map((row) => (
          <SecretRowView key={row.name} row={row} now={now} />
        ))}
      </tbody>
    </table>
  );
}

/** A label shown only where the header row is hidden. */
function CellLabel({ children }: { children: string }) {
  return <span className="mr-1.5 text-muted-foreground md:hidden">{children}:</span>;
}

function SecretRowView({ row, now }: { row: SecretRow; now: number }) {
  const age = ageInDays(row.updatedAt, now);
  const overdue = age !== null && age > ROTATION_DAYS;
  const cell = "block py-1 md:table-cell md:py-2 md:pr-3 md:align-top";

  return (
    <tr
      className="mb-2 block rounded-lg border border-border p-3 md:mb-0 md:table-row md:rounded-none md:border-0 md:border-b md:p-0"
      data-testid={`secrets-row-${row.name}`}
    >
      <td className={`${cell} font-mono text-[12px] text-foreground`}>
        <span className="flex items-start gap-1.5 break-all">
          <KeyRound aria-hidden className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
          {row.name}
        </span>
        {row.envName !== row.name && (
          <span className="mt-0.5 block break-all text-[11px] text-muted-foreground">
            declared as {row.envName}
          </span>
        )}
      </td>
      <td className={cell} data-testid={`secrets-configured-${row.name}`}>
        <CellLabel>Configured</CellLabel>
        {row.configured ? (
          <span className="text-success">configured</span>
        ) : !row.vaultReadable ? (
          <span className="text-muted-foreground">unknown</span>
        ) : (
          <span className="text-warning">not configured</span>
        )}
      </td>
      <td className={cell} data-testid={`secrets-source-${row.name}`}>
        <CellLabel>Source</CellLabel>
        {/* Unknown only when nothing was found: an environment value is still
            known while the vault is down; only the vault's half is not. */}
        {row.vaultReadable
          ? SOURCE_LABEL[row.source]
          : row.source === "none"
            ? "unknown"
            : `${SOURCE_LABEL[row.source]} (vault unknown)`}
        {row.shadowed && (
          <span className="mt-0.5 block text-[11px] text-warning">
            The two copies differ; {row.shadowedBy ?? "one store"} is not being served.
          </span>
        )}
      </td>
      <td className={cell}>
        <CellLabel>Last change</CellLabel>
        {row.updatedAt ? formatWhen(row.updatedAt) : "never recorded"}
      </td>
      <td className={cell}>
        <CellLabel>Readers</CellLabel>
        <span className="inline-flex flex-wrap gap-1">
          {row.readers.map((unit) => (
            <span key={unit} className="rounded border border-border px-1.5 font-mono text-[11px]">
              {unit}
            </span>
          ))}
        </span>
      </td>
      <td className={cell}>
        <CellLabel>Rotation</CellLabel>
        {overdue ? (
          <span
            className="rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-[11px] text-warning"
            data-testid={`secrets-overdue-${row.name}`}
          >
            rotation overdue
          </span>
        ) : age === null ? (
          <span className="text-muted-foreground">unknown</span>
        ) : (
          <span className="text-muted-foreground">{age} days old</span>
        )}
      </td>
    </tr>
  );
}
