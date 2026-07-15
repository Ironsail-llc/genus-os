"use client";

import { useEffect, useState } from "react";

const BRIDGE_URL = "/api/bridge";

type WalStatus = { archived_count?: number; failed_count?: number;
  last_archived_time?: string | null; status?: string; error?: string };
type Backup = { unit: string; age_hours?: number | null; status?: string };
type Disk = { mount: string; used_pct?: number | null; free_gb?: number | null; status?: string };
type Health = {
  wal?: WalStatus;
  backups?: Backup[] | { status: string; error?: string };
  failed_units?: string[] | { status: string; error?: string };
  disks?: Disk[] | { status: string; error?: string };
  generated_at?: string;
};

// emerald only for a genuinely-good status; everything else (warn/unknown/failed) amber.
function toneFor(good: boolean): string {
  return good
    ? "border-emerald-500/50 bg-emerald-500/5"
    : "border-amber-500/50 bg-amber-500/5";
}

export function HealthView({ visible = true }: { visible?: boolean }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!visible) return;
    let active = true;
    (async () => {
      try {
        const res = await fetch(`${BRIDGE_URL}/api/health/system`);
        if (!active) return;
        if (!res.ok) {
          setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
          return;
        }
        const data = await res.json();
        if (!active) return;
        setHealth(data);
        setError(null);
      } catch {
        if (active) setError("Could not reach the bridge.");
      }
    })();
    return () => {
      active = false;
    };
  }, [visible]);

  const wal = health?.wal;
  const walGood = wal?.status === "ok";
  const backups = health?.backups;
  const backupsArr = Array.isArray(backups) ? backups : [];
  const backupsGood = Array.isArray(backups) && backups.every((b) => b.status === "ok");
  const units = health?.failed_units;
  const unitsArr = Array.isArray(units) ? units : [];
  const unitsGood = Array.isArray(units) && units.length === 0;
  const disks = health?.disks;
  const disksArr = Array.isArray(disks) ? disks : [];
  const disksGood = Array.isArray(disks) && disks.every((d) => d.status === "ok");

  return (
    <div data-testid="health-view" className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}>
      <h2 className="text-lg font-semibold text-zinc-100">Health</h2>
      {error && <p className="text-amber-400 text-sm">{error}</p>}
      {health && (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <div data-testid="health-wal" className={`rounded-md border p-3 ${toneFor(walGood)}`}>
            <div className="font-medium text-zinc-100">WAL archiving</div>
            <div className="text-xs text-zinc-400">
              archived {wal?.archived_count ?? "—"}, failed {wal?.failed_count ?? "—"},
              last {wal?.last_archived_time ?? "never"}
            </div>
          </div>

          <div data-testid="health-backups" className={`rounded-md border p-3 ${toneFor(backupsGood)}`}>
            <div className="font-medium text-zinc-100">Backups</div>
            {backupsArr.length === 0 && (
              <div className="text-xs text-amber-300">probe unknown</div>
            )}
            {backupsArr.map((b) => (
              <div key={b.unit} className="text-xs text-zinc-400">
                {b.unit}: {b.age_hours == null ? "never" : `${b.age_hours.toFixed(1)}h ago`} ({b.status})
              </div>
            ))}
          </div>

          <div data-testid="health-units" className={`rounded-md border p-3 ${toneFor(unitsGood)}`}>
            <div className="font-medium text-zinc-100">Failed units</div>
            <div className="text-xs text-zinc-400">
              {unitsArr.length === 0 ? "none" : unitsArr.join(", ")}
            </div>
          </div>

          <div data-testid="health-disks" className={`rounded-md border p-3 ${toneFor(disksGood)}`}>
            <div className="font-medium text-zinc-100">Disks</div>
            {disksArr.map((d) => (
              <div key={d.mount} className="text-xs text-zinc-400">
                {d.mount}: {d.used_pct ?? "—"}% used, {d.free_gb ?? "—"} GB free ({d.status})
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default HealthView;
