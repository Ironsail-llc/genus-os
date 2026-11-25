"use client";

import { useEffect, useRef, useState } from "react";

import { useAccessibleTenants } from "@/hooks/use-accessible-tenants";

import { unavailableText } from "./telemetry-shared";
import {
  TELEMETRY_BRIDGE,
  num,
  readTelemetry,
  records,
  str,
  type TelemetryState,
} from "./telemetry-shared";

/**
 * The Organizations summary at the top of the Logs page, for the root tenant's
 * owner or admin AT HOME (`GET /api/telemetry/overview`): one row per
 * organization they may reach, with its errors in the last hour, its firing
 * monitors, or "Not connected" where it has no Datadog scope. One organization
 * failing is that row's state, never the summary's.
 *
 * Read once when it first shows, and again only on Refresh: switching tabs or
 * leaving the page keeps it, since every read is one audit row per
 * organization on the bridge.
 */

export interface OrgFigures {
  tenantId: string;
  connected: boolean;
  errors: number | null;
  firingMonitors: number | null;
  errorKind: string | null;
}

function parseOrg(row: Record<string, unknown>): OrgFigures {
  return {
    tenantId: str(row.tenant_id),
    connected: row.connected === true,
    errors: num(row.errors),
    firingMonitors: num(row.firing_monitors),
    errorKind: str(row.error_kind) || null,
  };
}

/** The summary's data: read when `enabled` first holds, then kept. */
export function useOrgOverview(enabled: boolean) {
  const [reloads, setReloads] = useState(0);
  const [answer, setAnswer] = useState<{ key: number; state: TelemetryState; orgs: OrgFigures[] } | null>(
    null,
  );
  const sentKey = useRef<number | null>(null);
  // Only the newest read may write. No cleanup that drops a reply: the page
  // going off screen mid-read must not leave the summary loading for good,
  // since coming back does not read again (the other tabs do the same).
  const generation = useRef(0);

  useEffect(() => {
    if (!enabled || sentKey.current === reloads) return;
    sentKey.current = reloads;
    const mine = ++generation.current;
    void readTelemetry(`${TELEMETRY_BRIDGE}/api/telemetry/overview?since=1h`).then((read) => {
      if (generation.current !== mine) return;
      setAnswer({ key: reloads, state: read.state, orgs: records(read.body.tenants).map(parseOrg) });
    });
  }, [enabled, reloads]);

  return {
    state: answer && answer.key === reloads ? answer.state : ({ kind: "loading" } as TelemetryState),
    orgs: answer?.orgs ?? [],
    reload: () => setReloads((n) => n + 1),
  };
}

export function OrganizationsSummary({
  state,
  orgs,
}: {
  state: TelemetryState;
  orgs: OrgFigures[];
}) {
  const tenants = useAccessibleTenants(true);
  const nameOf = (id: string) => tenants?.find((t) => t.id === id)?.display_name || id;

  return (
    <section
      data-testid="telemetry-orgs"
      aria-label="Organizations"
      className="min-w-0 rounded-lg border border-border bg-card p-3"
    >
      <div className="mb-2 flex items-baseline gap-2">
        <h3 className="text-xs font-medium text-foreground">Organizations</h3>
        <span className="text-[11px] text-muted-foreground">Errors and firing monitors, last hour</span>
      </div>
      {state.kind === "loading" ? (
        <p className="text-xs text-muted-foreground">Reading every organization&hellip;</p>
      ) : state.kind !== "ok" ? (
        <p className="text-xs text-muted-foreground" data-testid="telemetry-orgs-state">
          {state.kind === "not_configured"
            ? state.message
            : state.kind === "unavailable"
              ? unavailableText(state.errorKind)
              : "The organizations summary could not be read."}
        </p>
      ) : (
        <table className="w-full text-xs">
          <thead className="text-[10px] uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="py-1 text-left font-normal">Organization</th>
              <th className="w-24 py-1 text-right font-normal">Errors</th>
              <th className="w-32 py-1 text-right font-normal">Firing monitors</th>
            </tr>
          </thead>
          <tbody>
            {orgs.map((org) => (
              <tr key={org.tenantId} data-testid={`telemetry-org-${org.tenantId}`} className="border-t border-border/60">
                <td className="max-w-0 truncate py-1.5 text-foreground" title={org.tenantId}>
                  {nameOf(org.tenantId)}
                </td>
                {!org.connected ? (
                  <td colSpan={2} className="py-1.5 text-right text-muted-foreground">
                    Not connected
                  </td>
                ) : org.errorKind && org.errors === null && org.firingMonitors === null ? (
                  <td colSpan={2} className="py-1.5 text-right text-warning" title={unavailableText(org.errorKind)}>
                    Unavailable: {unavailableText(org.errorKind)}
                  </td>
                ) : (
                  // One of the two reads failed: that cell says so. The other
                  // read worked, so the failure is this cell's, not "not shared".
                  <>
                    <td
                      data-testid="telemetry-org-errors"
                      className={`py-1.5 text-right tabular-nums ${
                        org.errors === null && org.errorKind
                          ? "text-warning"
                          : org.errors
                            ? "text-destructive"
                            : "text-muted-foreground"
                      }`}
                      title={org.errors === null && org.errorKind ? unavailableText(org.errorKind) : undefined}
                    >
                      {org.errors === null && org.errorKind ? "Unavailable" : (org.errors ?? "—")}
                    </td>
                    <td
                      data-testid="telemetry-org-monitors"
                      className={`py-1.5 text-right tabular-nums ${
                        org.firingMonitors === null && org.errorKind
                          ? "text-warning"
                          : org.firingMonitors
                            ? "text-warning"
                            : "text-muted-foreground"
                      }`}
                      title={
                        org.firingMonitors !== null
                          ? undefined
                          : org.errorKind
                            ? unavailableText(org.errorKind)
                            : "Monitors are not shared with this organization."
                      }
                    >
                      {org.firingMonitors === null && org.errorKind ? "Unavailable" : (org.firingMonitors ?? "—")}
                    </td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
