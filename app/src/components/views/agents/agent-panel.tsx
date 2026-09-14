"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, Plus, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import {
  DELIVERY_MODES,
  DEPARTMENTS,
  deriveAgentId,
  describeCron,
  fieldForIssue,
  nextRunText,
  timezoneChoices,
  type ManifestDetail,
  type ModelEntry,
  type ValidationIssue,
  type ValidationVerdict,
} from "@/lib/agents/manifests";
import { readBridgeReply } from "@/lib/bridge/read-reply";

/**
 * The agent builder panel — three fields, and everything else behind Advanced.
 *
 * Two decisions shape the whole file.
 *
 * **A PATCH carries only what changed.** `PatchRequest` treats an absent field
 * as "leave it alone" and a present one as a write, so posting the form back
 * whole would rewrite `delivery.to` with the value it already had, bump the
 * version, and append a changelog line for an edit nobody made. The panel keeps
 * the document it loaded and diffs against it.
 *
 * **A refusal is rendered where it can be fixed.** A 422 from this bridge is a
 * verdict — dotted schema paths with stable codes — and `fieldForPath` puts
 * each one under the input that produced it. The paths this form has no input
 * for are listed on their own rather than dropped: they are real faults, and
 * the operator has to be told which ones need the YAML.
 *
 * The YAML pane is read-only and edit-only, because there is no YAML write
 * route. Everything the drawer collects posts through Create/Patch.
 */

const BRIDGE = "/api/bridge";

const DEFAULT_CHANGE = "Edited via the Helm agent builder";

export interface PanelFields {
  id: string;
  name: string;
  job: string;
  instructions: string;
  department: string;
  model: string;
  fallbacks: string[];
  cron: string;
  timezone: string;
  enabled: boolean;
  deliveryMode: string;
  deliveryChannel: string;
  deliveryTo: string;
  tools: string[];
}

const EMPTY: PanelFields = {
  id: "",
  name: "",
  job: "",
  instructions: "",
  department: "",
  model: "",
  fallbacks: [],
  cron: "",
  timezone: "",
  enabled: true,
  deliveryMode: "",
  deliveryChannel: "",
  deliveryTo: "",
  tools: [],
};

/** The manifest document, flattened into the fields this form owns. */
function fieldsFromManifest(document: Record<string, unknown>): PanelFields {
  const block = (key: string): Record<string, unknown> => {
    const value = document[key];
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  };
  const text = (value: unknown): string => (typeof value === "string" ? value : "");
  const list = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  const schedule = block("schedule");
  const delivery = block("delivery");
  const model = block("model");
  return {
    id: text(document.id),
    name: text(document.name),
    job: text(document.description),
    instructions: "",
    department: text(document.department),
    model: text(model.primary),
    fallbacks: list(model.fallbacks),
    cron: text(schedule.cron),
    timezone: text(schedule.timezone),
    // Absent means enabled, which is what the engine's loader assumes.
    enabled: schedule.enabled === undefined ? true : Boolean(schedule.enabled),
    deliveryMode: text(delivery.mode),
    deliveryChannel: text(delivery.channel),
    deliveryTo: text(delivery.to),
    tools: list(document.tools_allowed),
  };
}

const sameList = (a: string[], b: string[]) =>
  a.length === b.length && a.every((value, index) => value === b[index]);

/**
 * The PATCH body for an edit: the bridge's field names, and only the fields
 * whose value is not the one that was loaded.
 */
export function changedFields(before: PanelFields, after: PanelFields): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (after.name !== before.name) body.name = after.name;
  if (after.job !== before.job) body.description = after.job;
  if (after.department !== before.department) body.department = after.department;
  if (after.model !== before.model) body.model = after.model;
  const fallbacks = after.fallbacks.filter(Boolean);
  if (!sameList(fallbacks, before.fallbacks)) body.fallbacks = fallbacks;
  if (after.cron !== before.cron) body.cron = after.cron;
  if (after.timezone !== before.timezone) body.timezone = after.timezone;
  if (after.enabled !== before.enabled) body.enabled = after.enabled;
  // A blank mode is the select's "leave it alone" option, not a value. The
  // schema's enum is ['announce', 'log', 'none'] and `_merge_owned` writes what
  // it is handed, so sending "" made a control labelled as a no-op fail the save.
  if (after.deliveryMode && after.deliveryMode !== before.deliveryMode) {
    body.delivery_mode = after.deliveryMode;
  }
  if (after.deliveryChannel !== before.deliveryChannel) body.delivery_channel = after.deliveryChannel;
  if (after.deliveryTo !== before.deliveryTo) body.delivery_to = after.deliveryTo;
  if (!sameList(after.tools, before.tools)) body.tools_allowed = after.tools;
  return body;
}

