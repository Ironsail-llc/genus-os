"use client";

import { useState, useEffect, useCallback } from "react";
import { useSession } from "next-auth/react";
import { RefreshCw, ShieldAlert } from "lucide-react";
import { PageHeader } from "@/components/business/page-header";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";

interface ControlVerdict {
  status: string;
  message: string;
  last_fired: string | null;
  count_7d: number;
}

interface Control {
  name: string;
  value: string;
  valid_values: string[];
  verdict: ControlVerdict;
}

interface ControlsViewProps {
  visible?: boolean;
}

// Browser calls stay on the authenticated same-origin BFF. The BFF alone owns
// the internal Bridge address and forwards the signed-in caller's token — see
// src/app/api/bridge/[...path]/route.ts.
//
// The bridge handler (crm/bridge/routers/controls.py::_require_operator) is
// the real enforcement point: it 403s any service token AND any human whose
// role isn't in {owner, admin} — dashboard SSO admits every verified org
// member (viewer, user, member, auditor, ...), not just the operator. This
// component mirrors that same allow-list to hide the write control from
// non-operators; it's a UX nicety only, never the security boundary.
const BRIDGE_URL = "/api/bridge";

// Mirrors crm/bridge/routers/controls.py::OPERATOR_ROLES.
const OPERATOR_ROLES = new Set(["owner", "admin"]);

// THE ONE HONESTY RULE: a flag whose verdict.status is INERT / BLIND /
// UNKNOWN must render as a warning, never as healthy/green. Only ENFORCING
// renders affirmatively. Zero evidence reads as a question, not a checkmark.
const AFFIRMATIVE_STATUSES = new Set(["ENFORCING"]);
const WARNING_STATUSES = new Set(["INERT", "BLIND", "UNKNOWN"]);

function badgeClassFor(status: string): string {
  if (AFFIRMATIVE_STATUSES.has(status)) {
    return "bg-success/10 text-success border-success/30";
  }
  if (WARNING_STATUSES.has(status)) {
    return "bg-warning/10 text-warning border-warning/30";
  }
  // UNPROVEN and any unrecognized status: neutral question mark, never green.
  return "bg-muted text-muted-foreground border-border";
}

interface Draft {
  value: string;
  reason: string;
}

export function ControlsView({ visible = true }: ControlsViewProps) {
  const { data: session } = useSession();
  const isOperator = OPERATOR_ROLES.has(session?.role ?? "");
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
        <PageHeader title="Controls" description="Governed platform flags with live enforcement verdicts">
          <button
            onClick={fetchControls}
            disabled={loading}
            className="p-1 rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            title="Refresh"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          </button>
        </PageHeader>

        {message && (
          <div
            className={`text-sm px-3 py-2 rounded-md ${
              message.type === "success"
                ? "bg-success/10 text-success"
                : "bg-destructive/10 text-destructive"
            }`}
          >
            {message.text}
          </div>
        )}

        {error && (
          <div className="text-sm px-3 py-2 rounded-md bg-destructive/10 text-destructive">{error}</div>
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
              const values = control.valid_values;
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

                  {isOperator ? (
                    <div className="flex items-center gap-2 flex-wrap">
                      <NativeSelect
                        value={draft.value}
                        onChange={(e) => setDraftValue(control.name, e.target.value)}
                        data-testid={`select-${control.name}`}
                      >
                        {values.map((v) => (
                          <option key={v} value={v}>
                            {v}
                          </option>
                        ))}
                      </NativeSelect>
                      <Input
                        type="text"
                        placeholder="Reason (required)"
                        value={draft.reason}
                        onChange={(e) => setDraftReason(control.name, e.target.value)}
                        className="h-8 flex-1 min-w-[160px] text-sm"
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
                  ) : (
                    <p
                      className="text-[10px] text-muted-foreground italic"
                      data-testid={`readonly-note-${control.name}`}
                    >
                      Operator only — ask an owner or admin to change this control.
                    </p>
                  )}
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
