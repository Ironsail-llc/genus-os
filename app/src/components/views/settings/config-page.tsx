"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, Check, Flag, KeyRound, Loader2, RefreshCw, Search } from "lucide-react";

import { PageHeader } from "@/components/business/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { BRIDGE_UNREACHABLE } from "@/lib/bridge/use-bridge-poll";
import {
  asSecretStatus,
  fromDraft,
  matchesQuery,
  normalizeSchema,
  normalizeValues,
  toDraft,
  type SettingField,
  type SettingGroup,
  type SettingSource,
  type SettingValue,
} from "@/lib/settings/schema";
import {
  dismissRestartNotice,
  noteRestartNeeded,
  useRestartNotice,
} from "@/lib/settings/restart-banner";

/**
 * Settings › Config — `genus config` for an operator who is not on the box.
 *
 * The form is DESCRIBED by the bridge, not by this file: `GET
 * /api/settings/schema` says which fields exist, what type each is, which
 * layer may supply it, whether it holds a credential, and what a change waits
 * on. Nothing here enumerates a setting. That is the whole point — a
 * hand-written form is a second declaration of the settings model, and the two
 * drift the first time a field is added.
 *
 * Four rules this page is built around, each of which the bridge enforces too,
 * because a form that disagreed with the server would only produce refusals
 * nobody can act on:
 *
 * **A secret gets no input.** The route answers `{configured, fingerprint}`
 * and never a credential, and `genus vault set` (or the Secrets page) is where
 * one is written. A box the operator could type into would be a box that
 * writes a credential into a plain file that gets copied into bug reports.
 *
 * **A field the environment supplies is read-only, with the reason.** Writing
 * config.yaml under a variable that overrides it reports applied and changes
 * nothing — the exact failure the settings package exists to end.
 *
 * **A save posts only what changed.** `PATCH /api/settings` is all-or-nothing,
 * so a batch carrying every untouched field would let one unrelated bad value
 * block an edit the operator actually made.
 *
 * **Nothing is re-read under the operator's cursor.** The schema and the
 * values are fetched once per mount and never polled: the schema is ~164 KB
 * and cannot change without a restart, and a poll over the values would
 * overwrite unsaved edits. Refresh is a button, because a re-read is a thing
 * the operator chooses, having seen what they would lose.
 */

const BRIDGE = "/api/bridge";

/** The layer names, as an operator would say them rather than as the API spells them. */
const SOURCE_LABEL: Record<SettingSource, string> = {
  default: "default",
  config: "config.yaml",
  env: "environment",
  db: "flag store",
  unknown: "unknown",
};

const SOURCE_CLASS: Record<SettingSource, string> = {
  default: "border-border bg-muted text-muted-foreground",
  config: "border-info/30 bg-info/10 text-info",
  env: "border-warning/30 bg-warning/10 text-warning",
  db: "border-primary/25 bg-primary/10 text-foreground",
  unknown: "border-border bg-muted text-muted-foreground",
};

function unitList(units: string[]): string {
  return units.length ? units.join(", ") : "the services that read it";
}

/**
 * The types this build knows how to put a control behind.
 *
 * The settings model declares only these four today. A field arriving as
 * something else (a `list`, when one is added) is rendered read-only and says
 * so: a text box that posts `"a,b"` as a string is an editable control whose
 * save is a guaranteed 422, which is worse than an honest refusal.
 */
const EDITABLE_TYPES = new Set(["str", "int", "float", "bool"]);

/**
 * What to say when the route marked a field not editable and told us nothing
 * else — an older bridge, predating `values[].reason`.
 *
 * It deliberately names NO variable. The route resolves the variable actually
 * in use across the field's deprecated aliases, so on a mid-migration box the
 * canonical name is not the one that is set; a guess here would send the
 * operator to clear something that does not exist, and leave the field
 * overridden after the restart they did for it.
 */
function unexplainedRefusal(field: SettingField): string {
  return (
    `This instance will not let ${field.env} be changed from here, and did not say why. ` +
    `Run \`genus config explain ${field.env}\` on the box — it names the layer supplying the ` +
    `value and the variable in use.`
  );
}