/** The CreateRequest body: the three fields, plus whatever Advanced was given. */
export function createBody(fields: PanelFields): Record<string, unknown> {
  const body: Record<string, unknown> = { name: fields.name.trim() };
  const typedId = fields.id.trim();
  if (typedId) body.id = typedId;
  if (fields.job.trim()) body.description = fields.job.trim();
  if (fields.instructions.trim()) body.instructions = fields.instructions.trim();
  if (fields.department.trim()) body.department = fields.department.trim();
  if (fields.model) body.model = fields.model;
  const fallbacks = fields.fallbacks.filter(Boolean);
  if (fallbacks.length) body.fallbacks = fallbacks;
  if (fields.cron.trim()) body.cron = fields.cron.trim();
  if (fields.timezone) body.timezone = fields.timezone;
  if (fields.deliveryMode) body.delivery_mode = fields.deliveryMode;
  if (fields.deliveryChannel.trim()) body.delivery_channel = fields.deliveryChannel.trim();
  if (fields.deliveryTo.trim()) body.delivery_to = fields.deliveryTo.trim();
  if (fields.tools.length) body.tools_allowed = fields.tools;
  return body;
}

interface Refusal {
  /** Errors keyed by the form field their path names. */
  byField: Record<string, ValidationIssue[]>;
  /** Errors whose path this form owns no input for. */
  other: ValidationIssue[];
  /** A refusal that was a sentence rather than a verdict. */
  message: string | null;
}

const NO_REFUSAL: Refusal = { byField: {}, other: [], message: null };

function refusalFromVerdict(errors: ValidationIssue[]): Refusal {
  const byField: Record<string, ValidationIssue[]> = {};
  const other: ValidationIssue[] = [];
  for (const issue of errors) {
    const field = fieldForIssue(issue);
    if (field) (byField[field] ??= []).push(issue);
    else other.push(issue);
  }
  return { byField, other, message: null };
}

/**
 * A failed write, as something the panel can render.
 *
 * `_refuse` answers 422 with the whole verdict as `detail`; `_refused` answers
 * 422 with a sentence; FastAPI's own body validation answers 422 with a list.
 * All three arrive on the same status code and only the first is field-keyed.
 */
async function refusalFrom(res: Response): Promise<Refusal> {
  let payload: unknown = null;
  try {
    payload = await res.json();
  } catch {
    return { ...NO_REFUSAL, message: `The bridge refused the request (HTTP ${res.status}).` };
  }
  const detail = (payload as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const verdict = detail as ValidationVerdict;
    if (Array.isArray(verdict.errors) && verdict.errors.length) {
      return refusalFromVerdict(verdict.errors);
    }
  }
  if (typeof detail === "string" && detail.trim()) {
    return { ...NO_REFUSAL, message: detail };
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item && typeof item === "object" ? (item as { msg?: string }).msg : null))
      .filter((message): message is string => Boolean(message));
    if (messages.length) return { ...NO_REFUSAL, message: messages.join("; ") };
  }
  return { ...NO_REFUSAL, message: `The bridge refused the request (HTTP ${res.status}).` };
}

export interface AgentPanelProps {
  /** `null` is a new agent; a string is the id being edited. */
  agentId: string | null;
  models: ModelEntry[];
  onClose: () => void;
  /** Called after any write the bridge accepted, so the list can re-read. */
  onSaved: () => void;
}

const LABEL = "text-[11px] uppercase tracking-[0.08em] text-muted-foreground";
const FIELD = "flex flex-col gap-1";
const TEXTAREA =
  "min-h-24 w-full rounded-md border border-input bg-card px-2.5 py-2 text-sm text-foreground " +
  "focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-1 " +
  "read-only:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50";

