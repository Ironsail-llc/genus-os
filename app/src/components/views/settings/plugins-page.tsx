"use client";

import { useCallback, useMemo, useState } from "react";
import { Loader2, Puzzle, RefreshCw } from "lucide-react";

import { PageHeader } from "@/components/business/page-header";
import { EmptyState } from "@/components/business/empty-state";
import { Button } from "@/components/ui/button";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { useRowActions } from "@/lib/bridge/row-actions";
import { BRIDGE_UNREACHABLE, useBridgePoll } from "@/lib/bridge/use-bridge-poll";

/**
 * Settings › Plugins — the third-party code this instance runs, and the three
 * acts an operator has over it: record, toggle, reload.
 *
 * `pip install` used to be the whole of plugin governance: a distribution that
 * published a `genus.*` entry point became part of the engine, and the only way
 * to stop it was to uninstall the package. `plugins.lock` is the record that
 * was missing, and this page is the only place an operator can write it without
 * a shell on the box.
 *
 * Four rules, each of which the bridge's shape makes it easy to get wrong:
 *
 * **The verdict is not rendered.** Every recorded row carries
 * `verdict: "unscanned"` and nothing scans a plugin yet (that is C6). A pill
 * reading "unscanned" on the screen where third-party code is turned on invites
 * the reading "scanned, and fine" the moment a scanner does exist and the word
 * changes; a field that has never carried a judgement is not a judgement.
 *
 * **Recording is the fresh install's primary act.** Until `genus plugin sync`
 * has run, `lockfile.present` is `false`, every `recorded` is `false`, and
 * enable/disable both answer 404. `POST /api/plugins/sync` is the only thing on
 * this page that works on a box nobody has ever shelled into.
 *
 * **409 and 503 are not the same refusal.** 409 is the file's CONTENTS —
 * `genus plugin sync --force` rebuilds it, at the cost of every recorded
 * disable, and that escape is deliberately CLI-only so the operator reads what
 * it costs first. 503 is the PATH, and forcing cannot fix a filesystem, so this
 * page never mentions `--force` for one.
 *
 * **A toggle is not an apply.** enable/disable write the lock row and answer
 * `reloaded: false`; the running engine keeps serving the set it discovered at
 * boot. So the switch moves and the STATE pill does not, and a bar comes up
 * saying the change is recorded but not live. One reload after N toggles.
 */

const BRIDGE = "/api/bridge";
const LISTING_URL = `${BRIDGE}/api/plugins`;

/** The engine answered nothing at all. A bridge that cannot reach it says 502. */
const ENGINE_UNREACHABLE =
  "The engine is unreachable from the bridge, so the plugin set could not be read. Nothing has been changed.";

/** The reason string the engine writes for a plugin its operator turned off. */
const BY_OPERATOR = "disabled by operator";

export interface Lockfile {
  pathConfigured: boolean;
  present: boolean;
  malformed: boolean;
  rows: number;
}

export interface PluginManifest {
  contractVersion: number | null;
  declared: Array<{ kind: string; names: string[] }>;
}

export interface Plugin {
  name: string;
  version: string;
  enabled: boolean;
  recorded: boolean;
  state: string;
  drifted: boolean;
  groups: string[];
  contributions: Array<{ kind: string; count: number }>;
  failureReason: string | null;
  manifest: PluginManifest | null;
}

export interface Listing {
  generation: number | null;
  lockfile: Lockfile;
  plugins: Plugin[];
}

interface SyncReport {
  recorded: string[];
  added: string[];
  updated: string[];
  removed: string[];
}

interface ReloadFailure {
  name: string;
  group: string;
  reason: string;
}

interface ReloadReport {
  generation: number | null;
  loaded: number;
  failures: ReloadFailure[];
}

const EMPTY_LISTING: Listing = {
  generation: null,
  lockfile: { pathConfigured: true, present: false, malformed: false, rows: 0 },
  plugins: [],
};

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function normalizeManifest(value: unknown): PluginManifest | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  const declared = row.declared;
  return {
    contractVersion: typeof row.contract_version === "number" ? row.contract_version : null,
    declared:
      declared && typeof declared === "object"
        ? Object.entries(declared as Record<string, unknown>)
            .map(([kind, names]) => ({ kind, names: strings(names) }))
            .filter((entry) => entry.names.length > 0)
        : [],
  };
}

