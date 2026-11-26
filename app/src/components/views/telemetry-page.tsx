"use client";

import { useState, type ReactNode } from "react";

import { TelemetryActivity } from "./telemetry-activity";
import { TelemetryLogs } from "./telemetry-logs";
import { TelemetryMonitors } from "./telemetry-monitors";
import { OrganizationsSummary, useOrgOverview } from "./telemetry-orgs";
import { TelemetryTraces, type TraceRequest } from "./telemetry-traces";

/**
 * Observe › Logs › Datadog — the organization's Datadog data in four tabs:
 * **Logs** (the search), **Traces** (error traces, and one trace as a tree),
 * **Monitors** (firing only by default) and **Activity** (what Genus recorded:
 * monitor alerts and watch findings, labelled recorded, not sent).
 *
 * Every tab stays mounted once opened and gates itself on `visible` after its
 * hooks, so a tab that goes off screen keeps its rows and sends nothing (no
 * Datadog query, no audit row) when it comes back. A tab never opened is never
 * read.
 *
 * The root tenant's owner or admin at home, with child organizations, also
 * gets the Organizations summary above the tabs. It is read here, once, so
 * switching tabs does not read it again.
 */

export type TelemetryTab = "logs" | "traces" | "monitors" | "activity";

const TABS: Array<[TelemetryTab, string]> = [
  ["logs", "Logs"],
  ["traces", "Traces"],
  ["monitors", "Monitors"],
  ["activity", "Activity"],
];

export interface TelemetryPageProps {
  visible: boolean;
  switcher?: ReactNode;
  /** Show the Organizations summary: the root's owner or admin, at home, with children. */
  showOrganizations?: boolean;
  /** A search handed in by a link (`?v=logs&q=`); a new one opens the Logs tab on it. */
  query?: string;
  /** The window the linked search opens on. */
  since?: string;
}

export function TelemetryPage({
  visible,
  switcher,
  showOrganizations = false,
  query,
  since,
}: TelemetryPageProps) {
  const [tab, setTab] = useState<TelemetryTab>("logs");
  const [opened, setOpened] = useState<ReadonlySet<TelemetryTab>>(() => new Set(["logs"]));
  const [traceRequest, setTraceRequest] = useState<TraceRequest | null>(null);

  // A new search from a link lands on the Logs tab. Adjusted during render,
  // like the shell's view route, so the first frame is already the Logs tab.
  const [seenQuery, setSeenQuery] = useState(query);
  if (query !== seenQuery) {
    setSeenQuery(query);
    if (query) setTab("logs");
  }

  const show = (next: TelemetryTab) => {
    setTab(next);
    if (!opened.has(next)) setOpened(new Set(opened).add(next));
  };

  const overview = useOrgOverview(visible && showOrganizations);

  const above = (
    <>
      {showOrganizations ? <OrganizationsSummary state={overview.state} orgs={overview.orgs} /> : null}
      <div
        role="tablist"
        aria-label="Datadog data"
        className="flex min-w-0 shrink-0 flex-wrap items-center gap-1 border-b border-border"
      >
        {TABS.map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            data-testid={`telemetry-tab-${id}`}
            onClick={() => show(id)}
            className={`-mb-px border-b-2 px-2.5 py-1.5 text-xs transition-colors ${
              tab === id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            {label}
          </button>
        ))}
      </div>
    </>
  );

  const isOpen = (id: TelemetryTab) => opened.has(id) || tab === id;

  return (
    <>
      <TelemetryLogs
        visible={visible && tab === "logs"}
        switcher={switcher}
        above={above}
        query={query}
        since={since}
        onViewTrace={(traceId) => {
          setTraceRequest((prev) => ({ traceId, n: (prev?.n ?? 0) + 1 }));
          show("traces");
        }}
        onRefresh={showOrganizations ? overview.reload : undefined}
      />
      {isOpen("traces") ? (
        <TelemetryTraces
          visible={visible && tab === "traces"}
          switcher={switcher}
          above={above}
          request={traceRequest}
        />
      ) : null}
      {isOpen("monitors") ? (
        <TelemetryMonitors visible={visible && tab === "monitors"} switcher={switcher} above={above} />
      ) : null}
      {isOpen("activity") ? (
        <TelemetryActivity visible={visible && tab === "activity"} switcher={switcher} above={above} />
      ) : null}
    </>
  );
}

export default TelemetryPage;