/** A fingerprint is `sha256:` + eight characters. Anything longer is the bridge being wrong. */
const FINGERPRINT_MAX = 24;

interface FieldState {
  field: SettingField;
  current: SettingValue | null;
  draft: string;
  dirty: boolean;
}

export interface ConfigPageProps {
  /** The Settings container unmounts an inactive page; this is the load gate. */
  visible?: boolean;
  /** Take the operator to Settings › Flags, where a governed flag's verdict lives. */
  onOpenFlags?: () => void;
}

export function ConfigPage({ visible = true, onOpenFlags }: ConfigPageProps) {
  const [groups, setGroups] = useState<SettingGroup[] | null>(null);
  const [values, setValues] = useState<Record<string, SettingValue>>({});
  const [ignoredUnits, setIgnoredUnits] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<Record<string, true>>({});
  const [sectionErrors, setSectionErrors] = useState<Record<string, string>>({});
  const [savingGroup, setSavingGroup] = useState<string | null>(null);

  const notice = useRestartNotice();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [schemaRes, valuesRes] = await Promise.all([
        fetch(`${BRIDGE}/api/settings/schema`),
        fetch(`${BRIDGE}/api/settings`),
      ]);
      const bad = !schemaRes.ok ? schemaRes : !valuesRes.ok ? valuesRes : null;
      if (bad) {
        setError(await readBridgeReply(bad));
        return;
      }
      setGroups(normalizeSchema(await schemaRes.json()));
      const read = normalizeValues(await valuesRes.json());
      setValues(read.values);
      setIgnoredUnits(read.pendingRestart);
      setError(null);
    } catch {
      setError(BRIDGE_UNREACHABLE);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    void load();
  }, [visible, load]);

  const stateFor = useCallback(
    (field: SettingField): FieldState => {
      const current = values[field.name] ?? null;
      const saved = toDraft(field, current?.value);
      const draft = drafts[field.name] ?? saved;
      return { field, current, draft, dirty: draft !== saved };
    },
    [drafts, values]
  );

  /** What the operator may actually change here. Secrets and env-supplied fields are not it. */
  const writable = (state: FieldState): boolean =>
    !state.field.secret && (state.current?.editable ?? false);

  const setDraft = (name: string, next: string) => {
    setDrafts((prev) => ({ ...prev, [name]: next }));
    // An edited field is no longer the thing that was saved, and no longer the
    // thing that was refused: both marks belong to a value that is now gone.
    setSaved((prev) => {
      if (!(name in prev)) return prev;
      const next = { ...prev };
      delete next[name];
      return next;
    });
    setFieldErrors((prev) => {
      if (!(name in prev)) return prev;
      const next = { ...prev };
      delete next[name];
      return next;
    });
  };

  /**
   * The sections on screen, and how many of each one's unsaved changes the
   * filter is hiding.
   *
   * A group holding a dirty field is never filtered away. A draft that
   * disappears is worse than one that is refused: it survives in state, there
   * is no Save and no discard while it is hidden, and it is silently included
   * in the next save of that group once the filter is cleared.
   */
  const visibleGroups = useMemo(() => {
    if (!groups) return [];
    const needle = query.trim();
    // `group` is always the WHOLE group: a save posts a section's dirty fields,
    // and a save computed from the filtered list would quietly drop the ones
    // the filter is hiding. `shown` is what is drawn.
    if (!needle) return groups.map((group) => ({ group, shown: group.fields, hidden: 0 }));
    const out: Array<{ group: SettingGroup; shown: SettingField[]; hidden: number }> = [];
    for (const group of groups) {
      const shown: SettingField[] = [];
      let hidden = 0;
      for (const field of group.fields) {
        if (matchesQuery(field, needle)) shown.push(field);
        else if (stateFor(field).dirty) hidden += 1;
      }
      if (shown.length === 0 && hidden === 0) continue;
      out.push({ group, shown, hidden });
    }
    return out;
  }, [groups, query, stateFor]);

  const searching = query.trim().length > 0;

  async function saveGroup(group: SettingGroup) {
    const dirty = group.fields.map(stateFor).filter((s) => s.dirty && writable(s));
    if (dirty.length === 0) return;

    setSavingGroup(group.id);
    setSectionErrors((prev) => ({ ...prev, [group.id]: "" }));
    try {
      const changes: Record<string, unknown> = {};
      for (const state of dirty) changes[state.field.name] = fromDraft(state.field, state.draft);

      const res = await fetch(`${BRIDGE}/api/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ changes }),
      });
      const body = (await res.json().catch(() => null)) as {
        applied?: string[];
        pending_restart?: string[];
        errors?: Array<{ name?: string; message?: string }>;
      } | null;

      // `applied` FIRST, always, and whatever the status code says.
      //
      // The route documents a PARTIAL write: a failure during application
      // cannot be rolled back across a file and a table, so it answers 500
      // with `applied` naming exactly what landed. Reading `errors` first and
      // returning — which this did — reported such a save as a total failure:
      // the field that WAS written to config.yaml kept showing its old value,
      // and the units it needs never reached the restart banner. An operator
      // reads "the save failed", walks away, and never restarts a service a
      // change already in the file is waiting on.
      const applied = new Set(Array.isArray(body?.applied) ? body.applied : []);
      const landedStates = dirty.filter((s) => applied.has(s.field.name));

      if (landedStates.length > 0) {
        // The new values are taken from what was SENT rather than re-read: a
        // re-read would also replace every other section's unsaved edits,
        // which is a worse surprise than a source pill that is one layer stale
        // until Refresh. The route writes a governed flag to the flag store
        // and everything else to config.yaml, so the layer is knowable.
        setValues((prev) => {
          const next = { ...prev };
          for (const state of landedStates) {
            next[state.field.name] = {
              value: fromDraft(state.field, state.draft),
              source: state.field.governed ? "db" : "config",
              editable: true,
              reason: null,
            };
          }
          return next;
        });
        setDrafts((prev) => {
          const next = { ...prev };
          for (const state of landedStates) delete next[state.field.name];
          return next;
        });
        setSaved((prev) => {
          const next = { ...prev };
          for (const state of landedStates) next[state.field.name] = true;
          return next;
        });

        noteRestartNeeded(
          Array.isArray(body?.pending_restart) ? body.pending_restart : [],
          landedStates.map((s) => s.field.name)
        );
      }

      const errors = Array.isArray(body?.errors) ? body.errors : [];
      if (errors.length > 0) {
        // Every error lands on its own field. One that names something this
        // section is not rendering (an unknown name, a field filtered out of
        // view, or the request-level keys `changes`/`note`) still has to be
        // readable, or the save looks like it did nothing at all.
        const landed: Record<string, string> = {};
        const orphans: string[] = [];
        const rendered = new Set(group.fields.map((f) => f.name));
        for (const entry of errors) {
          const message = typeof entry?.message === "string" ? entry.message : "";
          const name = typeof entry?.name === "string" ? entry.name : "";
          if (name && rendered.has(name)) landed[name] = message || `${name} was refused.`;
          else orphans.push(message || `${name || "a setting"} was refused.`);
        }
        setFieldErrors((prev) => ({ ...prev, ...landed }));

        const partial = landedStates.length
          ? `Part of this section was written before the instance failed: ${landedStates
              .map((s) => s.field.name)
              .join(", ")} landed and the rest did not. `
          : "";
        if (partial || orphans.length) {
          setSectionErrors((prev) => ({ ...prev, [group.id]: `${partial}${orphans.join(" ")}`.trim() }));
        }
        return;
      }

      if (!res.ok) {
        setSectionErrors((prev) => ({
          ...prev,
          [group.id]: `The bridge refused the save (HTTP ${res.status}).`,
        }));
      }
    } catch {
      setSectionErrors((prev) => ({ ...prev, [group.id]: BRIDGE_UNREACHABLE }));
    } finally {
      setSavingGroup(null);
    }
  }

  if (!visible) return null;

  return (
    <div className="flex min-w-0 flex-col gap-4 p-4" data-testid="settings-page-config">
      <PageHeader title="Config" description="What this instance is configured to do.">
        <Button variant="outline" size="sm" onClick={() => void load()} data-testid="config-refresh">
          <RefreshCw aria-hidden className={loading ? "animate-spin" : undefined} />
          Refresh
        </Button>
      </PageHeader>

      <p className="max-w-3xl text-xs text-muted-foreground">
        Every field here is declared by the platform itself — this page renders whatever{" "}
        <span className="font-mono">genus config list</span> would print. Credentials are not
        editable here and their values are never shown. A field the box&apos;s environment supplies
        is read-only, because a change written under it would apply to nothing.
      </p>

      {notice ? (
        <div
          data-testid="config-restart-banner"
          className="flex max-w-3xl flex-wrap items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning"
        >
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
          <div className="min-w-0 flex-1">
            <p className="font-medium">Saved — but not running yet.</p>
            <p className="mt-0.5 text-warning/90">
              {notice.names.join(", ")} {notice.names.length === 1 ? "waits" : "wait"} on a restart
              of <span className="font-mono">{notice.units.join(", ")}</span>. Until then the
              running services keep the value they started with.
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={dismissRestartNotice}
            data-testid="config-restart-dismiss"
          >
            Dismiss
          </Button>
        </div>
      ) : null}

      {ignoredUnits.length > 0 ? (
        <p
          data-testid="config-ignored-notice"
          className="max-w-3xl rounded-lg border border-border bg-card p-3 text-xs text-muted-foreground"
        >
          Something in <span className="font-mono">config.yaml</span> is being ignored right now:
          for at least one restart-required setting the file says one thing and the running
          process&apos;s environment says another, and the environment wins. Clearing the variable
          and restarting <span className="font-mono">{ignoredUnits.join(", ")}</span> is what makes
          the file&apos;s value apply. This is not about anything saved here.
        </p>
      ) : null}

      <label className="flex max-w-md items-center gap-2 text-xs text-muted-foreground">
        <Search aria-hidden className="size-3.5" />
        <Input
          data-testid="config-search"
          value={query}
          placeholder="Filter by name or description"
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>

      {error ? (
        <p
          data-testid="config-error"
          className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
        >
          {error}
        </p>
      ) : null}

      {loading && groups === null ? (
        <div className="flex items-center gap-2 p-6 text-xs text-muted-foreground" data-testid="config-loading">
          <Loader2 aria-hidden className="size-4 animate-spin" />
          Reading what this instance declares…
        </div>
      ) : null}

      {groups !== null && groups.length === 0 && !error ? (
        <p data-testid="config-empty" className="max-w-3xl text-xs text-muted-foreground">
          This instance declared no settings at all. That is not a normal state — the settings model
          is part of the platform, so an empty schema means the bridge is running a build without
          one.
        </p>
      ) : null}

      {groups !== null && groups.length > 0 && visibleGroups.length === 0 ? (
        <p data-testid="config-no-matches" className="max-w-3xl text-xs text-muted-foreground">
          Nothing matches “{query.trim()}”. The filter reads the setting&apos;s name and its
          description, not its value.
        </p>
      ) : null}

      <div className="flex min-w-0 flex-col gap-2">
        {visibleGroups.map(({ group, shown, hidden }) => {
          const expanded = searching || open[group.id] === true;
          const states = shown.map(stateFor);
          // Counted over the WHOLE group, so the count and the save agree even
          // when the filter is hiding some of what will be posted.
          const dirty = group.fields.map(stateFor).filter((s) => s.dirty && writable(s));
          const sectionError = sectionErrors[group.id];
          return (
            <section
              key={group.id}
              data-testid={`config-group-${group.id}`}
              className="min-w-0 rounded-lg border border-border bg-card"
            >
              <button
                type="button"
                data-testid={`config-group-toggle-${group.id}`}
                aria-expanded={expanded}
                onClick={() => setOpen((prev) => ({ ...prev, [group.id]: !expanded }))}
                className="flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-left transition-colors hover:bg-accent/50"
              >
                {expanded ? (
                  <ChevronDown aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                ) : (
                  <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                )}
                <span className="truncate text-sm font-medium text-foreground">{group.label}</span>
                <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                  {searching ? `${shown.length} of ${group.fields.length}` : group.fields.length}{" "}
                  {group.fields.length === 1 ? "setting" : "settings"}
                  {dirty.length ? ` · ${dirty.length} changed` : ""}
                </span>
              </button>

              {expanded ? (
                <div className="flex min-w-0 flex-col gap-2 border-t border-border p-3">
                  {hidden > 0 ? (
                    <p
                      data-testid={`config-hidden-changes-${group.id}`}
                      className="text-[11px] text-warning"
                    >
                      {hidden} unsaved {hidden === 1 ? "change is" : "changes are"} hidden by the
                      filter. Saving this section posts {hidden === 1 ? "it" : "them"} too — clear
                      the filter to see {hidden === 1 ? "it" : "them"}.
                    </p>
                  ) : null}

                  {states.map((state) => (
                    <FieldRow
                      key={state.field.name}
                      state={state}
                      saved={saved[state.field.name] === true}
                      error={fieldErrors[state.field.name]}
                      onDraft={(next) => setDraft(state.field.name, next)}
                      onOpenFlags={onOpenFlags}
                    />
                  ))}

                  {sectionError ? (
                    <p
                      data-testid={`config-save-error-${group.id}`}
                      className="text-xs text-destructive"
                    >
                      {sectionError}
                    </p>
                  ) : null}

                  <div className="flex flex-wrap items-center gap-2 pt-1">
                    <Button
                      size="sm"
                      data-testid={`config-save-${group.id}`}
                      disabled={dirty.length === 0 || savingGroup === group.id}
                      onClick={() => void saveGroup(group)}
                    >
                      {savingGroup === group.id ? "Saving…" : `Save ${group.label}`}
                    </Button>
                    <span className="text-[11px] text-muted-foreground">
                      {dirty.length === 0
                        ? "Nothing changed in this section."
                        : `${dirty.length} changed — saved together, or not at all.`}
                    </span>
                  </div>
                </div>
              ) : null}
            </section>
          );
        })}
      </div>
    </div>
  );
}

function SourcePill({ field, source }: { field: SettingField; source: SettingSource }) {
  return (
    <span
      data-testid={`config-source-${field.name}`}
      className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] ${SOURCE_CLASS[source]}`}
    >
      {SOURCE_LABEL[source]}
    </span>
  );
}