export function AgentPanel({ agentId, models, onClose, onSaved }: AgentPanelProps) {
  const editing = agentId !== null;

  const [fields, setFields] = useState<PanelFields>(EMPTY);
  const [loaded, setLoaded] = useState<PanelFields | null>(null);
  const [detail, setDetail] = useState<ManifestDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(editing);

  const [advanced, setAdvanced] = useState(false);
  const [tab, setTab] = useState<"form" | "yaml">("form");
  const [toolDraft, setToolDraft] = useState("");
  const [change, setChange] = useState(DEFAULT_CHANGE);

  const [refusal, setRefusal] = useState<Refusal>(NO_REFUSAL);
  const [busy, setBusy] = useState<"create" | "create-run" | "save" | "validate" | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<ValidationIssue[]>([]);
  const [reconcileNote, setReconcileNote] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<ValidationVerdict | null>(null);

  const load = useCallback(async () => {
    if (agentId === null) return;
    setLoading(true);
    try {
      const res = await fetch(`${BRIDGE}/api/agent-manifests/${encodeURIComponent(agentId)}`);
      if (!res.ok) {
        setLoadError(await readBridgeReply(res));
        return;
      }
      const body = (await res.json()) as ManifestDetail;
      setDetail(body);
      const next = body.manifest
        ? { ...fieldsFromManifest(body.manifest), instructions: body.instructions ?? "" }
        : { ...EMPTY, id: agentId, instructions: body.instructions ?? "" };
      setFields(next);
      // The baseline a PATCH diffs against. Instructions are not patchable, so
      // they are deliberately not part of it.
      setLoaded(next);
      setLoadError(null);
    } catch {
      setLoadError("The dashboard could not reach the bridge to read that agent.");
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    void load();
  }, [load]);

  const modelGroups = useMemo(() => {
    const groups = new Map<string, ModelEntry[]>();
    for (const model of models) {
      const existing = groups.get(model.provider);
      if (existing) existing.push(model);
      else groups.set(model.provider, [model]);
    }
    return [...groups.entries()].map(([id, entries]) => ({
      id,
      models: entries.slice().sort((a, b) => a.id.localeCompare(b.id)),
    }));
  }, [models]);

  const catalogIds = useMemo(() => new Set(models.map((model) => model.id)), [models]);
  const zones = useMemo(() => timezoneChoices(), []);

  const derived = deriveAgentId(fields.name);
  const typedId = fields.id.trim();
  const effectiveId = typedId || derived;
  const patchBody = loaded ? changedFields(loaded, fields) : {};
  const dirty = Object.keys(patchBody).length > 0;

  function set<K extends keyof PanelFields>(key: K, value: PanelFields[K]) {
    setFields((prev) => ({ ...prev, [key]: value }));
    // A refusal describes the values that were sent, not the ones being typed.
    setRefusal(NO_REFUSAL);
  }

  function errorsFor(field: string): ValidationIssue[] {
    return refusal.byField[field] ?? [];
  }

  function FieldErrors({ field }: { field: string }) {
    const issues = errorsFor(field);
    if (!issues.length) return null;
    return (
      <span className="text-xs text-destructive" data-testid={`agent-error-${field}`}>
        {issues.map((issue) => issue.message).join(" ")}
      </span>
    );
  }

  /** The fields the panel shows with Advanced closed. */
  const ALWAYS_VISIBLE = new Set(["name", "job", "instructions"]);

  /**
   * Record a refusal, and open Advanced if any of it landed in there.
   *
   * A 422 about `schedule.cron` rendered beside a cron input inside a closed
   * disclosure is a refusal the operator is never shown: the form simply
   * declines to save, with no visible reason anywhere on screen.
   */
  function applyRefusal(next: Refusal) {
    setRefusal(next);
    const hidden = Object.keys(next.byField).some((field) => !ALWAYS_VISIBLE.has(field));
    if (hidden) setAdvanced(true);
  }

  function clearOutcome() {
    setResult(null);
    setWarnings([]);
    setReconcileNote(null);
    setRefusal(NO_REFUSAL);
  }

  function absorb(body: Record<string, unknown>, sentence: string) {
    setResult(sentence);
    const answered = Array.isArray(body.warnings) ? (body.warnings as ValidationIssue[]) : [];
    const pre = Array.isArray(body.pre_existing) ? (body.pre_existing as ValidationIssue[]) : [];
    setWarnings([...answered, ...pre]);
    const reconcile = body.reconcile as { applied?: boolean; error?: string } | undefined;
    setReconcileNote(
      reconcile && reconcile.applied === false
        ? `The manifest was written, but the engine did not pick up the schedule change (${
            reconcile.error ?? "the engine did not reconcile"
          }). It will be reconciled on the next watchdog pass.`
        : null
    );
  }

  /** POST the create, and hand back the id the SERVER chose. */
  async function create(): Promise<string | null> {
    const res = await fetch(`${BRIDGE}/api/agent-manifests`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(createBody(fields)),
    });
    if (!res.ok) {
      if (res.status === 409) {
        const conflict = await refusalFrom(res);
        applyRefusal({
          byField: {
            id: [
              {
                path: "id",
                code: "conflict",
                message: `${
                  conflict.message ?? "an agent with that id already exists"
                } — choose a different id below.`,
              },
            ],
          },
          other: [],
          message: null,
        });
        return null;
      }
      applyRefusal(await refusalFrom(res));
      return null;
    }
    const body = (await res.json()) as Record<string, unknown>;
    const id = typeof body.id === "string" ? body.id : effectiveId;
    absorb(body, `Created ${id}.`);
    onSaved();
    return id;
  }

  async function onCreate(alsoRun: boolean) {
    setBusy(alsoRun ? "create-run" : "create");
    clearOutcome();
    try {
      const id = await create();
      if (!id || !alsoRun) return;
      const res = await fetch(`${BRIDGE}/api/agent-manifests/${encodeURIComponent(id)}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) {
        setResult(`Created ${id}, but the run was refused: ${await readBridgeReply(res)}`);
        return;
      }
      setResult(`Created ${id} and triggered it once.`);
    } catch {
      applyRefusal({
        ...NO_REFUSAL,
        message: "The dashboard could not reach the bridge to create that agent.",
      });
    } finally {
      setBusy(null);
    }
  }

  async function onSave() {
    if (agentId === null || !dirty) return;
    setBusy("save");
    clearOutcome();
    try {
      const res = await fetch(`${BRIDGE}/api/agent-manifests/${encodeURIComponent(agentId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...patchBody, change: change.trim() || DEFAULT_CHANGE }),
      });
      if (!res.ok) {
        applyRefusal(await refusalFrom(res));
        return;
      }
      const body = (await res.json()) as Record<string, unknown>;
      const manifest = (body.manifest ?? {}) as Record<string, unknown>;
      const version = typeof manifest.version === "string" ? manifest.version : "";
      absorb(body, version ? `Saved ${agentId} as version ${version}.` : `Saved ${agentId}.`);
      setLoaded(fields);
      onSaved();
      void load();
    } catch {
      applyRefusal({
        ...NO_REFUSAL,
        message: "The dashboard could not reach the bridge to save that agent.",
      });
    } finally {
      setBusy(null);
    }
  }

  async function onValidate() {
    if (!detail) return;
    setBusy("validate");
    setVerdict(null);
    try {
      const res = await fetch(`${BRIDGE}/api/agent-manifests/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // The yaml on disk, not a document this panel re-serialised: the point
        // of the pane is to judge the file the engine actually loads.
        body: JSON.stringify({ yaml: detail.yaml }),
      });
      if (!res.ok) {
        setVerdict({
          ok: false,
          errors: [{ path: "", code: "unreachable", message: await readBridgeReply(res) }],
          warnings: [],
        });
        return;
      }
      setVerdict((await res.json()) as ValidationVerdict);
    } catch {
      setVerdict({
        ok: false,
        errors: [
          {
            path: "",
            code: "unreachable",
            message: "The dashboard could not reach the bridge to validate that manifest.",
          },
        ],
        warnings: [],
      });
    } finally {
      setBusy(null);
    }
  }

  function addTool() {
    const name = toolDraft.trim();
    if (!name || fields.tools.includes(name)) {
      setToolDraft("");
      return;
    }
    set("tools", [...fields.tools, name]);
    setToolDraft("");
  }

  const cronPreview = describeCron(fields.cron);
  // No zone substituted: `nextRunText` answers null for an unset zone, and the
  // note below says whose clock the schedule is on instead of inventing one.
  const cronNext = nextRunText(fields.cron, fields.timezone);
  const cronSchedulable = cronPreview !== "not a valid schedule" && fields.cron.trim() !== "";
  const canCreate = Boolean(fields.name.trim()) && Boolean(effectiveId) && busy === null;

  function modelOptions(selected: string) {
    const unlisted = selected && !catalogIds.has(selected) ? selected : null;
    return (
      <>
        <option value="">Inherit the fleet default</option>
        {unlisted ? <option value={unlisted}>{`${unlisted} (not in the catalog)`}</option> : null}
        {modelGroups.map((group) => (
          <optgroup key={group.id} label={group.id}>
            {group.models.map((model) => (
              <option key={model.id} value={model.id}>
                {model.id}
              </option>
            ))}
          </optgroup>
        ))}
      </>
    );
  }

  return (
    <section
      data-testid="agent-panel"
      aria-label={editing ? `Edit ${agentId}` : "New agent"}
      // A phone has no room to put a fifteen-input drawer beside a table: below
      // the tablet breakpoint this is a sheet over the page, and from `md` up it
      // is the inline card it always was.
      className={
        "fixed inset-0 z-50 flex flex-col gap-3 overflow-y-auto border-border bg-card p-4 " +
        "md:static md:z-auto md:rounded-lg md:border"
      }
    >
      <header className="sticky top-0 z-10 -mx-4 -mt-4 flex flex-wrap items-center justify-between gap-2 border-b border-border bg-card px-4 py-2 md:static md:mx-0 md:mt-0 md:border-b-0 md:px-0 md:py-0">
        <h3 className="text-sm font-medium text-foreground">
          {editing ? `Edit ${agentId}` : "New agent"}
        </h3>
        <Button variant="ghost" size="xs" data-testid="agent-cancel" onClick={onClose}>
          Close
        </Button>
      </header>

      {loading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground" data-testid="agent-panel-loading">
          <Loader2 aria-hidden className="size-4 animate-spin" />
          Reading the manifest from the bridge…
        </p>
      ) : null}

      {loadError ? (
        <p className="text-xs text-destructive" data-testid="agent-panel-load-error">
          {loadError}
        </p>
      ) : null}

      <div className={FIELD}>
        <label className={LABEL} htmlFor="agent-field-name">
          Name
        </label>
        <Input
          id="agent-field-name"
          data-testid="agent-field-name"
          value={fields.name}
          placeholder="Vendor follow up"
          onChange={(event) => set("name", event.target.value)}
        />
        <FieldErrors field="name" />
        {editing ? (
          <span className="font-mono text-[11px] text-muted-foreground" data-testid="agent-derived-id">
            {`id ${agentId} · fixed after creation`}
          </span>
        ) : (
          <span className="text-[11px] text-muted-foreground" data-testid="agent-derived-id">
            {fields.name.trim() === "" ? (
              "The id is derived from the name."
            ) : effectiveId ? (
              <>
                <span className="font-mono">{`id ${effectiveId}`}</span>
                {" · fixed after creation"}
              </>
            ) : (
              "No id can be derived from that name — it needs at least one letter or digit."
            )}
          </span>
        )}
      </div>

      <div className={FIELD}>
        <label className={LABEL} htmlFor="agent-field-job">
          Job
        </label>
        <Input
          id="agent-field-job"
          data-testid="agent-field-job"
          value={fields.job}
          placeholder="One line: what this agent is for."
          onChange={(event) => set("job", event.target.value)}
        />
        <FieldErrors field="job" />
      </div>

      <div className={FIELD}>
        <label className={LABEL} htmlFor="agent-field-instructions">
          Instructions
        </label>
        <textarea
          id="agent-field-instructions"
          data-testid="agent-field-instructions"
          className={TEXTAREA}
          value={fields.instructions}
          readOnly={editing}
          placeholder="What the agent should do, in your own words."
          onChange={(event) => set("instructions", event.target.value)}
        />
        {editing ? (
          <span className="text-[11px] text-muted-foreground" data-testid="agent-instructions-note">
            {detail?.manifest && typeof detail.manifest.instruction_file === "string"
              ? `Read-only here. This is ${detail.manifest.instruction_file} in the workspace — edit it there.`
              : "Read-only here. The instruction file is not editable through this API."}
          </span>
        ) : null}
        <FieldErrors field="instructions" />
      </div>

      <div>
        <Button
          variant="ghost"
          size="xs"
          data-testid="agent-advanced-toggle"
          aria-expanded={advanced}
          onClick={() => setAdvanced((open) => !open)}
        >
          {advanced ? <ChevronDown aria-hidden /> : <ChevronRight aria-hidden />}
          Advanced
        </Button>
      </div>

      {advanced ? (
        <div data-testid="agent-advanced" className="flex flex-col gap-3 rounded-md border border-border bg-background p-3">
          {editing ? (
            <div className="flex gap-1.5">
              <Button
                variant={tab === "form" ? "secondary" : "ghost"}
                size="xs"
                data-testid="agent-tab-form"
                onClick={() => setTab("form")}
              >
                Settings
              </Button>
              <Button
                variant={tab === "yaml" ? "secondary" : "ghost"}
                size="xs"
                data-testid="agent-tab-yaml"
                onClick={() => setTab("yaml")}
              >
                YAML
              </Button>
            </div>
          ) : null}

          {tab === "yaml" && editing ? (
            <div className="flex flex-col gap-2">
              <pre
                data-testid="agent-yaml"
                className="max-h-80 overflow-auto rounded-md border border-border bg-card p-2.5 font-mono text-[11px] text-foreground"
              >
                {detail?.yaml ?? ""}
              </pre>
              <p className="text-[11px] text-muted-foreground">
                Read-only. There is no YAML write route — every change above is posted as a field,
                so a hand-written key this form has never heard of survives an edit.
              </p>
              <div>
                <Button
                  variant="outline"
                  size="xs"
                  data-testid="agent-validate"
                  disabled={busy !== null}
                  onClick={() => void onValidate()}
                >
                  {busy === "validate" ? <Loader2 aria-hidden className="animate-spin" /> : null}
                  Validate
                </Button>
              </div>
              {verdict ? (
                <div
                  aria-live="polite"
                  data-testid="agent-validate-result"
                  className={verdict.ok ? "text-xs text-success" : "text-xs text-destructive"}
                >
                  {verdict.ok ? (
                    "The engine accepts this manifest."
                  ) : (
                    <ul className="flex flex-col gap-0.5">
                      {verdict.errors.map((issue, index) => (
                        <li key={index}>
                          <span className="font-mono">{issue.path || "(manifest)"}</span>
                          {` — ${issue.message}`}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ) : null}
            </div>
          ) : (
            <>
              {!editing ? (
                <div className={FIELD}>
                  <label className={LABEL} htmlFor="agent-field-id">
                    Id
                  </label>
                  <Input
                    id="agent-field-id"
                    data-testid="agent-field-id"
                    value={fields.id}
                    placeholder={derived || "derived from the name"}
                    onChange={(event) => set("id", event.target.value)}
                  />
                  <span className="text-[11px] text-muted-foreground">
                    Leave this empty to take the id derived from the name. It cannot be changed once
                    the agent exists.
                  </span>
                  <FieldErrors field="id" />
                </div>
              ) : null}

              <div className={FIELD}>
                <label className={LABEL} htmlFor="agent-field-department">
                  Department
                </label>
                <NativeSelect
                  id="agent-field-department"
                  data-testid="agent-field-department"
                  value={fields.department}
                  onChange={(event) => set("department", event.target.value)}
                >
                  {/* A closed enum in the manifest schema. As free text every
                      natural answer — ops, sales, finance — was a 422. */}
                  <option value="">Not set</option>
                  {DEPARTMENTS.map((department) => (
                    <option key={department} value={department}>
                      {department}
                    </option>
                  ))}
                </NativeSelect>
                <FieldErrors field="department" />
              </div>

              <div className={FIELD}>
                <label className={LABEL} htmlFor="agent-field-model">
                  Model
                </label>
                <NativeSelect
                  id="agent-field-model"
                  data-testid="agent-field-model"
                  value={fields.model}
                  onChange={(event) => set("model", event.target.value)}
                >
                  {modelOptions(fields.model)}
                </NativeSelect>
                <span className="text-[11px] text-muted-foreground" data-testid="agent-model-hint">
                  Left empty, this agent runs on the fleet default model set in Settings › Providers.
                </span>
                <FieldErrors field="model" />
              </div>

              <div className={FIELD}>
                <span className={LABEL}>Fallbacks</span>
                {fields.fallbacks.map((value, index) => (
                  <div key={index} className="flex flex-wrap items-center gap-2">
                    <NativeSelect
                      aria-label={`Fallback ${index + 1}`}
                      data-testid={`agent-fallback-${index}`}
                      value={value}
                      onChange={(event) =>
                        set(
                          "fallbacks",
                          fields.fallbacks.map((item, position) =>
                            position === index ? event.target.value : item
                          )
                        )
                      }
                    >
                      {modelOptions(value)}
                    </NativeSelect>
                    <Button
                      variant="ghost"
                      size="xs"
                      data-testid={`agent-fallback-remove-${index}`}
                      onClick={() =>
                        set(
                          "fallbacks",
                          fields.fallbacks.filter((_, position) => position !== index)
                        )
                      }
                    >
                      Remove
                    </Button>
                  </div>
                ))}
                <div>
                  <Button
                    variant="outline"
                    size="xs"
                    data-testid="agent-fallback-add"
                    onClick={() => set("fallbacks", [...fields.fallbacks, ""])}
                  >
                    <Plus aria-hidden />
                    Add fallback
                  </Button>
                </div>
                <FieldErrors field="fallbacks" />
              </div>

              <div className={FIELD}>
                <label className={LABEL} htmlFor="agent-field-cron">
                  Schedule
                </label>
                <Input
                  id="agent-field-cron"
                  data-testid="agent-field-cron"
                  value={fields.cron}
                  placeholder="0 9 * * 1-5"
                  spellCheck={false}
                  onChange={(event) => set("cron", event.target.value)}
                />
                <span
                  aria-live="polite"
                  data-testid="agent-cron-preview"
                  className={
                    cronPreview === "not a valid schedule"
                      ? "text-xs text-destructive"
                      : "text-xs text-muted-foreground"
                  }
                >
                  {cronPreview}
                </span>
                {cronNext ? (
                  <span className="text-[11px] text-muted-foreground" data-testid="agent-cron-next">
                    {`Next run ${cronNext}.`}
                  </span>
                ) : null}
                {!cronNext && cronSchedulable ? (
                  <span
                    className="text-[11px] text-muted-foreground"
                    data-testid="agent-cron-zone-note"
                  >
                    This runs on the engine&apos;s own zone, which nothing here can read — choose a
                    timezone below to see the next firing time.
                  </span>
                ) : null}
                <FieldErrors field="cron" />
              </div>

              <div className={FIELD}>
                <label className={LABEL} htmlFor="agent-field-timezone">
                  Timezone
                </label>
                <NativeSelect
                  id="agent-field-timezone"
                  data-testid="agent-field-timezone"
                  value={fields.timezone}
                  onChange={(event) => set("timezone", event.target.value)}
                >
                  <option value="">Use the engine&apos;s own zone</option>
                  {zones.map((zone) => (
                    <option key={zone} value={zone}>
                      {zone}
                    </option>
                  ))}
                </NativeSelect>
                <FieldErrors field="timezone" />
              </div>

              {editing ? (
                <label className="flex items-center gap-2 text-sm text-foreground">
                  <input
                    type="checkbox"
                    data-testid="agent-field-enabled"
                    checked={fields.enabled}
                    onChange={(event) => set("enabled", event.target.checked)}
                  />
                  On its schedule
                </label>
              ) : (
                <span className="text-[11px] text-muted-foreground">
                  A new agent starts on its schedule. Disable it from the list once it exists.
                </span>
              )}
              <FieldErrors field="enabled" />

              <div className={FIELD}>
                <label className={LABEL} htmlFor="agent-field-deliveryMode">
                  Delivery
                </label>
                <NativeSelect
                  id="agent-field-deliveryMode"
                  data-testid="agent-field-deliveryMode"
                  value={fields.deliveryMode}
                  onChange={(event) => set("deliveryMode", event.target.value)}
                >
                  <option value="">
                    {editing ? "Leave as the manifest has it" : "Not set"}
                  </option>
                  {DELIVERY_MODES.map((mode) => (
                    <option key={mode} value={mode}>
                      {mode}
                    </option>
                  ))}
                </NativeSelect>
                <FieldErrors field="deliveryMode" />
                <Input
                  aria-label="Delivery channel"
                  data-testid="agent-field-deliveryChannel"
                  value={fields.deliveryChannel}
                  placeholder="channel"
                  onChange={(event) => set("deliveryChannel", event.target.value)}
                />
                <FieldErrors field="deliveryChannel" />
                <Input
                  aria-label="Delivery recipient"
                  data-testid="agent-field-deliveryTo"
                  value={fields.deliveryTo}
                  placeholder="to"
                  onChange={(event) => set("deliveryTo", event.target.value)}
                />
                <FieldErrors field="deliveryTo" />
              </div>

              <div className={FIELD}>
                <span className={LABEL}>Tools allowed</span>
                <div className="flex flex-wrap items-center gap-1.5">
                  {fields.tools.map((tool, index) => (
                    <Badge
                      key={tool}
                      variant="outline"
                      data-testid={`agent-tool-${index}`}
                      className="gap-1 font-mono"
                    >
                      {tool}
                      <button
                        type="button"
                        aria-label={`Remove ${tool}`}
                        data-testid={`agent-tool-remove-${index}`}
                        onClick={() =>
                          set(
                            "tools",
                            fields.tools.filter((_, position) => position !== index)
                          )
                        }
                      >
                        <X aria-hidden className="size-3" />
                      </button>
                    </Badge>
                  ))}
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  <Input
                    aria-label="Tool name"
                    data-testid="agent-tool-input"
                    value={toolDraft}
                    placeholder="crm_search"
                    className="max-w-56"
                    onChange={(event) => setToolDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        addTool();
                      }
                    }}
                  />
                  <Button variant="outline" size="xs" data-testid="agent-tool-add" onClick={addTool}>
                    <Plus aria-hidden />
                    Add tool
                  </Button>
                </div>
                <span className="text-[11px] text-muted-foreground" data-testid="agent-tools-hint">
                  Free text: the engine owns the tool list. A name it does not know comes back as a
                  refusal under this field, naming the tool. An empty list leaves the manifest as it
                  is.
                </span>
                <FieldErrors field="tools" />
              </div>
            </>
          )}
        </div>
      ) : null}

      {editing ? (
        <div className={FIELD}>
          <label className={LABEL} htmlFor="agent-field-change">
            Change note
          </label>
          <Input
            id="agent-field-change"
            data-testid="agent-field-change"
            value={change}
            onChange={(event) => setChange(event.target.value)}
          />
          <span className="text-[11px] text-muted-foreground">
            Appended to the manifest changelog with the new version.
          </span>
        </div>
      ) : null}

      {refusal.other.length ? (
        <div
          data-testid="agent-errors-other"
          className="flex flex-col gap-1 rounded-md border border-destructive/30 bg-destructive/5 p-2.5"
        >
          <p className="text-xs font-medium text-destructive">
            The bridge refused this manifest for reasons no field here covers:
          </p>
          <ul className="flex flex-col gap-0.5 text-xs text-muted-foreground">
            {refusal.other.map((issue, index) => (
              <li key={index}>
                <span className="font-mono">{issue.path || "(manifest)"}</span>
                {` — ${issue.message}`}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {refusal.message ? (
        <p className="text-xs text-destructive" data-testid="agent-panel-error">
          {refusal.message}
        </p>
      ) : null}

      <div className="flex flex-wrap items-center gap-1.5">
        {editing ? (
          <Button
            size="xs"
            data-testid="agent-save"
            disabled={!dirty || busy !== null}
            onClick={() => void onSave()}
          >
            {busy === "save" ? <Loader2 aria-hidden className="animate-spin" /> : null}
            Save
          </Button>
        ) : (
          <>
            <Button
              size="xs"
              data-testid="agent-create"
              disabled={!canCreate}
              onClick={() => void onCreate(false)}
            >
              {busy === "create" ? <Loader2 aria-hidden className="animate-spin" /> : null}
              Create
            </Button>
            <Button
              variant="outline"
              size="xs"
              data-testid="agent-create-run"
              disabled={!canCreate}
              onClick={() => void onCreate(true)}
            >
              {busy === "create-run" ? <Loader2 aria-hidden className="animate-spin" /> : null}
              Create &amp; run once
            </Button>
          </>
        )}
      </div>

      {result ? (
        <p aria-live="polite" className="text-xs text-success" data-testid="agent-panel-result">
          {result}
        </p>
      ) : null}

      {reconcileNote ? (
        <p className="text-xs text-warning" data-testid="agent-panel-reconcile">
          {reconcileNote}
        </p>
      ) : null}

      {warnings.length ? (
        <ul className="flex flex-col gap-0.5 text-xs text-warning" data-testid="agent-panel-warnings">
          {warnings.map((issue, index) => (
            <li key={index}>
              <span className="font-mono">{issue.path || "(manifest)"}</span>
              {` — ${issue.message}`}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
