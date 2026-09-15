"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Flag as FlagIcon, Loader2, RefreshCw } from "lucide-react";

import { PageHeader } from "@/components/business/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { isOperatorRole } from "@/components/layout/nav-config";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { BRIDGE_UNREACHABLE } from "@/lib/bridge/use-bridge-poll";
import { flagSections } from "@/lib/settings/flag-sections";
import {
  normalizeSchema,
  normalizeValues,
  type SettingField,
  type SettingSource,
  type SettingValue,
} from "@/lib/settings/schema";

/**
 * Settings › Flags — the governed flags, and what each one is actually doing.
 *
 * It replaces `views/controls-view.tsx`, and keeps that view's one load-bearing
 * rule: **a control with no evidence is a question, not a checkmark.** Only an
 * `ENFORCING` verdict renders affirmatively; `INERT`, `BLIND` and `UNKNOWN`
 * render as warnings. This repo has shipped six controls that were built,
 * wired, tested and doing nothing, and the screen that reported them green is
 * how they stayed that way for months.
 *
 * Three routes, each answering a different question:
 *
 * * `GET /api/settings/schema` — WHICH flags exist (`governed: true`), what
 *   each one is for, and the exact rungs the write path accepts. Driving the
 *   page from here rather than from `/api/controls` means a governed flag with
 *   no verdict row yet renders as a flag with no verdict, rather than as a
 *   flag that does not exist.
 * * `GET /api/settings` — which LAYER supplied the current value.
 * * `GET /api/controls` — the VERDICT: what the control has actually done, and
 *   when it last did it. The settings API has no verdict and does not pretend
 *   to.
 *
 * Writes go to `PATCH /api/controls/{name}`, not to the settings route. Both
 * reach `robothor.flags.store.set_flag`, but the controls route is the one
 * that takes a per-flag `reason`, and `feature_flag_audit.reason` answering
 * "who widened this, and why" six months later is the whole point of making an
 * operator type one.
 *
 * A write here is HOT: the store is read on a five-second TTL, so there is no
 * restart banner on this page and there should never be one.
 */

const BRIDGE = "/api/bridge";

/** Verdict statuses that may render affirmatively. Exactly one. */
const AFFIRMATIVE = new Set(["ENFORCING"]);
/** Zero evidence, no evidence table, or no verdict at all. */
const WARNING = new Set(["INERT", "BLIND", "UNKNOWN"]);

const SOURCE_LABEL: Record<SettingSource, string> = {
  default: "default",
  config: "config.yaml",
  env: "environment",
  db: "flag store",
  unknown: "unknown",
};

interface Verdict {
  status: string;
  message: string;
  lastFired: string | null;
  count7d: number;
}

interface Control {
  value: string;
  validValues: string[];
  verdict: Verdict;
}

/** What a flag with no row in `/api/controls` gets. Never a pass. */
const NO_VERDICT: Verdict = {
  status: "UNKNOWN",
  message:
    "The controls route did not report on this flag, so what it is doing right now is not known from here.",
  lastFired: null,
  count7d: 0,
};

function badgeClass(status: string): string {
  if (AFFIRMATIVE.has(status)) return "border-success/30 bg-success/10 text-success";
  if (WARNING.has(status)) return "border-warning/30 bg-warning/10 text-warning";
  // UNPROVEN, and anything this build has not seen: neutral, never green.
  return "border-border bg-muted text-muted-foreground";
}

function normalizeControls(body: unknown): Record<string, Control> {
  if (!Array.isArray(body)) return {};
  const out: Record<string, Control> = {};
  for (const entry of body) {
    if (!entry || typeof entry !== "object") continue;
    const row = entry as Record<string, unknown>;
    const name = typeof row.name === "string" ? row.name : "";
    if (!name) continue;
    const verdict = (row.verdict ?? {}) as Record<string, unknown>;
    out[name] = {
      value: typeof row.value === "string" ? row.value : "",
      validValues: Array.isArray(row.valid_values)
        ? row.valid_values.filter((v): v is string => typeof v === "string")
        : [],
      verdict: {
        status: typeof verdict.status === "string" ? verdict.status : NO_VERDICT.status,
        message: typeof verdict.message === "string" ? verdict.message : "",
        lastFired: typeof verdict.last_fired === "string" ? verdict.last_fired : null,
        count7d: typeof verdict.count_7d === "number" ? verdict.count_7d : 0,
      },
    };
  }
  return out;
}

