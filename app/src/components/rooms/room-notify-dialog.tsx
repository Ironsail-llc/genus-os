"use client";

import { useId, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { refusalText } from "@/components/rooms/controls-shared";
import { refusalOf } from "@/lib/query/fetch-refused";
import { useAppQueryClient } from "@/lib/query/query-client";
import { fetchNotify, putNotify, type NotifyPrefs, type NotifyState } from "@/lib/rooms/api";
import { ROOM_LABEL } from "@/lib/rooms/labels";

/**
 * This user's own notification choices for one workspace: mute it, in-app
 * notes, and an external channel only when the user has one linked. Nobody
 * else's choices change; the unread count keeps counting when muted.
 */
export function RoomNotifyDialog({
  roomId,
  open,
  onOpenChange,
}: {
  roomId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const client = useAppQueryClient();
  const query = useQuery(
    { queryKey: ["rooms", roomId, "notify"], queryFn: ({ signal }) => fetchNotify(roomId, signal), enabled: open },
    client,
  );
  const refused = refusalOf(query.error);

  let body: ReactNode;
  if (query.isPending) {
    body = (
      <p className="inline-flex items-center gap-2 text-sm text-muted-foreground" aria-busy="true">
        <Loader2 className="size-4 animate-spin" aria-hidden />
        Loading…
      </p>
    );
  } else if (query.isError || !query.data) {
    body = (
      <p className="text-sm text-muted-foreground" role="alert" data-testid="notify-error">
        Your notification choices could not be read
        {refused?.status ? ` (the server answered ${refused.status})` : ""}. Nothing has been changed.
      </p>
    );
  } else {
    body = (
      <NotifyForm
        roomId={roomId}
        state={query.data}
        onSaved={(prefs) => client.setQueryData(["rooms", roomId, "notify"], { ...query.data, ...prefs })}
      />
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" data-testid="notify-dialog">
        <DialogHeader>
          <DialogTitle>Notifications</DialogTitle>
          <DialogDescription>How this {ROOM_LABEL.singular} tells you about new editions. Only you are affected.</DialogDescription>
        </DialogHeader>
        {body}
      </DialogContent>
    </Dialog>
  );
}

function NotifyForm({
  roomId,
  state,
  onSaved,
}: {
  roomId: string;
  state: NotifyState;
  onSaved: (prefs: NotifyPrefs) => void;
}) {
  const [muted, setMuted] = useState(state.muted);
  const [inApp, setInApp] = useState(state.inApp);
  const [channels, setChannels] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(state.linkedChannels.map((c) => [c.name, state.channels[c.name] === true])),
  );
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const touch = () => {
    setSaved(false);
    setError(null);
  };

  const save = async () => {
    const prefs: NotifyPrefs = { inApp, muted, channels };
    setSaving(true);
    setError(null);
    try {
      await putNotify(roomId, prefs);
      setSaved(true);
      onSaved(prefs);
    } catch (err) {
      setError(refusalText(err, "save your choices"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <Check
        label={`Mute this ${ROOM_LABEL.singular}`}
        checked={muted}
        onChange={(v) => {
          setMuted(v);
          touch();
        }}
        testId="notify-mute"
      />
      {muted && (
        <p className="-mt-1 pl-6 text-xs text-muted-foreground">
          You won&apos;t be notified. The unread count still shows on the rail.
        </p>
      )}
      <Check
        label="In-app notifications"
        checked={inApp && !muted}
        disabled={muted}
        onChange={(v) => {
          setInApp(v);
          touch();
        }}
        testId="notify-in-app"
      />
      <fieldset className="flex flex-col gap-2">
        <legend className="pb-1 text-xs font-medium text-foreground">Also send to</legend>
        {state.linkedChannels.length === 0 ? (
          <p className="text-xs text-muted-foreground" data-testid="notify-no-channels">
            No external channel is linked to your account, so only in-app notifications are offered.
          </p>
        ) : (
          state.linkedChannels.map((channel) => (
            <Check
              key={channel.name}
              label={`${channel.label} (a summary and a link)`}
              checked={channels[channel.name] === true && !muted}
              disabled={muted}
              onChange={(v) => {
                setChannels((c) => ({ ...c, [channel.name]: v }));
                touch();
              }}
              testId={`notify-channel-${channel.name}`}
            />
          ))
        )}
      </fieldset>
      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
      <div className="flex items-center gap-2 pt-1">
        <Button type="submit" size="sm" disabled={saving} data-testid="notify-save">
          {saving && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
          Save
        </Button>
        {saved && (
          <span className="text-xs text-success" role="status" data-testid="notify-saved">
            Saved
          </span>
        )}
      </div>
    </form>
  );
}

function Check({
  label,
  checked,
  disabled,
  onChange,
  testId,
}: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
  testId: string;
}) {
  const id = useId();
  return (
    <div className="flex items-center gap-2">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="size-4 accent-primary"
        data-testid={testId}
      />
      <label htmlFor={id} className={`text-sm ${disabled ? "text-muted-foreground" : "text-foreground"}`}>
        {label}
      </label>
    </div>
  );
}