export function normalizePlugin(value: unknown): Plugin | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  if (typeof row.name !== "string" || !row.name) return null;
  const contributions = row.contributions;
  return {
    name: row.name,
    version: typeof row.version === "string" ? row.version : "",
    // `enabled` defaults TRUE, matching the engine: a distribution with no lock
    // row is not off, it is ungoverned. Defaulting to false here would draw
    // every plugin on a fresh install as switched off while all of them run.
    enabled: row.enabled !== false,
    recorded: row.recorded === true,
    state: typeof row.state === "string" ? row.state : "",
    drifted: row.drifted === true,
    groups: strings(row.groups),
    contributions:
      contributions && typeof contributions === "object"
        ? Object.entries(contributions as Record<string, unknown>)
            .map(([kind, count]) => ({ kind, count: typeof count === "number" ? count : 0 }))
            .filter((entry) => entry.count > 0)
        : [],
    failureReason: typeof row.failure_reason === "string" ? row.failure_reason : null,
    manifest: normalizeManifest(row.manifest),
  };
}

export function normalizeListing(body: unknown): Listing {
  const root = (body ?? {}) as Record<string, unknown>;
  const lock = (root.lockfile ?? {}) as Record<string, unknown>;
  return {
    generation: typeof root.generation === "number" ? root.generation : null,
    lockfile: {
      // Absent means "the bridge did not say"; only an explicit `false` is a
      // claim that no path resolves, and only that may raise the alarm below.
      pathConfigured: lock.path_configured !== false,
      present: lock.present === true,
      malformed: lock.malformed === true,
      rows: typeof lock.rows === "number" ? lock.rows : 0,
    },
    plugins: Array.isArray(root.plugins)
      ? root.plugins.map(normalizePlugin).filter((p): p is Plugin => p !== null)
      : [],
  };
}

function normalizeSync(body: unknown): SyncReport {
  const root = (body ?? {}) as Record<string, unknown>;
  return {
    recorded: strings(root.recorded),
    added: strings(root.added),
    updated: strings(root.updated),
    removed: strings(root.removed),
  };
}

function normalizeReload(body: unknown): ReloadReport {
  const root = (body ?? {}) as Record<string, unknown>;
  return {
    generation: typeof root.generation === "number" ? root.generation : null,
    loaded: typeof root.loaded === "number" ? root.loaded : 0,
    failures: Array.isArray(root.failures)
      ? root.failures
          .map((entry) => {
            if (!entry || typeof entry !== "object") return null;
            const row = entry as Record<string, unknown>;
            return {
              name: typeof row.name === "string" ? row.name : "",
              group: typeof row.group === "string" ? row.group : "",
              reason: typeof row.reason === "string" ? row.reason : "",
            };
          })
          .filter((f): f is ReloadFailure => f !== null)
      : [],
  };
}

/** `-` and `_` are the same character in a distribution name; case is not either. */
function squash(name: string): string {
  return name.toLowerCase().replace(/[-_.]/g, "");
}

/**
 * Which distribution a reload failure belongs to.
 *
 * `failures[].name` is the ENTRY-POINT name, not the distribution's — one
 * distribution can appear in this list several times, once per group it
 * publishes into, and `genus-hostinfo` shows up as `hostinfo`. String equality
 * would silently drop most of them, so the listing is the index: narrow to the
 * distributions that publish into the failing group, and take the answer only
 * when it is unambiguous. Anything else is rendered as its own unattributed
 * line rather than filed under a guess — this page's whole job is saying what
 * is actually true about code the operator did not write.
 */
export function attributeFailure(failure: ReloadFailure, plugins: Plugin[]): string | null {
  const candidates = plugins.filter((p) => p.groups.includes(failure.group));
  if (candidates.length === 1) return candidates[0].name;
  const wanted = squash(failure.name);
  const byName = candidates.filter(
    (p) => squash(p.name) === wanted || squash(p.name).endsWith(wanted)
  );
  if (byName.length === 1) return byName[0].name;
  const byDeclared = candidates.filter((p) =>
    (p.manifest?.declared ?? []).some((entry) => entry.names.some((n) => squash(n) === wanted))
  );
  return byDeclared.length === 1 ? byDeclared[0].name : null;
}