function when(value: string | null): string {
  if (!value) return "never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export interface FlagsPageProps {
  /** The Settings container unmounts an inactive page; this is the load gate. */
  visible?: boolean;
  /** Session role. UX only — the bridge authorizes every controls route itself. */
  role?: string | null;
}

export function FlagsPage({ visible = true, role }: FlagsPageProps) {
  const [fields, setFields] = useState<SettingField[] | null>(null);
  const [values, setValues] = useState<Record<string, SettingValue>>({});
  const [controls, setControls] = useState<Record<string, Control>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [verdictError, setVerdictError] = useState<string | null>(null);

  const [picked, setPicked] = useState<Record<string, string>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [applying, setApplying] = useState<string | null>(null);

  const canWrite = isOperatorRole(role);

  /**
   * The verdicts, on their own.
   *
   * Separate from the schema/values load because it is the only one of the
   * three that is re-read after a write, and because it is the only one whose
   * failure must not take the page down: a flag whose verdict cannot be read
   * still has a value an operator may need to change.
   */
  const loadControls = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE}/api/controls`);
      if (!res.ok) {
        const refusal = await readBridgeReply(res);
        // Dropped, not kept. A banner reading "no flag below can be shown as
        // doing anything" above a row still badged ENFORCING is two
        // contradictory claims at once, and the green one is the one people
        // believe. Every row falls back to UNKNOWN, which is a warning.
        setControls({});
        setVerdictError(refusal);
        return;
      }
      setControls(normalizeControls(await res.json()));
      setVerdictError(null);
    } catch {
      setControls({});
      setVerdictError(BRIDGE_UNREACHABLE);
    }
  }, []);

  /**
   * The layer each flag's value came from.
   *
   * Its own loader, separate from the schema, because this is what a write
   * changes: a flag that was `env` before the write is `db` after it, and the
   * schema — 164 KB, derived from a registry that cannot change without a
   * restart — is not worth re-fetching to learn that.
   */
  const loadValues = useCallback(async (): Promise<string | null> => {
    const res = await fetch(`${BRIDGE}/api/settings`);
    if (!res.ok) return readBridgeReply(res);
    setValues(normalizeValues(await res.json()).values);
    return null;
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const schemaRes = await fetch(`${BRIDGE}/api/settings/schema`);
      if (!schemaRes.ok) {
        setError(await readBridgeReply(schemaRes));
        return;
      }
      const groups = normalizeSchema(await schemaRes.json());
      setFields(groups.flatMap((group) => group.fields).filter((field) => field.governed));
      setError(await loadValues());
    } catch {
      setError(BRIDGE_UNREACHABLE);
    } finally {
      setLoading(false);
    }
  }, [loadValues]);

  useEffect(() => {
    if (!visible) return;
    void load();
    void loadControls();
  }, [visible, load, loadControls]);

  const sections = useMemo(() => flagSections(fields ?? []), [fields]);

  /**
   * The value to show as current.
   *
   * `/api/settings` and `/api/controls` are tested against each other on the
   * bridge and agree on every flag, so either would do; the controls route is
   * preferred because it is the one this page re-reads after a write.
   */
  const currentOf = (field: SettingField): string => {
    const control = controls[field.name];
    if (control?.value) return control.value;
    const value = values[field.name]?.value;
    if (typeof value === "string") return value;
    if (typeof value === "boolean") return value ? "true" : "false";
    return "";
  };

  const rungsOf = (field: SettingField): string[] => {
    const control = controls[field.name];
    if (control?.validValues.length) return control.validValues;
    return field.choices ?? [];
  };

  async function apply(field: SettingField) {
    const value = picked[field.name];
    const reason = (reasons[field.name] ?? "").trim();
    // Repeated here rather than left to the disabled button: `disabled` is a
    // hint to the pointer, not a rule about what this function may send.
    if (!value || !reason || value === currentOf(field)) return;

    setApplying(field.name);
    setRowErrors((prev) => ({ ...prev, [field.name]: "" }));
    try {
      const res = await fetch(`${BRIDGE}/api/controls/${encodeURIComponent(field.name)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value, reason }),
      });
      if (!res.ok) {
        const refusal = await readBridgeReply(res);
        setRowErrors((prev) => ({ ...prev, [field.name]: refusal }));
        return;
      }
      setPicked((prev) => {
        const next = { ...prev };
        delete next[field.name];
        return next;
      });
      setReasons((prev) => ({ ...prev, [field.name]: "" }));
      // BOTH, because one row shows facts from both routes. The verdict is
      // what the control is DOING and it changed the moment the row was
      // written; the source pill and the env note come from /api/settings and
      // changed at the same instant, since the write is what put the operator
      // row there. Refreshing only the first leaves a row asserting a fresh
      // value beside a stale layer — on the screen whose purpose is saying
      // what is actually true.
      await Promise.all([
        loadControls(),
        loadValues().catch(() => {
          // The write landed and the verdict is being re-read; a values read
          // that failed on top of that is not worth taking the page down for.
          // The pill stays as it was until the next Refresh.
        }),
      ]);
    } catch {
      setRowErrors((prev) => ({ ...prev, [field.name]: BRIDGE_UNREACHABLE }));
    } finally {
      setApplying(null);
    }
  }

  if (!visible) return null;

  return (
    <div className="flex min-w-0 flex-col gap-4 p-4" data-testid="settings-page-flags">
      <PageHeader title="Flags" description="Governed controls, and what each one is doing.">
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            void load();
            void loadControls();
          }}
          data-testid="flags-refresh"
        >
          <RefreshCw aria-hidden className={loading ? "animate-spin" : undefined} />
          Refresh
        </Button>
      </PageHeader>

      <p className="max-w-3xl text-xs text-muted-foreground">
        A change here applies live — the engine reads these from the flag store, not from a file, so
        nothing needs restarting. The verdict beside each flag is evidence, not configuration: a
        control that has never fired is not proof that it is safe to promote, and it is not rendered
        as one.
      </p>

      {error ? (
        <p
          data-testid="flags-error"
          className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
        >
          {error}
        </p>
      ) : null}

      {verdictError ? (
        <p
          data-testid="flags-verdict-error"
          className="max-w-3xl rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning"
        >
          The verdicts could not be read ({verdictError}), so no flag below can be shown as doing
          anything. The values are still current, and a change still applies.
        </p>
      ) : null}

      {loading && fields === null ? (
        <div
          className="flex items-center gap-2 p-6 text-xs text-muted-foreground"
          data-testid="flags-loading"
        >
          <Loader2 aria-hidden className="size-4 animate-spin" />
          Reading the governed flags…
        </div>
      ) : null}

      {fields !== null && sections.length === 0 && !error ? (
        <p data-testid="flags-empty" className="max-w-3xl text-xs text-muted-foreground">
          This instance declares no governed flags. Every guardrail it runs is then fixed at build
          time, which is not how the platform ships — check that the bridge and the engine are the
          same version.
        </p>
      ) : null}

      {sections.map((section) => (
        <section
          key={section.id}
          data-testid={`flags-section-${section.id}`}
          className="flex min-w-0 flex-col gap-2"
        >
          <div className="flex min-w-0 flex-col gap-0.5">
            <h3 className="text-sm font-medium text-foreground">{section.label}</h3>
            <p className="max-w-3xl text-[11px] text-muted-foreground">{section.blurb}</p>
          </div>

          {section.fields.map((field) => {
            const control = controls[field.name];
            const verdict = control?.verdict ?? NO_VERDICT;
            const current = currentOf(field);
            const rungs = rungsOf(field);
            const chosen = picked[field.name] ?? current;
            const reason = reasons[field.name] ?? "";
            const source = values[field.name]?.source ?? "unknown";
            const canApply = Boolean(reason.trim()) && chosen !== current;

            return (
              <div
                key={field.name}
                data-testid={`flag-${field.name}`}
                className="flex min-w-0 flex-col gap-1.5 rounded-lg border border-border bg-card p-3"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="min-w-0 break-all font-mono text-[12px] text-foreground">
                    {field.env}
                  </span>
                  <span className="shrink-0 rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                    now: {current || "unset"}
                  </span>
                  <span
                    data-testid={`flag-source-${field.name}`}
                    className="shrink-0 rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] text-muted-foreground"
                  >
                    {SOURCE_LABEL[source]}
                  </span>
                  <span
                    data-testid={`flag-verdict-${field.name}`}
                    data-status={verdict.status}
                    className={`ml-auto shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium ${badgeClass(
                      verdict.status
                    )}`}
                  >
                    {verdict.status}
                  </span>
                </div>

                {field.description ? (
                  <p className="text-[11px] text-muted-foreground">{field.description}</p>
                ) : null}

                <p className="text-[11px] text-muted-foreground">{verdict.message}</p>
                <p className="text-[10px] text-muted-foreground/80">
                  Last fired {when(verdict.lastFired)} · {verdict.count7d} events / 7d
                </p>

                {source === "env" ? (
                  <p
                    data-testid={`flag-env-note-${field.name}`}
                    className="text-[11px] text-muted-foreground"
                  >
                    An environment variable on the box currently supplies this. A change saved here
                    writes an operator row, which outranks the variable — so it applies, and it
                    keeps applying until somebody removes the row on the box.
                  </p>
                ) : null}

                {canWrite ? (
                  <div className="flex min-w-0 flex-wrap items-center gap-2 pt-0.5">
                    <div
                      role="group"
                      aria-label={`${field.env} value`}
                      className="flex min-w-0 flex-wrap items-center gap-1 rounded-md border border-border p-0.5"
                    >
                      {rungs.map((rung) => {
                        const active = rung === chosen;
                        return (
                          <button
                            key={rung}
                            type="button"
                            aria-pressed={active}
                            data-testid={`flag-value-${field.name}-${rung}`}
                            onClick={() =>
                              setPicked((prev) => ({ ...prev, [field.name]: rung }))
                            }
                            className={`rounded px-2 py-1 text-xs transition-colors ${
                              active
                                ? "bg-primary/15 text-foreground"
                                : "text-muted-foreground hover:bg-accent/60"
                            }`}
                          >
                            {rung}
                          </button>
                        );
                      })}
                    </div>
                    <Input
                      data-testid={`flag-reason-${field.name}`}
                      placeholder="Reason (recorded in the audit log)"
                      value={reason}
                      onChange={(event) =>
                        setReasons((prev) => ({ ...prev, [field.name]: event.target.value }))
                      }
                      className="h-8 min-w-[160px] flex-1 text-sm sm:max-w-sm"
                    />
                    <Button
                      size="sm"
                      data-testid={`flag-apply-${field.name}`}
                      disabled={!canApply || applying === field.name}
                      onClick={() => void apply(field)}
                    >
                      {applying === field.name ? "Applying…" : "Apply"}
                    </Button>
                  </div>
                ) : (
                  <p
                    data-testid={`flag-readonly-${field.name}`}
                    className="text-[11px] italic text-muted-foreground"
                  >
                    Operator only — ask an owner or admin of this instance to change this flag.
                  </p>
                )}

                {rowErrors[field.name] ? (
                  <p
                    data-testid={`flag-error-${field.name}`}
                    className="text-[11px] text-destructive"
                  >
                    {rowErrors[field.name]}
                  </p>
                ) : null}
              </div>
            );
          })}
        </section>
      ))}

      {sections.length > 0 ? (
        <p className="flex max-w-3xl items-center gap-1.5 text-[11px] text-muted-foreground">
          <FlagIcon aria-hidden className="size-3" />
          Every change is recorded with its reason. The inventory behind these flags — owner,
          production mode, promotion date — lives in <span className="font-mono">infra/flags.yaml</span>.
        </p>
      ) : null}
    </div>
  );
}

export default FlagsPage;