function FieldRow({
  state,
  saved,
  error,
  onDraft,
  onOpenFlags,
}: {
  state: FieldState;
  saved: boolean;
  error?: string;
  onDraft: (next: string) => void;
  onOpenFlags?: () => void;
}) {
  const { field, current, draft } = state;
  const source = current?.source ?? "unknown";
  const secret = field.secret ? asSecretStatus(current?.value) : null;
  const known = EDITABLE_TYPES.has(field.type) || (field.choices?.length ?? 0) > 0;

  /**
   * Why this row has no control, or `null` when it has one.
   *
   * Every branch here ends in a SENTENCE, because a row with no input and no
   * explanation is the thing an operator files a bug about. Three of them, in
   * the order they have to be tested:
   *
   * 1. no entry in the values map at all — `editable` is `undefined`, which is
   *    not `false`, so testing `editable === false` first drew a live-looking
   *    input whose Save silently discarded what was typed (`writable()`
   *    requires `editable ?? false`);
   * 2. the route refused it — its own sentence, verbatim (see `SettingValue.reason`);
   * 3. a type this build has no control for.
   */
  const locked: string | null = !current
    ? `${field.env} is declared by this instance but was not reported by the server, so there is ` +
      `no value to change here. Refresh; if it stays missing, the bridge and the engine are not ` +
      `the same build.`
    : !current.editable
      ? (current.reason ?? unexplainedRefusal(field))
      : !known
        ? `This build does not know how to edit a ${field.type}. Set it with ` +
          `\`genus config set ${field.env}\` on the box; the value above is what is running.`
        : null;

  return (
    <div
      data-testid={`config-field-${field.name}`}
      className="flex min-w-0 flex-col gap-1.5 rounded-md border border-border/60 bg-background/40 p-2.5"
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="min-w-0 break-all font-mono text-[12px] text-foreground">{field.env}</span>
        <SourcePill field={field} source={source} />
        {field.hot ? (
          <span className="shrink-0 rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-[10px] text-success">
            applies live
          </span>
        ) : (
          <span className="shrink-0 rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
            restart {unitList(field.restartUnits)}
          </span>
        )}
        {saved ? (
          <span
            data-testid={`config-saved-${field.name}`}
            className="flex shrink-0 items-center gap-1 rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-[10px] text-success"
          >
            <Check aria-hidden className="size-3" />
            saved
          </span>
        ) : null}
      </div>

      {field.description ? (
        <p className="text-[11px] text-muted-foreground">{field.description}</p>
      ) : null}

      <div className="flex min-w-0 flex-wrap items-center gap-2">
        {field.secret ? (
          <span
            data-testid={`config-secret-${field.name}`}
            className="flex w-fit items-center gap-1.5 rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] text-muted-foreground"
          >
            <KeyRound aria-hidden className="size-3" />
            {secret?.configured
              ? // Capped. The route's fingerprint is `sha256:` + eight
                // characters; a longer one means the bridge is wrong, and the
                // secret path exists for exactly that case.
                `configured · ${(secret.fingerprint ?? "no fingerprint").slice(0, FINGERPRINT_MAX)}`
              : "not set here"}
          </span>
        ) : locked ? (
          <span className="min-w-0 break-all font-mono text-[12px] text-foreground">
            {draft || "(empty)"}
          </span>
        ) : (
          <FieldControl field={field} draft={draft} onDraft={onDraft} />
        )}

        {/*
          Outside the editable branch on purpose: a governed field is always
          editable under the current contract, and the day that stops being
          true the verdict is the FIRST thing its row should still offer.
        */}
        {field.governed ? (
          <Button
            variant="ghost"
            size="sm"
            data-testid={`config-flags-link-${field.name}`}
            onClick={() => onOpenFlags?.()}
          >
            <Flag aria-hidden />
            Verdict on Flags
          </Button>
        ) : null}

        {!field.secret && field.default !== null && field.default !== "" ? (
          <span
            data-testid={`config-default-${field.name}`}
            className="shrink-0 text-[10px] text-muted-foreground/80"
          >
            default {String(field.default)}
          </span>
        ) : null}
      </div>

      {locked ? (
        <p data-testid={`config-readonly-${field.name}`} className="text-[11px] text-warning">
          {locked}
        </p>
      ) : null}

      {field.secret ? (
        <p className="text-[11px] text-muted-foreground">
          That status reads the environment and config.yaml only — the vault is not consulted — so
          a credential stored with <span className="font-mono">genus vault set</span> shows here as
          not set. It is not a report that anything is missing.
        </p>
      ) : null}

      {error ? (
        <p data-testid={`config-error-${field.name}`} className="text-[11px] text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/**
 * One control, chosen by what the schema declares — never by the field's name.
 *
 * A type this build has not seen falls through to a text box rather than to
 * nothing: the settings model declares only str/int/float/bool today, and a
 * field added as a list must still be readable and saveable here rather than
 * rendering as a gap the operator cannot explain.
 */
function FieldControl({
  field,
  draft,
  onDraft,
}: {
  field: SettingField;
  draft: string;
  onDraft: (next: string) => void;
}) {
  if (field.choices && field.choices.length > 0) {
    return (
      <NativeSelect
        data-testid={`config-select-${field.name}`}
        value={draft}
        onChange={(event) => onDraft(event.target.value)}
      >
        {field.choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </NativeSelect>
    );
  }

  if (field.type === "bool") {
    const on = draft === "true";
    return (
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={field.env}
        data-testid={`config-switch-${field.name}`}
        onClick={() => onDraft(on ? "false" : "true")}
        className={`inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors ${
          on ? "border-primary/40 bg-primary/70" : "border-border bg-muted"
        }`}
      >
        <span
          className={`size-3.5 rounded-full bg-background transition-transform ${
            on ? "translate-x-[18px]" : "translate-x-[3px]"
          }`}
        />
      </button>
    );
  }

  const numeric = field.type === "int" || field.type === "float";
  return (
    <Input
      data-testid={`config-input-${field.name}`}
      type={numeric ? "number" : "text"}
      step={field.type === "float" ? "any" : undefined}
      autoComplete="off"
      spellCheck={false}
      className="h-8 min-w-0 flex-1 text-sm sm:max-w-md"
      value={draft}
      onChange={(event) => onDraft(event.target.value)}
    />
  );
}

export default ConfigPage;
