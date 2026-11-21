"use client";

import { useId, useMemo, useState, type ReactNode } from "react";
import { Copy, Loader2, PauseCircle, PlayCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { RevisionHistory } from "@/components/rooms/revision-history";
import type { RoomAction } from "@/lib/rooms/access";
import { RoomRequestError, patchRoom, type DefinitionChanges } from "@/lib/rooms/api";
import { formatWhen } from "@/lib/rooms/format";
import { ROOM_LABEL } from "@/lib/rooms/labels";
import type { Room } from "@/lib/rooms/types";

export interface RoomSettingsDrawerProps {
  room: Room;
  /** What this user may do here (archived rooms already narrowed). */
  can: (action: RoomAction) => boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** A change was saved, or a reload was asked for: read the room again. */
  onSaved: () => void;
  /** "Duplicate as mine", offered to a user who cannot edit. */
  onDuplicate: () => void;
}

interface Form {
  title: string;
  instructions: string;
  scheduleText: string;
  tz: string;
  defaultGrain: string;
  notifyDefault: boolean;
}

function setting(room: Room, ...keys: string[]): unknown {
  for (const key of keys) {
    if (room.settings && key in room.settings) return room.settings[key];
  }
  return undefined;
}

function formOf(room: Room): Form {
  const text = (v: unknown) => (typeof v === "string" ? v : "");
  return {
    title: room.title ?? "",
    instructions: room.instructions ?? "",
    scheduleText: text(setting(room, "schedule_text", "scheduleText")),
    tz: text(setting(room, "tz")) || "UTC",
    defaultGrain: text(setting(room, "default_grain", "defaultGrain")) || "week",
    notifyDefault: setting(room, "notify_default", "notifyDefault") !== false,
  };
}

let zones: string[] | null = null;
function timeZones(current: string): string[] {
  if (!zones) {
    try {
      zones = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
    } catch {
      zones = [];
    }
  }
  const all = new Set(["UTC", ...zones]);
  if (current) all.add(current);
  return Array.from(all);
}

/**
 * A workspace's settings, and its revision history.
 *
 * An assigned user (or an admin) edits; everyone else with access reads the
 * same fields and is offered "Duplicate as mine". A save sends only what
 * changed, against the revision the form was read at: a stale form gets a
 * 409, is told so and offered a reload, and nothing is overwritten. A 422
 * puts the server's message on the field it is about.
 */
export function RoomSettingsDrawer({ room, can, open, onOpenChange, onSaved, onDuplicate }: RoomSettingsDrawerProps) {
  const editable = can("edit_definition");
  const isReport = room.kind === "report";
  const [tab, setTab] = useState<"settings" | "history">("settings");
  const [form, setForm] = useState<Form>(() => formOf(room));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  // A newer revision of the room (after a save, a reload, a restore) resets
  // the form to it. Read at render, not in an effect, so the fields never
  // show the old revision for a frame.
  const [readAt, setReadAt] = useState({ id: room.id, rev: room.rev, paused: room.paused });
  if (readAt.id !== room.id || readAt.rev !== room.rev || readAt.paused !== room.paused) {
    setReadAt({ id: room.id, rev: room.rev, paused: room.paused });
    setForm(formOf(room));
    setErrors({});
    setConflict(false);
  }

  const base = useMemo(() => formOf(room), [room]);
  const changes = useMemo(() => {
    const out: DefinitionChanges = {};
    if (form.title !== base.title) out.title = form.title.trim();
    if (form.instructions !== base.instructions) out.instructions = form.instructions;
    if (isReport) {
      if (form.scheduleText !== base.scheduleText) out.scheduleText = form.scheduleText.trim();
      if (form.tz !== base.tz) out.tz = form.tz;
      if (form.defaultGrain !== base.defaultGrain) out.defaultGrain = form.defaultGrain as DefinitionChanges["defaultGrain"];
      if (form.notifyDefault !== base.notifyDefault) out.notifyDefault = form.notifyDefault;
    }
    return out;
  }, [form, base, isReport]);
  const dirty = Object.keys(changes).length > 0;

  const update = <K extends keyof Form>(key: K, value: Form[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    setSaved(false);
    setErrors((e) => {
      if (!(key in e) && !("form" in e)) return e;
      const next = { ...e };
      delete next[key];
      delete next.form;
      return next;
    });
  };

  const save = async (toSend: DefinitionChanges) => {
    setSaving(true);
    setSaved(false);
    try {
      await patchRoom(room.id, room.rev, toSend);
      setSaved(true);
      onSaved();
    } catch (err) {
      if (err instanceof RoomRequestError && err.status === 409) {
        setConflict(true);
      } else if (err instanceof RoomRequestError && err.status === 422) {
        const field = err.field && err.field in form ? err.field : "form";
        setErrors({ [field]: err.serverMessage ?? "The server refused this change." });
      } else {
        const status = err instanceof RoomRequestError ? err.status : 0;
        setErrors({ form: status ? `Not saved: the server answered ${status}.` : "Not saved: no answer from the server." });
      }
    } finally {
      setSaving(false);
    }
  };

  const onSubmit = () => {
    if (!form.title.trim()) {
      setErrors({ title: "A title is required." });
      return;
    }
    if (!dirty) return;
    void save(changes);
  };

  const nextRun = room.paused
    ? "Paused: no scheduled runs. Resuming schedules the next run from now, with no catch-up of missed runs."
    : room.nextRunAt
      ? `Next run: ${formatWhen(room.nextRunAt)}`
      : form.scheduleText
        ? "The next run time appears once the schedule is saved."
        : "No schedule: it runs only when someone runs it.";

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-full gap-0 overflow-y-auto sm:max-w-md"
        data-testid="room-settings"
      >
        <SheetHeader className="border-b border-border pr-10">
          <SheetTitle>{editable ? "Settings" : "Details"}</SheetTitle>
          <SheetDescription className="truncate">{room.title}</SheetDescription>
          <div role="tablist" aria-label="Settings sections" className="mt-2 flex gap-1">
            <TabButton active={tab === "settings"} onClick={() => setTab("settings")} testId="settings-tab-settings">
              Settings
            </TabButton>
            <TabButton active={tab === "history"} onClick={() => setTab("history")} testId="settings-tab-history">
              History
            </TabButton>
          </div>
        </SheetHeader>

        {tab === "history" ? (
          <div className="p-4" role="tabpanel">
            <RevisionHistory
              roomId={room.id}
              currentRev={room.rev}
              canRestore={can("restore_revision")}
              onRestored={onSaved}
            />
          </div>
        ) : (
          <form
            role="tabpanel"
            className="flex flex-col gap-4 p-4"
            onSubmit={(event) => {
              event.preventDefault();
              onSubmit();
            }}
            noValidate
          >
            {!editable && (
              <div className="rounded-lg border border-border bg-muted/50 p-3 text-sm" data-testid="settings-readonly">
                <p className="text-foreground">
                  Only the people assigned to this {ROOM_LABEL.singular} can change its settings.
                </p>
                {can("duplicate") && (
                  <>
                    <p className="mt-1 text-xs text-muted-foreground">Make your own copy to change it your way.</p>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="mt-2"
                      onClick={onDuplicate}
                      data-testid="settings-duplicate"
                    >
                      <Copy className="size-3.5" aria-hidden />
                      Duplicate as mine
                    </Button>
                  </>
                )}
              </div>
            )}
            {conflict && (
              <div
                className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm"
                role="alert"
                data-testid="settings-conflict"
              >
                <p className="font-medium text-foreground">
                  These settings were changed since you opened them, by someone else. Nothing was saved.
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  Reload to see their version, then make your change again.
                </p>
                <Button type="button" variant="outline" size="sm" className="mt-2" onClick={onSaved}>
                  Reload
                </Button>
              </div>
            )}

            <Field label="Title" name="title" error={errors.title}>
              {(props) => (
                <Input
                  {...props}
                  value={form.title}
                  readOnly={!editable}
                  onChange={(e) => update("title", e.target.value)}
                />
              )}
            </Field>
            <Field label="Instructions" name="instructions" error={errors.instructions} hint="What the agent does each run.">
              {(props) => (
                <textarea
                  {...props}
                  value={form.instructions}
                  readOnly={!editable}
                  rows={6}
                  onChange={(e) => update("instructions", e.target.value)}
                  className="min-h-28 w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm text-foreground shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 aria-invalid:border-destructive dark:bg-input/30"
                />
              )}
            </Field>

            {isReport && (
              <>
                <Field
                  label="Schedule"
                  name="scheduleText"
                  error={errors.scheduleText}
                  hint={
                    <span data-testid="settings-next-run">{nextRun}</span>
                  }
                >
                  {(props) => (
                    <Input
                      {...props}
                      value={form.scheduleText}
                      readOnly={!editable}
                      placeholder="every Monday at 08:00"
                      onChange={(e) => update("scheduleText", e.target.value)}
                    />
                  )}
                </Field>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <Field label="Time zone" name="tz" error={errors.tz}>
                    {(props) => (
                      <NativeSelect
                        {...props}
                        value={form.tz}
                        disabled={!editable}
                        onChange={(e) => update("tz", e.target.value)}
                        wrapperClassName="w-full"
                        className="w-full"
                      >
                        {timeZones(form.tz).map((zone) => (
                          <option key={zone} value={zone}>
                            {zone}
                          </option>
                        ))}
                      </NativeSelect>
                    )}
                  </Field>
                  <Field label="Default period" name="defaultGrain" error={errors.defaultGrain}>
                    {(props) => (
                      <NativeSelect
                        {...props}
                        value={form.defaultGrain}
                        disabled={!editable}
                        onChange={(e) => update("defaultGrain", e.target.value)}
                        wrapperClassName="w-full"
                        className="w-full"
                      >
                        <option value="day">Day</option>
                        <option value="week">Week</option>
                        <option value="month">Month</option>
                      </NativeSelect>
                    )}
                  </Field>
                </div>
                <CheckField
                  label="Notify people with access when an edition is published"
                  checked={form.notifyDefault}
                  disabled={!editable}
                  onChange={(v) => update("notifyDefault", v)}
                  hint="Each person can still mute it for themselves."
                />
              </>
            )}

            {errors.form && (
              <p className="text-sm text-destructive" role="alert" data-testid="field-error-form">
                {errors.form}
              </p>
            )}

            {editable && (
              <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
                <Button type="submit" size="sm" disabled={saving || conflict || !dirty} data-testid="settings-save">
                  {saving && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
                  Save
                </Button>
                {isReport && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={saving || conflict}
                    onClick={() => void save({ paused: !room.paused })}
                    data-testid="settings-pause"
                  >
                    {room.paused ? <PlayCircle className="size-3.5" aria-hidden /> : <PauseCircle className="size-3.5" aria-hidden />}
                    {room.paused ? "Resume scheduled runs" : "Pause scheduled runs"}
                  </Button>
                )}
                {saved && (
                  <span className="text-xs text-success" role="status" data-testid="settings-saved">
                    Saved
                  </span>
                )}
              </div>
            )}
          </form>
        )}
      </SheetContent>
    </Sheet>
  );
}

function TabButton({
  active,
  onClick,
  testId,
  children,
}: {
  active: boolean;
  onClick: () => void;
  testId: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-ring ${
        active ? "bg-primary/10 text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"
      }`}
      data-testid={testId}
    >
      {children}
    </button>
  );
}

interface FieldProps {
  id: string;
  name: string;
  "aria-invalid"?: boolean;
  "aria-describedby"?: string;
}

function Field({
  label,
  name,
  error,
  hint,
  children,
}: {
  label: string;
  name: string;
  error?: string;
  hint?: ReactNode;
  children: (props: FieldProps) => ReactNode;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-xs font-medium text-foreground">
        {label}
      </label>
      {children({ id, name, "aria-invalid": error ? true : undefined, "aria-describedby": describedBy })}
      {hint && (
        <p id={hintId} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-xs text-destructive" data-testid={`field-error-${name}`}>
          {error}
        </p>
      )}
    </div>
  );
}

function CheckField({
  label,
  checked,
  disabled,
  onChange,
  hint,
}: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
  hint?: string;
}) {
  const id = useId();
  return (
    <div className="flex items-start gap-2.5">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 size-4 rounded border-input accent-primary"
      />
      <div>
        <label htmlFor={id} className="text-sm text-foreground">
          {label}
        </label>
        {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      </div>
    </div>
  );
}
