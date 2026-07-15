"use client";

import { useState, useEffect, useCallback } from "react";
import { RefreshCw, ShieldAlert } from "lucide-react";

interface ControlVerdict {
  status: string;
  message: string;
  last_fired: string | null;
  count_7d: number;
}

interface Control {
  name: string;
  value: string;
  verdict: ControlVerdict;
}

interface ControlsViewProps {
  visible?: boolean;
}

// Browser calls stay on the authenticated same-origin BFF. The BFF alone owns
// the internal Bridge address and forwards the signed-in caller's token — see
// src/app/api/bridge/[...path]/route.ts. The controls API is operator-only at
// the bridge handler itself (crm/bridge/routers/controls.py rejects service
// tokens with 403), so the dashboard session reaching this view is always the
// operator; no additional client-side role gate is needed here.
const BRIDGE_URL = "/api/bridge";

// Mirrors crm/bridge/routers/controls.py::_valid_values_for — mode-ladder
// flags ("*_MODE") and boolean flags ("*_ENABLED") have disjoint value sets.
const MODE_VALUES = ["off", "observe", "alert", "enforce"];
const BOOL_VALUES = ["true", "false"];

function validValuesFor(name: string): string[] {
  return name.endsWith("_ENABLED") ? BOOL_VALUES : MODE_VALUES;
}

// THE ONE HONESTY RULE: a flag whose verdict.status is INERT / BLIND /
// UNKNOWN must render as a warning, never as healthy/green. Only ENFORCING
// renders affirmatively. Zero evidence reads as a question, not a checkmark.
const AFFIRMATIVE_STATUSES = new Set(["ENFORCING"]);
const WARNING_STATUSES = new Set(["INERT", "BLIND", "UNKNOWN"]);

function badgeClassFor(status: string): string {
  if (AFFIRMATIVE_STATUSES.has(status)) {
    return "bg-emerald-500/10 text-emerald-500 border-emerald-500/30";
  }
  if (WARNING_STATUSES.has(status)) {
    return "bg-amber-500/10 text-amber-500 border-amber-500/30";
  }
  // UNPROVEN and any unrecognized status: neutral question mark, never green.
  return "bg-zinc-500/10 text-zinc-400 border-zinc-500/30";
}

interface Draft {
  value: string;
  reason: string;
}

export function ControlsView({ visible = true }: ControlsViewProps) {
  const [controls, setControls] = useState<Control[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);

  const fetchControls = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${BRIDGE_URL}/api/controls`);
      if (res.ok) {
        const data: Control[] = await res.json();
        setControls(data);
      } else {
        setError(`Failed to load controls (${res.status})`);
      }
    } catch {
      setError("Failed to load controls: network error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (visible) fetchControls();
  }, [visible, fetchControls]);

  const draftFor = (control: Control): Draft =>
    drafts[control.name] ?? { value: control.value, reason: "" };

  const setDraftValue = (name: string, value: string) => {
    setDrafts((prev) => ({ ...prev, [name]: { value, reason: prev[name]?.reason ?? "" } }));
  };

  const setDraftReason = (name: string, reason: string) => {
    setDrafts((prev) => ({ ...prev, [name]: { value: prev[name]?.value ?? "", reason } }));
  };

  const handleSave = async (control: Control) => {
    const draft = draftFor(control);
    if (!draft.reason.trim() || draft.value === control.value) return;

    setSaving(control.name);
    setMessage(null);
    try {
      const res = await fetch(`${BRIDGE_URL}/api/controls/${control.name}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: draft.value, reason: draft.reason.trim() }),
      });
      if (res.ok) {
        setMessage({ text: `${control.name} set to ${draft.value}`, type: "success" });
        setDrafts((prev) => {
          const next = { ...prev };
          delete next[control.name];
          return next;
        });
        fetchControls();
      } else {
        const err = await res.json().catch(() => ({ detail: undefined }));
        setMessage({ text: err.detail || "Update failed", type: "error" });
      }
    } catch {
      setMessage({ text: "Update failed: network error", type: "error" });
    } finally {
      setSaving(null);
    }
  };

  return (
    <div
      className="h-full w-full flex flex-col overflow-y-auto"
      style={{ display: visible ? "flex" : "none" }}
      data-testid="controls-view"
    >
      <div className="p-4 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Controls</h2>
          <button
            onClick={fetchControls}
            disabled={loading}
            className="p-1 rounded hover:bg-accent"
            title="Refresh"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>

        {message && (
          <div
            className={`text-sm px-3 py-2 rounded-md ${
              message.type === "success"
                ? "bg-emerald-500/10 text-emerald-500"
                : "bg-red-500/10 text-red-500"
            }`}
          >
            {message.text}
          </div>
        )}

        {error && (
          <div className="text-sm px-3 py-2 rounded-md bg-red-500/10 text-red-500">{error}</div>
        )}

        {loading && controls.length === 0 ? (
          <div className="flex items-center justify-center py-12 text-muted-foreground text-sm">
            Loading controls...
          </div>
        ) : controls.length === 0 && !error ? (
          <div className="text-center py-8 text-muted-foreground text-sm">
            <ShieldAlert className="w-8 h-8 mx-auto mb-2 opacity-40" />
            No governed controls found.
          </div>
        ) : (
          <div className="space-y-2">
            {controls.map((control) => {
              const draft = draftFor(control);
              const values = validValuesFor(control.name);
              const canApply = draft.reason.trim().length > 0 && draft.value !== control.value;
              return (
                <div
                  key={control.name}
                  className="p-3 rounded-lg border border-border bg-card space-y-2"
                  data-testid={`control-${control.name}`}
                >
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="font-medium text-sm truncate">{control.name}</span>
                      <span className="text-xs text-muted-foreground">mode: {control.value}</span>
                    </div>
                    <span
                      data-testid={`verdict-${control.name}`}
                      data-status={control.verdict.status}
                      className={`text-[10px] px-2 py-0.5 rounded-full border font-medium ${badgeClassFor(
                        control.verdict.status
                      )}`}
                    >
                      {control.verdict.status}
                    </span>
                  </div>

                  <p className="text-xs text-muted-foreground">{control.verdict.message}</p>

                  {control.verdict.last_fired && (
                    <p className="text-[10px] text-muted-foreground">
                      Last fired {control.verdict.last_fired} &middot; {control.verdict.count_7d} events / 7d
                    </p>
                  )}

                  <div className="flex items-center gap-2 flex-wrap">
                    <select
                      value={draft.value}
                      onChange={(e) => setDraftValue(control.name, e.target.value)}
                      className="text-sm bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-zinc-200"
                      data-testid={`select-${control.name}`}
                    >
                      {values.map((v) => (
                        <option key={v} value={v}>
                          {v}
                        </option>
                      ))}
                    </select>
                    <input
                      type="text"
                      placeholder="Reason (required)"
                      value={draft.reason}
                      onChange={(e) => setDraftReason(control.name, e.target.value)}
                      className="flex-1 min-w-[160px] px-2 py-1 text-sm rounded-md border border-input bg-background"
                      data-testid={`reason-${control.name}`}
                    />
                    <button
                      onClick={() => handleSave(control)}
                      disabled={saving === control.name || !canApply}
                      className="px-3 py-1 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                      data-testid={`save-${control.name}`}
                    >
                      {saving === control.name ? "Saving..." : "Apply"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

export default ControlsView;
