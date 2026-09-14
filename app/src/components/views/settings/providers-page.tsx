"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { KeyRound, Loader2, Plus, RefreshCw, Trash2, Zap } from "lucide-react";

import { PageHeader } from "@/components/business/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";

/**
 * Providers: credentials in, digests out.
 *
 * The shapes here are the bridge's, read off `crm/bridge/routers/providers.py`
 * and `robothor/engine/admin_providers.py` — not invented. The single property
 * this screen is built around is that a credential travels IN only: the field
 * that takes one is a password input bound to component state, the value is
 * sent in a request BODY (never a query string), it is cleared on save and on
 * cancel, and nothing that comes back from the bridge is ever key material —
 * only a `sha256:` digest, a source, and a verdict.
 *
 * Errors render the server's own sentence. The bridge refuses a key written
 * past a numbering gap, and a removal that would strand a spare, with a
 * message that names the slot to fix first; replacing that with "Something
 * went wrong" would throw away the only instruction the operator needs.
 */

const BRIDGE = "/api/bridge";

/** Mirrors robothor/engine/key_pool.py::MAX_KEY_SLOTS. */
const MAX_KEY_SLOTS = 16;

export interface ProviderSlot {
  /** The STORAGE slot, which is what the key routes address. */
  position: number;
  source: string; // "vault" | "env"
  fingerprint: string;
  state: string; // active | spare | capped | revoked | orphaned
  updated_at: string | null;
}

export interface Provider {
  id: string;
  label: string;
  configured: boolean;
  slots: ProviderSlot[];
  env_var: string;
  default_model: string;
}

export interface ModelEntry {
  id: string;
  provider: string;
  context_window: number | null;
  supports_thinking: boolean;
  supports_tools: boolean | null;
  source: string;
}

interface TestVerdict {
  ok: boolean;
  model: string;
  latency_ms: number;
  error_class: string | null;
  message?: string;
}

interface DefaultsResult {
  model: string;
  fallbacks: string[];
  applied: boolean;
}

/** The server's own words, whenever it gave any. */
async function readError(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object") {
      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail.trim()) return detail;
      if (Array.isArray(detail)) {
        const messages = detail
          .map((item) => (item && typeof item === "object" ? (item as { msg?: string }).msg : null))
          .filter((msg): msg is string => Boolean(msg));
        if (messages.length) return messages.join("; ");
      }
      const error = (body as { error?: unknown }).error;
      if (typeof error === "string" && error.trim()) return error;
    }
  } catch {
    // A non-JSON body is not a reason to lose the status code below.
  }
  return `The bridge refused the request (HTTP ${res.status}).`;
}

function stateClass(state: string): string {
  if (state === "active") return "border-success/30 bg-success/10 text-success";
  if (state === "spare") return "border-info/30 bg-info/10 text-info";
  if (state === "orphaned") return "border-warning/30 bg-warning/10 text-warning";
  if (state === "capped" || state === "revoked")
    return "border-destructive/30 bg-destructive/10 text-destructive";
  return "border-border bg-muted text-muted-foreground";
}

function formatContext(tokens: number | null): string {
  if (!tokens || tokens <= 0) return "context unknown";
  return `${tokens.toLocaleString("en-US")} tokens`;
}

function formatUpdated(value: string | null): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

/** The slots this provider holds, plus the one free slot a spare may go into. */
function slotChoices(provider: Provider): number[] {
  const occupied = provider.slots.map((slot) => slot.position).sort((a, b) => a - b);
  const choices = [...occupied];
  for (let position = 1; position <= MAX_KEY_SLOTS; position += 1) {
    if (!occupied.includes(position)) {
      choices.push(position);
      break;
    }
  }
  // Sorted, because a provider holding a stranded key (slots 1 and 3, say)
  // would otherwise offer the free slot 2 after slot 3 — and slot 2 is exactly
  // the one the bridge will tell the operator to fill first.
  return choices.sort((a, b) => a - b);
}

function defaultSlot(provider: Provider): number {
  const vault = provider.slots.find((slot) => slot.source === "vault");
  if (vault) return vault.position;
  return slotChoices(provider)[0] ?? 1;
}