function statePill(state: string): { label: string; className: string } {
  if (state === "loaded") {
    return { label: "loaded", className: "border-success/30 bg-success/10 text-success" };
  }
  if (state === "failed") {
    // A fault: something the operator did not ask for and has to fix.
    return {
      label: "failed",
      className: "border-destructive/30 bg-destructive/10 text-destructive",
    };
  }
  if (state === "disabled") {
    // A decision, not a fault. Neutral, never red — the operator made it.
    return { label: "disabled", className: "border-border bg-muted text-muted-foreground" };
  }
  return { label: state || "unknown", className: "border-border bg-muted text-muted-foreground" };
}

export interface PluginsPageProps {
  /** The Settings container renders one page at a time; this is the load gate. */
  visible?: boolean;
}

export function PluginsPage({ visible = true }: PluginsPageProps) {
  const [listing, setListing] = useState<Listing | null>(null);

  const [syncing, setSyncing] = useState(false);
  const [syncReport, setSyncReport] = useState<SyncReport | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  /** Only a 409 may offer the CLI escape; a 503 is a filesystem, not a file. */
  const [syncForcible, setSyncForcible] = useState(false);

  const [reloading, setReloading] = useState(false);
  const [reloadReport, setReloadReport] = useState<ReloadReport | null>(null);
  const [reloadError, setReloadError] = useState<string | null>(null);
  /** A lock row was written and the engine has not been asked to act on it yet. */
  const [pending, setPending] = useState(false);

  const { busyRow, rowErrors, rowNotes, act, setRowNote } = useRowActions();

  /*
    The notes a toggle leaves behind all say the same thing — "recorded, and
    the engine has not been told yet" — so the reload that tells it is what
    retires them. Leaving them up would put "reload to apply it" under a row
    the operator has just reloaded, which is the page contradicting the report
    directly above it. The row ERRORS are not touched: a refusal is a fact
    about a write that did not happen, and a reload does not answer it.
  */
  const clearNotes = useCallback(() => {
    for (const name of Object.keys(rowNotes)) setRowNote(name, null);
  }, [rowNotes, setRowNote]);

  const onData = useCallback((body: unknown) => {
    setListing(normalizeListing(body));
  }, []);

  /*
    Nothing here is polled.

    Every fact on this page changes only when somebody acts — an install on the
    box, or one of the three buttons below — and a plugin listing walks the
    entry points of every installed distribution on the engine's side. A beat
    would put that work on the box once a minute so that the screen could show
    the same answer again.
  */
  const poll = useBridgePoll({ visible, url: LISTING_URL, onData, paused: true });

  const data = listing ?? EMPTY_LISTING;
  const engineDown = poll.status === 502;
  const listError = poll.forbidden ? null : engineDown ? ENGINE_UNREACHABLE : poll.error;

  const record = useCallback(async () => {
    setSyncing(true);
    setSyncError(null);
    setSyncForcible(false);
    setSyncReport(null);
    try {
      const res = await fetch(`${BRIDGE}/api/plugins/sync`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) {
        setSyncError(res.status === 502 ? ENGINE_UNREACHABLE : await readBridgeReply(res));
        setSyncForcible(res.status === 409);
        return;
      }
      setSyncReport(normalizeSync(await res.json()));
      // Every row's `recorded` just changed, and so did the lockfile header.
      poll.reload();
    } catch {
      setSyncError(BRIDGE_UNREACHABLE);
    } finally {
      setSyncing(false);
    }
  }, [poll]);

  const reload = useCallback(async () => {
    setReloading(true);
    setReloadError(null);
    setReloadReport(null);
    try {
      const res = await fetch(`${BRIDGE}/api/plugins/reload`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) {
        setReloadError(res.status === 502 ? ENGINE_UNREACHABLE : await readBridgeReply(res));
        return;
      }
      setReloadReport(normalizeReload(await res.json()));
      // The bar comes down whatever the report says: the engine has been asked,
      // and asking it a second time would not change the answer. A reload that
      // failed reports itself below, in its own words.
      setPending(false);
      clearNotes();
      poll.reload();
    } catch {
      setReloadError(BRIDGE_UNREACHABLE);
    } finally {
      setReloading(false);
    }
  }, [poll, clearNotes]);

  const toggle = useCallback(
    (plugin: Plugin) => {
      const verb = plugin.enabled ? "disable" : "enable";
      void act(
        plugin.name,
        `${BRIDGE}/api/plugins/${encodeURIComponent(plugin.name)}/${verb}`,
        { method: "POST" },
        (body) => {
          const row = (body ?? {}) as Record<string, unknown>;
          const enabled = row.enabled === true;
          setListing((current) =>
            current === null
              ? current
              : {
                  ...current,
                  plugins: current.plugins.map((p) =>
                    p.name === plugin.name
                      ? {
                          ...p,
                          // ONLY what the lock row says. `state`, `contributions`
                          // and `failure_reason` describe the RUNNING engine,
                          // which this write did not touch (`reloaded: false`) —
                          // moving them here would draw a plugin as unloaded
                          // while it is still serving tool calls.
                          enabled,
                          recorded: true,
                          version: typeof row.version === "string" ? row.version : p.version,
                        }
                      : p
                  ),
                }
          );
          setRowNote(
            plugin.name,
            enabled
              ? "Recorded as enabled. The engine is still running the set it discovered — reload to apply it."
              : "Recorded as disabled. The engine is still running it — reload to stop it being imported."
          );
          setPending(true);
        }
      );
    },
    [act, setRowNote]
  );

  /** The reload report's failures, filed under the distribution each belongs to. */
  const attributed = useMemo(() => {
    if (!reloadReport) return { byPlugin: [], unmatched: [] as ReloadFailure[] };
    const byPlugin = new Map<string, ReloadFailure[]>();
    const unmatched: ReloadFailure[] = [];
    for (const failure of reloadReport.failures) {
      const name = attributeFailure(failure, data.plugins);
      if (name === null) unmatched.push(failure);
      else byPlugin.set(name, [...(byPlugin.get(name) ?? []), failure]);
    }
    return {
      byPlugin: [...byPlugin.entries()].map(([name, failures]) => ({ name, failures })),
      unmatched,
    };
  }, [reloadReport, data.plugins]);

  if (!visible) return null;

  const reloadButton = (
    <Button size="sm" data-testid="plugins-reload" disabled={reloading} onClick={() => void reload()}>
      {reloading ? "Reloading…" : "Reload engine plugins"}
    </Button>
  );

  return (
    <div
      className="flex min-w-0 flex-col gap-4 p-4 pb-20"
      data-testid="settings-page-plugins"
    >
      <PageHeader
        title="Plugins"
        description="What this instance loads, and what it refuses."
        className="flex-wrap"
      >
        <Button
          variant="outline"
          size="sm"
          data-testid="plugins-refresh"
          onClick={() => poll.reload()}
        >
          <RefreshCw aria-hidden className={poll.loading ? "animate-spin" : undefined} />
          Refresh
        </Button>
      </PageHeader>

      <p className="max-w-3xl text-xs text-muted-foreground">
        A plugin is a Python distribution that publishes a <code>genus.*</code> entry point, and
        installing one is what puts it here. The lockfile records what was accepted and what is
        turned off; turning one off writes that record, and the engine keeps running the set it
        discovered until it is reloaded.
      </p>

      {poll.forbidden ? (
        <p className="text-xs text-muted-foreground" data-testid="plugins-forbidden">
          Plugins are operator-only on this appliance, so there is nothing to show here. A role that
          looks like an operator in this browser can still be refused by the bridge — it also
          requires the platform tenant and a human session. Ask an owner or admin on this instance.
        </p>
      ) : null}

      {listError ? (
        <p
          data-testid="plugins-error"
          className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
        >
          {listError}
        </p>
      ) : null}

      {!poll.forbidden && poll.loading && listing === null && !listError ? (
        <div
          className="flex items-center gap-2 p-6 text-xs text-muted-foreground"
          data-testid="plugins-loading"
        >
          <Loader2 aria-hidden className="size-4 animate-spin" />
          Reading what this instance has installed…
        </div>
      ) : null}

      {listing !== null && !poll.forbidden ? (
        <>
          <div className="flex max-w-3xl flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
            <span
              data-testid="plugins-generation"
              className="rounded-full border border-border bg-muted px-2 py-0.5"
            >
              discovery generation {data.generation ?? "unknown"}
            </span>
            <span
              data-testid="plugins-lockfile"
              className="rounded-full border border-border bg-muted px-2 py-0.5"
            >
              {data.lockfile.present
                ? `lockfile: ${data.lockfile.rows} recorded ${
                    data.lockfile.rows === 1 ? "row" : "rows"
                  }`
                : "lockfile: not written yet"}
            </span>
          </div>

          {/*
            The acts, on their own wrapping row rather than in the header: a
            phone cannot take three buttons on one baseline, and the header's
            action slot is a single non-wrapping line by design.

            Each of them appears in exactly ONE place at a time. Record lives
            here once the lockfile exists and in the empty-state card before
            that; Reload lives here until a row has been written and in the
            sticky bar after, because that is the moment it stops being an
            errand and becomes the thing the operator came to do.
          */}
          <div className="flex max-w-3xl flex-wrap items-center gap-2">
            {data.lockfile.present ? (
              <Button
                variant="outline"
                size="sm"
                data-testid="plugins-record"
                disabled={syncing}
                onClick={() => void record()}
              >
                {syncing ? "Recording…" : "Record installed plugins"}
              </Button>
            ) : null}
            {!pending ? reloadButton : null}
          </div>

          {!data.lockfile.pathConfigured ? (
            <p
              data-testid="plugins-lockfile-unconfigured"
              className="max-w-3xl rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning"
            >
              No workspace resolves on this deployment, so there is nowhere to keep the lockfile.
              Nothing can be recorded or turned off until that is fixed on the box; every installed
              plugin loads, which is how the platform behaved before the lockfile existed.
            </p>
          ) : null}

          {data.lockfile.malformed ? (
            <p
              data-testid="plugins-lockfile-malformed"
              className="max-w-3xl rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning"
            >
              Some rows in the lockfile could not be read. The rows that do parse still govern —
              they are not discarded, because that would put every disabled plugin straight back
              into service — but recording will refuse until the file is rebuilt with{" "}
              <code className="font-mono">genus plugin sync --force</code> on the box, which keeps
              the old bytes and reports what it could not read.
            </p>
          ) : null}

          {!data.lockfile.present ? (
            <div
              data-testid="plugins-record-empty"
              className="flex max-w-3xl flex-col gap-2 rounded-lg border border-primary/25 bg-primary/5 p-3"
            >
              <p className="text-sm font-medium text-foreground">
                Nothing is recorded on this instance yet
              </p>
              <p className="text-xs text-muted-foreground">
                The lockfile is opt-in: with no file, every installed plugin loads exactly as it
                always has and none of them can be turned off. Recording writes one row per
                installed distribution — it changes nothing about what is running, and it is what
                makes the switches below work.
              </p>
              <div>
                <Button
                  size="sm"
                  data-testid="plugins-record"
                  disabled={syncing}
                  onClick={() => void record()}
                >
                  {syncing ? "Recording…" : "Record installed plugins"}
                </Button>
              </div>
            </div>
          ) : null}

          {syncError ? (
            <p
              data-testid="plugins-record-error"
              className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
            >
              {syncError}
            </p>
          ) : null}

          {syncForcible ? (
            <p
              data-testid="plugins-record-force"
              className="max-w-3xl text-[11px] text-muted-foreground"
            >
              Rebuilding the file is a shell command, not a button:{" "}
              <code className="font-mono">genus plugin sync --force</code>. It is deliberately not
              offered over HTTP — it discards every disable it cannot read, and the CLI is where
              that cost is printed before it happens. The old bytes are kept as{" "}
              <code className="font-mono">plugins.lock.rejected</code>.
            </p>
          ) : null}

          {syncReport ? (
            <p
              data-testid="plugins-record-result"
              className="max-w-3xl rounded-lg border border-border bg-card p-3 text-xs text-muted-foreground"
            >
              Recorded {syncReport.recorded.length}{" "}
              {syncReport.recorded.length === 1 ? "distribution" : "distributions"}
              {syncReport.added.length ? ` · added ${syncReport.added.join(", ")}` : ""}
              {syncReport.updated.length ? ` · updated ${syncReport.updated.join(", ")}` : ""}
              {syncReport.removed.length
                ? ` · dropped rows for ${syncReport.removed.join(", ")}, which are no longer installed`
                : ""}
              . Recording is not applying — nothing was loaded or unloaded by this.
            </p>
          ) : null}

          {data.plugins.length === 0 && !listError ? (
            <EmptyState
              testId="plugins-empty"
              icon={Puzzle}
              title="No plugins are installed"
              description="Nothing on this box publishes a genus.* entry point. Install one with pip and it appears here — there is no registry to enrol in and no package format to learn."
            />
          ) : null}

          {data.plugins.map((plugin) => {
            const pill = statePill(plugin.state);
            const busy = busyRow === plugin.name;
            return (
              <div
                key={plugin.name}
                data-testid={`plugin-${plugin.name}`}
                className="flex min-w-0 max-w-3xl flex-col gap-2 rounded-lg border border-border bg-card p-3"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="min-w-0 break-all text-sm font-medium text-foreground">
                    {plugin.name}
                  </span>
                  <span className="shrink-0 rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                    {plugin.version || "version unknown"}
                  </span>
                  <span
                    data-testid={`plugin-state-${plugin.name}`}
                    data-state={plugin.state}
                    className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium ${pill.className}`}
                  >
                    {pill.label}
                  </span>
                  {plugin.drifted ? (
                    <span
                      data-testid={`plugin-drifted-${plugin.name}`}
                      className="shrink-0 rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-[10px] font-medium text-warning"
                    >
                      drifted
                    </span>
                  ) : null}
                </div>

                <div
                  data-testid={`plugin-groups-${plugin.name}`}
                  className="flex min-w-0 flex-wrap items-center gap-1"
                >
                  {plugin.groups.length === 0 ? (
                    <span className="text-[11px] text-muted-foreground">
                      No entry-point group — nothing to load.
                    </span>
                  ) : (
                    plugin.groups.map((group) => (
                      <span
                        key={group}
                        className="rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
                      >
                        {group}
                      </span>
                    ))
                  )}
                </div>

                <p
                  data-testid={`plugin-contributions-${plugin.name}`}
                  className="text-[11px] text-muted-foreground"
                >
                  {plugin.contributions.length === 0
                    ? "Contributing nothing to the running engine right now."
                    : plugin.contributions
                        .map((entry) => `${entry.kind} × ${entry.count}`)
                        .join(" · ")}
                </p>

                {plugin.failureReason ? (
                  <p
                    data-testid={`plugin-failure-${plugin.name}`}
                    className={`text-[11px] ${
                      plugin.failureReason === BY_OPERATOR
                        ? "text-muted-foreground"
                        : "text-destructive"
                    }`}
                  >
                    {plugin.failureReason}
                  </p>
                ) : null}

                <details
                  data-testid={`plugin-manifest-${plugin.name}`}
                  className="text-[11px] text-muted-foreground"
                >
                  <summary className="cursor-pointer select-none">Manifest</summary>
                  {plugin.manifest === null ? (
                    <p className="pt-1">
                      This distribution ships no <code className="font-mono">genus-plugin.yaml</code>
                      , so it declares nothing and nothing about it can drift.
                    </p>
                  ) : (
                    <div className="flex flex-col gap-0.5 pt-1">
                      <p>
                        Contract version{" "}
                        <span className="font-mono">
                          {plugin.manifest.contractVersion ?? "not declared"}
                        </span>
                      </p>
                      {plugin.manifest.declared.length === 0 ? (
                        <p>The manifest declares no names.</p>
                      ) : (
                        plugin.manifest.declared.map((entry) => (
                          <p key={entry.kind}>
                            <span className="font-mono">{entry.kind}</span>:{" "}
                            <span className="font-mono">{entry.names.join(", ")}</span>
                          </p>
                        ))
                      )}
                    </div>
                  )}
                </details>

                <div className="flex min-w-0 flex-wrap items-center gap-2 pt-0.5">
                  <button
                    type="button"
                    role="switch"
                    aria-checked={plugin.enabled}
                    aria-label={`${plugin.name} enabled`}
                    data-testid={`plugin-switch-${plugin.name}`}
                    disabled={!plugin.recorded || busy}
                    onClick={() => toggle(plugin)}
                    className={`inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                      plugin.enabled ? "border-primary/40 bg-primary/70" : "border-border bg-muted"
                    }`}
                  >
                    <span
                      aria-hidden
                      className={`size-3.5 rounded-full bg-background transition-transform ${
                        plugin.enabled ? "translate-x-[18px]" : "translate-x-[2px]"
                      }`}
                    />
                  </button>
                  <span className="text-[11px] text-muted-foreground">
                    {plugin.enabled ? "Enabled" : "Disabled"}
                    {busy ? " · saving…" : ""}
                  </span>
                  {!plugin.recorded ? (
                    <span
                      data-testid={`plugin-hint-${plugin.name}`}
                      className="text-[11px] text-muted-foreground"
                    >
                      Record this instance&apos;s plugins first — there is no row to turn off yet,
                      and the engine answers a toggle on one with a 404.
                    </span>
                  ) : null}
                </div>

                {rowNotes[plugin.name] ? (
                  <p
                    data-testid={`plugin-note-${plugin.name}`}
                    className="text-[11px] text-muted-foreground"
                  >
                    {rowNotes[plugin.name]}
                  </p>
                ) : null}

                {rowErrors[plugin.name] ? (
                  <p
                    data-testid={`plugin-error-${plugin.name}`}
                    className="text-[11px] text-destructive"
                  >
                    {rowErrors[plugin.name]}
                  </p>
                ) : null}
              </div>
            );
          })}

          {reloadError ? (
            <p
              data-testid="plugins-reload-error"
              className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
            >
              {reloadError}
            </p>
          ) : null}

          {reloadReport && reloadReport.generation === null ? (
            <p
              data-testid="plugins-reload-failed"
              className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
            >
              The reload failed; the engine kept its previous plugins. Nothing was unloaded and
              nothing new was picked up — the set below is what it was already running.
            </p>
          ) : null}

          {reloadReport && reloadReport.generation !== null ? (
            <div
              data-testid="plugins-reload-result"
              className="flex max-w-3xl flex-col gap-1.5 rounded-lg border border-border bg-card p-3 text-xs text-muted-foreground"
            >
              <p>
                Reloaded at generation {reloadReport.generation} — {reloadReport.loaded}{" "}
                {reloadReport.loaded === 1 ? "plugin" : "plugins"} loaded.
              </p>
              {attributed.byPlugin.map(({ name, failures }) => {
                const intended = failures.every((f) => f.reason === BY_OPERATOR);
                return (
                  <p
                    key={name}
                    data-testid={`plugins-reload-${intended ? "intended" : "failure"}-${name}`}
                    className={intended ? "text-muted-foreground" : "text-destructive"}
                  >
                    <span className="font-medium">{name}</span>
                    {intended
                      ? " — not loaded, because you turned it off. That is the decision arriving back, not a fault."
                      : ` — ${failures.map((f) => `${f.group}: ${f.reason}`).join("; ")}`}
                  </p>
                );
              })}
              {attributed.unmatched.length ? (
                <p data-testid="plugins-reload-unmatched">
                  Refusals this listing could not tie to an installed distribution:{" "}
                  {attributed.unmatched
                    .map((f) => `${f.group}/${f.name}: ${f.reason}`)
                    .join("; ")}
                </p>
              ) : null}
            </div>
          ) : null}
        </>
      ) : null}

      {pending && !poll.forbidden && listing !== null ? (
        <div
          data-testid="plugins-reload-bar"
          className="sticky bottom-0 -mx-4 -mb-20 flex flex-wrap items-center gap-3 border-t border-border bg-card/95 px-4 py-3 backdrop-blur"
        >
          <p className="min-w-0 flex-1 text-[11px] text-muted-foreground">
            The lockfile has been changed and the engine has not been told. It is still running the
            set it discovered — reload to apply what you recorded.
          </p>
          {reloadButton}
        </div>
      ) : null}
    </div>
  );
}

export default PluginsPage;