/** One `<label>`-shaped caption, visible only where the table has collapsed. */
function CellLabel({ children }: { children: string }) {
  return (
    <span className="shrink-0 text-[11px] uppercase tracking-[0.08em] text-muted-foreground md:hidden">
      {children}
    </span>
  );
}

const CELL =
  "flex items-start justify-between gap-3 px-3 py-1.5 text-sm md:table-cell md:py-2.5 md:align-top";

interface KeyFormState {
  providerId: string;
  value: string;
  slot: number;
  verdict: TestVerdict | null;
  error: string | null;
  busy: "test" | "save" | null;
}

export function ProvidersPage() {
  const [providers, setProviders] = useState<Provider[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [models, setModels] = useState<ModelEntry[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);

  const [verdicts, setVerdicts] = useState<Record<string, TestVerdict>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [form, setForm] = useState<KeyFormState | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  const [primary, setPrimary] = useState("");
  const [fallbacks, setFallbacks] = useState<string[]>([]);
  const [savingDefaults, setSavingDefaults] = useState(false);
  const [defaultsResult, setDefaultsResult] = useState<DefaultsResult | null>(null);
  const [defaultsError, setDefaultsError] = useState<string | null>(null);

  const loadProviders = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${BRIDGE}/api/providers`);
      if (!res.ok) {
        setListError(await readError(res));
        return;
      }
      const body = (await res.json()) as { providers?: Provider[] };
      setProviders(body.providers ?? []);
      setListError(null);
    } catch {
      setListError("The dashboard could not reach the bridge. Check that the service is running.");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadModels = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE}/api/models`);
      if (!res.ok) {
        setModelsError(await readError(res));
        return;
      }
      const body = (await res.json()) as { models?: ModelEntry[] };
      setModels(body.models ?? []);
      setModelsError(null);
    } catch {
      setModelsError("The dashboard could not reach the bridge to read the model catalog.");
    }
  }, []);

  useEffect(() => {
    void loadProviders();
    void loadModels();
  }, [loadProviders, loadModels]);

  const providerLabels = useMemo(() => {
    const labels: Record<string, string> = {};
    for (const provider of providers ?? []) labels[provider.id] = provider.label;
    return labels;
  }, [providers]);

  /**
   * Models grouped by the provider that bills them, in the order the providers
   * listing uses, with anything the credential catalog does not cover — a
   * plugin's own namespace, a local Ollama — kept at the end rather than
   * dropped.
   */
  const modelGroups = useMemo(() => {
    const groups = new Map<string, ModelEntry[]>();
    for (const model of models) {
      const existing = groups.get(model.provider);
      if (existing) existing.push(model);
      else groups.set(model.provider, [model]);
    }
    const known = (providers ?? []).map((provider) => provider.id);
    const ordered = [
      ...known.filter((id) => groups.has(id)),
      ...[...groups.keys()].filter((id) => !known.includes(id)),
    ];
    return ordered.map((id) => ({
      id,
      label: providerLabels[id] ?? id,
      models: (groups.get(id) ?? []).slice().sort((a, b) => a.id.localeCompare(b.id)),
    }));
  }, [models, providers, providerLabels]);

  const setRowError = (providerId: string, message: string | null) =>
    setRowErrors((prev) => {
      const next = { ...prev };
      if (message) next[providerId] = message;
      else delete next[providerId];
      return next;
    });

  async function runTest(provider: Provider) {
    setTesting(provider.id);
    setRowError(provider.id, null);
    try {
      const res = await fetch(`${BRIDGE}/api/providers/${provider.id}/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        setRowError(provider.id, await readError(res));
        return;
      }
      const verdict = (await res.json()) as TestVerdict;
      setVerdicts((prev) => ({ ...prev, [provider.id]: verdict }));
    } catch {
      setRowError(provider.id, "The dashboard could not reach the bridge to run that test.");
    } finally {
      setTesting(null);
    }
  }

  function openForm(provider: Provider) {
    setRowError(provider.id, null);
    setForm({
      providerId: provider.id,
      value: "",
      slot: defaultSlot(provider),
      verdict: null,
      error: null,
      busy: null,
    });
  }

  /** The only exit from the form. The typed value dies with this call. */
  function closeForm() {
    setForm(null);
  }

  async function testCandidate(provider: Provider) {
    if (!form || form.providerId !== provider.id || !form.value.trim()) return;
    setForm({ ...form, busy: "test", error: null, verdict: null });
    try {
      const res = await fetch(`${BRIDGE}/api/providers/${provider.id}/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // The candidate key, sent once, in a body. It is not stored by this call.
        body: JSON.stringify({ api_key: form.value }),
      });
      if (!res.ok) {
        const message = await readError(res);
        setForm((prev) => (prev ? { ...prev, busy: null, error: message } : prev));
        return;
      }
      const verdict = (await res.json()) as TestVerdict;
      setForm((prev) => (prev ? { ...prev, busy: null, verdict } : prev));
    } catch {
      setForm((prev) =>
        prev
          ? { ...prev, busy: null, error: "The dashboard could not reach the bridge to test that key." }
          : prev
      );
    }
  }

  async function saveKey(provider: Provider) {
    if (!form || form.providerId !== provider.id || !form.value.trim()) return;
    setForm({ ...form, busy: "save", error: null });
    try {
      const res = await fetch(`${BRIDGE}/api/providers/${provider.id}/keys/${form.slot}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: form.value }),
      });
      if (!res.ok) {
        const message = await readError(res);
        setForm((prev) => (prev ? { ...prev, busy: null, error: message } : prev));
        return;
      }
      closeForm();
      await loadProviders();
    } catch {
      setForm((prev) =>
        prev
          ? { ...prev, busy: null, error: "The dashboard could not reach the bridge to save that key." }
          : prev
      );
    }
  }

  async function removeKey(provider: Provider, position: number) {
    setRowError(provider.id, null);
    try {
      const res = await fetch(`${BRIDGE}/api/providers/${provider.id}/keys/${position}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        setRowError(provider.id, await readError(res));
        return;
      }
      setConfirming(null);
      await loadProviders();
    } catch {
      setRowError(provider.id, "The dashboard could not reach the bridge to remove that key.");
    }
  }

  async function saveDefaults() {
    if (!primary) return;
    setSavingDefaults(true);
    setDefaultsError(null);
    try {
      const res = await fetch(`${BRIDGE}/api/providers/defaults`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: primary, fallbacks: fallbacks.filter(Boolean) }),
      });
      if (!res.ok) {
        setDefaultsError(await readError(res));
        return;
      }
      setDefaultsResult((await res.json()) as DefaultsResult);
    } catch {
      setDefaultsError("The dashboard could not reach the bridge to save the fleet default.");
    } finally {
      setSavingDefaults(false);
    }
  }

  const rows = providers ?? [];

  return (
    <div className="flex flex-col gap-5 p-4" data-testid="settings-page-providers">
      <PageHeader
        title="Providers"
        description="Keys go in; only digests come back."
      >
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            void loadProviders();
            void loadModels();
          }}
          data-testid="providers-refresh"
        >
          <RefreshCw aria-hidden />
          Refresh
        </Button>
      </PageHeader>

      <p className="max-w-3xl text-xs text-muted-foreground">
        A credential typed here is written to the instance vault and never shown again — this page
        reports a keyed <span className="font-mono">sha256:</span> digest, where the key is stored,
        and what happened the last time it was dialled. Test connection makes a real completion, so
        “configured” and “working” stay two different words.
      </p>

      <div data-testid="providers-page" className="flex flex-col gap-3">
        {loading && !providers ? (
          <div
            className="flex items-center gap-2 rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground"
            data-testid="providers-loading"
          >
            <Loader2 aria-hidden className="size-4 animate-spin" />
            Reading the provider state from the bridge…
          </div>
        ) : null}

        {listError ? (
          <div
            className="flex flex-col gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-4"
            data-testid="providers-error"
          >
            <p className="text-sm font-medium text-destructive">The provider listing failed</p>
            <p className="text-xs text-muted-foreground">{listError}</p>
            <div>
              <Button variant="outline" size="sm" onClick={() => void loadProviders()}>
                Try again
              </Button>
            </div>
          </div>
        ) : null}

        {!listError && !loading && rows.length === 0 ? (
          <div
            className="rounded-lg border border-dashed border-border bg-card/40 p-4"
            data-testid="providers-empty"
          >
            <p className="text-sm font-medium text-foreground">No providers to configure</p>
            <p className="mt-1 max-w-xl text-xs text-muted-foreground">
              This build of the engine carries no provider catalog, so there is nothing for a key to
              belong to. Check that the engine is running the same version as the dashboard.
            </p>
          </div>
        ) : null}

        {!listError && rows.length > 0 ? (
          <div className="overflow-x-auto rounded-lg border border-border bg-card">
            <table className="w-full border-collapse text-sm">
              <thead className="hidden md:table-header-group">
                <tr className="border-b border-border text-left text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Provider</th>
                  <th className="px-3 py-2 font-medium">Key</th>
                  <th className="px-3 py-2 font-medium">Source</th>
                  <th className="px-3 py-2 font-medium">Last test</th>
                  <th className="px-3 py-2 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="block md:table-row-group">
                {rows.map((provider) => {
                  const verdict = verdicts[provider.id];
                  const rowError = rowErrors[provider.id];
                  const sources = [...new Set(provider.slots.map((slot) => slot.source))];
                  const hasEnv = sources.includes("env");
                  const isOpen = form?.providerId === provider.id;

                  return (
                    <tr
                      key={provider.id}
                      data-testid={`provider-row-${provider.id}`}
                      className="block border-b border-border last:border-b-0 md:table-row"
                    >
                      <td className={CELL}>
                        <CellLabel>Provider</CellLabel>
                        <div className="flex flex-col items-end text-right md:items-start md:text-left">
                          <span className="font-medium text-foreground">{provider.label}</span>
                          <span className="font-mono text-[11px] text-muted-foreground">
                            {provider.default_model}
                          </span>
                        </div>
                      </td>

                      <td className={CELL}>
                        <CellLabel>Key</CellLabel>
                        <div className="flex flex-col items-end gap-1.5 md:items-start">
                          <Badge
                            variant="outline"
                            data-testid={`provider-key-state-${provider.id}`}
                            className={
                              provider.configured
                                ? "border-success/30 bg-success/10 text-success"
                                : "border-border bg-muted text-muted-foreground"
                            }
                          >
                            {provider.configured ? "Set" : "Not set"}
                          </Badge>
                          {provider.slots.map((slot) => {
                            const confirmId = `${provider.id}:${slot.position}`;
                            const readOnly = slot.source === "env";
                            const updated = formatUpdated(slot.updated_at);
                            return (
                              <div
                                key={slot.position}
                                data-testid={`provider-slot-${provider.id}-${slot.position}`}
                                className="flex flex-col items-end gap-1 md:items-start"
                              >
                                <div className="flex flex-wrap items-center justify-end gap-1.5 md:justify-start">
                                  <span className="text-[11px] text-muted-foreground">
                                    slot {slot.position}
                                  </span>
                                  <span className="font-mono text-xs text-foreground">
                                    {slot.fingerprint}
                                  </span>
                                  <Badge variant="outline" className={stateClass(slot.state)}>
                                    {slot.state}
                                  </Badge>
                                  <Button
                                    variant="ghost"
                                    size="icon-xs"
                                    disabled={readOnly}
                                    aria-label={`Remove the ${provider.label} key in slot ${slot.position}`}
                                    data-testid={`provider-remove-${provider.id}-${slot.position}`}
                                    onClick={() => setConfirming(confirmId)}
                                  >
                                    <Trash2 aria-hidden />
                                  </Button>
                                </div>
                                {updated ? (
                                  <span className="text-[11px] text-muted-foreground">
                                    changed {updated}
                                  </span>
                                ) : null}
                                {readOnly ? (
                                  <span
                                    className="text-[11px] text-muted-foreground"
                                    data-testid={`provider-remove-reason-${provider.id}-${slot.position}`}
                                  >
                                    Set in the environment as{" "}
                                    <span className="font-mono">{provider.env_var}</span> — remove it
                                    there, or write a vault key to this slot to override it.
                                  </span>
                                ) : null}
                                {confirming === confirmId ? (
                                  <div className="flex flex-wrap items-center justify-end gap-2 rounded-md border border-border bg-background px-2 py-1.5 md:justify-start">
                                    <span className="text-xs text-foreground">
                                      Remove slot {slot.position}? The key is deleted from the vault.
                                    </span>
                                    <Button
                                      variant="destructive"
                                      size="xs"
                                      data-testid={`provider-remove-confirm-${provider.id}-${slot.position}`}
                                      onClick={() => void removeKey(provider, slot.position)}
                                    >
                                      Remove key
                                    </Button>
                                    <Button
                                      variant="ghost"
                                      size="xs"
                                      data-testid={`provider-remove-cancel-${provider.id}-${slot.position}`}
                                      onClick={() => setConfirming(null)}
                                    >
                                      Keep it
                                    </Button>
                                  </div>
                                ) : null}
                              </div>
                            );
                          })}
                        </div>
                      </td>

                      <td className={CELL}>
                        <CellLabel>Source</CellLabel>
                        <div
                          className="flex flex-wrap items-center justify-end gap-1.5 md:justify-start"
                          data-testid={`provider-source-${provider.id}`}
                        >
                          {sources.length === 0 ? (
                            <span className="text-xs text-muted-foreground">—</span>
                          ) : (
                            sources.map((source) => (
                              <Badge key={source} variant="outline" className="text-muted-foreground">
                                {source}
                              </Badge>
                            ))
                          )}
                          {hasEnv ? (
                            <Badge
                              variant="outline"
                              className="border-warning/30 bg-warning/10 text-warning"
                            >
                              read-only
                            </Badge>
                          ) : null}
                        </div>
                      </td>

                      <td className={CELL}>
                        <CellLabel>Last test</CellLabel>
                        <div className="flex flex-col items-end gap-1 text-right md:items-start md:text-left">
                          <span
                            aria-live="polite"
                            data-testid={`provider-test-result-${provider.id}`}
                            className={
                              verdict
                                ? verdict.ok
                                  ? "text-xs text-success"
                                  : "text-xs text-destructive"
                                : "text-xs text-muted-foreground"
                            }
                          >
                            {verdict
                              ? verdict.ok
                                ? `Answered in ${verdict.latency_ms}ms on ${verdict.model}.`
                                : `${verdict.error_class ?? "failed"} — ${verdict.message ?? "the provider refused the call."}`
                              : "Not tested in this session."}
                          </span>
                          {rowError ? (
                            <span
                              className="text-xs text-destructive"
                              data-testid={`provider-error-${provider.id}`}
                            >
                              {rowError}
                            </span>
                          ) : null}
                        </div>
                      </td>

                      <td className={CELL}>
                        <CellLabel>Actions</CellLabel>
                        <div className="flex flex-col items-end gap-2 md:items-start">
                          <div className="flex flex-wrap items-center justify-end gap-1.5 md:justify-start">
                            <Button
                              variant="outline"
                              size="xs"
                              disabled={testing === provider.id}
                              data-testid={`provider-test-${provider.id}`}
                              onClick={() => void runTest(provider)}
                            >
                              {testing === provider.id ? (
                                <Loader2 aria-hidden className="animate-spin" />
                              ) : (
                                <Zap aria-hidden />
                              )}
                              Test
                            </Button>
                            <Button
                              variant="outline"
                              size="xs"
                              data-testid={`provider-add-${provider.id}`}
                              onClick={() => (isOpen ? closeForm() : openForm(provider))}
                            >
                              {provider.configured ? <KeyRound aria-hidden /> : <Plus aria-hidden />}
                              {provider.configured ? "Rotate" : "Add key"}
                            </Button>
                          </div>

                          {isOpen && form ? (
                            <form
                              data-testid={`provider-key-form-${provider.id}`}
                              className="flex w-full max-w-md flex-col gap-2 rounded-md border border-border bg-background p-2.5"
                              onSubmit={(event) => {
                                event.preventDefault();
                                void saveKey(provider);
                              }}
                            >
                              <label
                                className="text-[11px] uppercase tracking-[0.08em] text-muted-foreground"
                                htmlFor={`provider-key-input-${provider.id}`}
                              >
                                API key
                              </label>
                              <Input
                                id={`provider-key-input-${provider.id}`}
                                data-testid={`provider-key-input-${provider.id}`}
                                type="password"
                                autoComplete="off"
                                spellCheck={false}
                                placeholder={`Paste the ${provider.label} key`}
                                value={form.value}
                                onChange={(event) =>
                                  setForm((prev) =>
                                    prev ? { ...prev, value: event.target.value, verdict: null } : prev
                                  )
                                }
                              />
                              <div className="flex items-center gap-2">
                                <label
                                  className="text-[11px] uppercase tracking-[0.08em] text-muted-foreground"
                                  htmlFor={`provider-key-slot-${provider.id}`}
                                >
                                  Slot
                                </label>
                                <NativeSelect
                                  id={`provider-key-slot-${provider.id}`}
                                  data-testid={`provider-key-slot-${provider.id}`}
                                  value={String(form.slot)}
                                  onChange={(event) =>
                                    setForm((prev) =>
                                      prev ? { ...prev, slot: Number(event.target.value) } : prev
                                    )
                                  }
                                >
                                  {slotChoices(provider).map((position) => {
                                    const existing = provider.slots.find(
                                      (slot) => slot.position === position
                                    );
                                    return (
                                      <option key={position} value={String(position)}>
                                        {existing
                                          ? `Replace slot ${position} (${existing.fingerprint})`
                                          : `Add as slot ${position} (spare)`}
                                      </option>
                                    );
                                  })}
                                </NativeSelect>
                              </div>
                              <p className="text-[11px] text-muted-foreground">
                                Slot 1 is the key the fleet dials; higher slots are spares it rotates
                                onto when slot 1 caps.
                              </p>
                              <div className="flex flex-wrap items-center gap-1.5">
                                <Button
                                  type="button"
                                  variant="outline"
                                  size="xs"
                                  disabled={!form.value.trim() || form.busy !== null}
                                  data-testid={`provider-key-test-${provider.id}`}
                                  onClick={() => void testCandidate(provider)}
                                >
                                  {form.busy === "test" ? (
                                    <Loader2 aria-hidden className="animate-spin" />
                                  ) : (
                                    <Zap aria-hidden />
                                  )}
                                  Test with this key
                                </Button>
                                <Button
                                  type="submit"
                                  size="xs"
                                  disabled={!form.value.trim() || form.busy !== null}
                                  data-testid={`provider-key-save-${provider.id}`}
                                >
                                  {form.busy === "save" ? (
                                    <Loader2 aria-hidden className="animate-spin" />
                                  ) : null}
                                  Save
                                </Button>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="xs"
                                  data-testid={`provider-key-cancel-${provider.id}`}
                                  onClick={closeForm}
                                >
                                  Cancel
                                </Button>
                              </div>
                              <span
                                aria-live="polite"
                                data-testid={`provider-key-test-result-${provider.id}`}
                                className={
                                  form.verdict
                                    ? form.verdict.ok
                                      ? "text-xs text-success"
                                      : "text-xs text-destructive"
                                    : "text-xs text-muted-foreground"
                                }
                              >
                                {form.verdict
                                  ? form.verdict.ok
                                    ? `This key answered in ${form.verdict.latency_ms}ms on ${form.verdict.model}. It has not been saved yet.`
                                    : `${form.verdict.error_class ?? "failed"} — ${form.verdict.message ?? "the provider refused the call."}`
                                  : "Not tested yet. Testing dials the provider once with this key and stores nothing."}
                              </span>
                              {form.error ? (
                                <span
                                  className="text-xs text-destructive"
                                  data-testid={`provider-key-error-${provider.id}`}
                                >
                                  {form.error}
                                </span>
                              ) : null}
                            </form>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>

      <section
        data-testid="fleet-defaults"
        className="flex max-w-3xl flex-col gap-2.5 rounded-lg border border-border bg-card p-4"
      >
        <h3 className="text-sm font-medium text-foreground">Fleet default model</h3>
        <p className="text-xs text-muted-foreground">
          The model every agent inherits, with the chain it falls back to. Agents that pin a model in
          their own manifest keep it — this changes nothing for them.
        </p>
        <p className="text-xs text-muted-foreground">
          The bridge has no route that reports the model the fleet is running on right now, so
          nothing is pre-selected here: what you choose below is written to{" "}
          <span className="font-mono">_defaults.yaml</span> and reloaded.
        </p>

        {modelsError ? (
          <p className="text-xs text-destructive" data-testid="fleet-models-error">
            {modelsError}
          </p>
        ) : null}

        <div className="flex flex-wrap items-center gap-2">
          <label
            className="text-[11px] uppercase tracking-[0.08em] text-muted-foreground"
            htmlFor="fleet-default-model"
          >
            Primary
          </label>
          <NativeSelect
            id="fleet-default-model"
            data-testid="fleet-default-model"
            value={primary}
            disabled={models.length === 0}
            onChange={(event) => setPrimary(event.target.value)}
          >
            <option value="">Choose a model…</option>
            {modelGroups.map((group) => (
              <optgroup key={group.id} label={group.label}>
                {group.models.map((model) => (
                  <option key={model.id} value={model.id}>
                    {`${model.id} · ${formatContext(model.context_window)}`}
                  </option>
                ))}
              </optgroup>
            ))}
          </NativeSelect>
        </div>

        {fallbacks.map((value, index) => (
          <div key={index} className="flex flex-wrap items-center gap-2">
            <label
              className="text-[11px] uppercase tracking-[0.08em] text-muted-foreground"
              htmlFor={`fleet-fallback-${index}`}
            >
              Fallback {index + 1}
            </label>
            <NativeSelect
              id={`fleet-fallback-${index}`}
              data-testid={`fleet-fallback-${index}`}
              value={value}
              onChange={(event) =>
                setFallbacks((prev) =>
                  prev.map((item, position) => (position === index ? event.target.value : item))
                )
              }
            >
              <option value="">Choose a model…</option>
              {modelGroups.map((group) => (
                <optgroup key={group.id} label={group.label}>
                  {group.models.map((model) => (
                    <option key={model.id} value={model.id}>
                      {`${model.id} · ${formatContext(model.context_window)}`}
                    </option>
                  ))}
                </optgroup>
              ))}
            </NativeSelect>
            <Button
              variant="ghost"
              size="xs"
              data-testid={`fleet-fallback-remove-${index}`}
              onClick={() => setFallbacks((prev) => prev.filter((_, position) => position !== index))}
            >
              Remove
            </Button>
          </div>
        ))}

        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            variant="outline"
            size="xs"
            data-testid="fleet-fallback-add"
            disabled={models.length === 0}
            onClick={() => setFallbacks((prev) => [...prev, ""])}
          >
            <Plus aria-hidden />
            Add fallback
          </Button>
          <Button
            size="xs"
            disabled={!primary || savingDefaults}
            data-testid="fleet-default-save"
            onClick={() => void saveDefaults()}
          >
            {savingDefaults ? <Loader2 aria-hidden className="animate-spin" /> : null}
            Save default
          </Button>
        </div>

        <span
          aria-live="polite"
          data-testid="fleet-default-status"
          className={
            defaultsResult && !defaultsResult.applied
              ? "text-xs text-warning"
              : "text-xs text-muted-foreground"
          }
        >
          {defaultsResult
            ? defaultsResult.applied
              ? `${defaultsResult.model} is in use by the fleet.`
              : `${defaultsResult.model} was written to _defaults.yaml, but the engine did not confirm the reload — it may still be running the previous model.`
            : "No change saved from this screen yet."}
        </span>

        {defaultsError ? (
          <span className="text-xs text-destructive" data-testid="fleet-default-error">
            {defaultsError}
          </span>
        ) : null}
      </section>
    </div>
  );
}
