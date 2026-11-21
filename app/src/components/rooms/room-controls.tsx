"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import {
  Archive,
  ArchiveRestore,
  Bell,
  Copy,
  FlaskConical,
  Fingerprint,
  Loader2,
  MoreHorizontal,
  Play,
  Settings2,
  Share2,
} from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { EditionCard } from "@/components/rooms/edition-card";
import { PeriodSelector } from "@/components/rooms/period-selector";
import { ProvenanceDrawer } from "@/components/rooms/provenance-drawer";
import { RoomNotifyDialog } from "@/components/rooms/room-notify-dialog";
import { RoomSettingsDrawer } from "@/components/rooms/room-settings-drawer";
import type { RoomSlotContext, RoomViewSlots } from "@/components/rooms/room-slots";
import { ShareDialog } from "@/components/rooms/share-dialog";
import {
  editionError,
  seedEditionWatch,
  useEditionArrival,
  useEditionWatch,
  type EditionRequest,
} from "@/components/rooms/use-edition-watch";
import { ROOMS_QUERY_KEY } from "@/hooks/use-rooms";
import { useAppQueryClient } from "@/lib/query/query-client";
import type { RoomAction } from "@/lib/rooms/access";
import { archiveRoom, duplicateRoom, runRoom, unarchiveRoom } from "@/lib/rooms/api";
import { goToRoom, refusalText } from "@/components/rooms/controls-shared";
import type { Edition } from "@/lib/rooms/types";


type Panel = "settings" | "share" | "provenance" | "notify" | "archive" | "preview" | null;

/**
 * The workspace header's controls, by what this user may do here: Run now
 * and a preview run (report workspaces), Settings (read-only for a shared
 * user), Share (assigned and admins), and a menu with notifications,
 * provenance, Duplicate as mine and Archive. A viewer gets none of the
 * mutating ones. The bridge refuses anyway; this keeps the screen honest.
 */
export function RoomHeaderActions({ ctx }: { ctx: RoomSlotContext }) {
  const { room } = ctx;
  const client = useAppQueryClient();
  // `ctx.can` already applies the archived rule (only read, archive,
  // duplicate and list_all in an archived room).
  const allowed = (action: RoomAction) => ctx.can(action);
  const isReport = room.kind === "report";

  const [panel, setPanel] = useState<Panel>(null);
  const [running, setRunning] = useState<EditionRequest | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ id: string | null; error: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [menuError, setMenuError] = useState<string | null>(null);
  const previewHint = useId();

  const reloadLists = () => {
    void client.invalidateQueries({ queryKey: ROOMS_QUERY_KEY });
    void client.invalidateQueries({ queryKey: ["rooms", "all"] });
  };

  // A run: done when its edition is among the published ones (an event or the
  // poll brought it in), and then it is shown.
  const { editions, showEdition, refresh } = ctx;
  const run = useEditionArrival(room.id, running, editions, showEdition, refresh);
  const runFailure = run.failed
    ? `The run failed${editionError(run.failed) ? `: ${editionError(run.failed)}` : "."}`
    : null;

  const runNow = async () => {
    setRunError(null);
    try {
      const edition = await runRoom(room.id);
      if (edition) seedEditionWatch(client, room.id, edition);
      setRunning({ key: Date.now(), id: edition?.id ?? null, afterSeq: editions[0]?.seq ?? 0 });
    } catch (err) {
      setRunError(refusalText(err, "start the run"));
    }
  };

  const runPreview = async () => {
    setPanel("preview");
    setPreview({ id: null, error: null });
    try {
      const edition = await runRoom(room.id, { preview: true });
      setPreview({ id: edition?.id ?? null, error: edition ? null : "The preview started, but the server did not say which edition it is building." });
    } catch (err) {
      setPreview({ id: null, error: refusalText(err, "start the preview") });
    }
  };

  const act = async (fn: () => Promise<unknown>, doing: string, after: () => void) => {
    setBusy(true);
    setMenuError(null);
    try {
      await fn();
      after();
    } catch (err) {
      setMenuError(refusalText(err, doing));
    } finally {
      setBusy(false);
    }
  };

  const duplicate = () =>
    act(
      async () => {
        const copy = await duplicateRoom(room.id);
        reloadLists();
        if (copy?.id) goToRoom(copy.id);
      },
      "duplicate it",
      () => setPanel(null),
    );

  const menuItems: MenuItem[] = [
    { id: "notify", label: "Notifications", icon: Bell, onSelect: () => setPanel("notify") },
  ];
  if (ctx.edition) {
    menuItems.push({ id: "provenance", label: "How this edition was made", icon: Fingerprint, onSelect: () => setPanel("provenance") });
  }
  if (allowed("duplicate")) {
    menuItems.push({ id: "duplicate", label: "Duplicate as mine", icon: Copy, onSelect: () => void duplicate() });
  }
  if (allowed("archive") && !room.archived) {
    menuItems.push({ id: "archive", label: "Archive", icon: Archive, onSelect: () => setPanel("archive") });
  }
  // Restoring is an admin's, and it is the one thing done TO an archived
  // room that the archived rule does not list: asked of the access level.
  if (room.archived && ctx.access === "admin") {
    menuItems.push({
      id: "unarchive",
      label: "Restore",
      icon: ArchiveRestore,
      onSelect: () =>
        void act(() => unarchiveRoom(room.id), "restore it", () => {
          refresh();
          reloadLists();
        }),
    });
  }

  const canRun = isReport && allowed("run");
  const readOnlySettings = !allowed("edit_definition");

  return (
    <>
      {(runError ?? runFailure) && (
        <p className="max-w-[16rem] text-xs text-destructive" role="alert" data-testid="room-run-error">
          {runError ?? runFailure}
        </p>
      )}
      {menuError && (
        <p className="max-w-[16rem] text-xs text-destructive" role="alert" data-testid="room-action-error">
          {menuError}
        </p>
      )}
      {(run.waiting || run.timedOut) && (
        <span
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"
          aria-live="polite"
          data-testid="room-run-status"
        >
          {run.timedOut ? (
            "Still running. Check back shortly: the edition appears when it is ready."
          ) : (
            <>
              <Loader2 className="size-3.5 animate-spin" aria-hidden />
              Running…
            </>
          )}
        </span>
      )}
      {canRun && (
        <HeaderButton
          testId="room-run-now"
          icon={Play}
          label="Run now"
          onClick={() => void runNow()}
          disabled={run.waiting}
        />
      )}
      {canRun && (
        <>
          <HeaderButton
            testId="room-preview-run"
            icon={FlaskConical}
            label="Preview run"
            onClick={() => void runPreview()}
            describedBy={previewHint}
          />
          <span id={previewHint} className="sr-only">
            A dry run only you can see. Nobody is notified and no tasks are created.
          </span>
        </>
      )}
      {ctx.access !== "viewer" && (
        <HeaderButton
          testId="room-settings-open"
          icon={Settings2}
          label={readOnlySettings ? "Details" : "Settings"}
          onClick={() => setPanel("settings")}
        />
      )}
      {allowed("share") && (
        <HeaderButton testId="room-share-open" icon={Share2} label="Share" onClick={() => setPanel("share")} />
      )}
      <OverflowMenu items={menuItems} disabled={busy} />

      {panel === "settings" && (
        <RoomSettingsDrawer
          room={room}
          can={allowed}
          open
          onOpenChange={(open) => !open && setPanel(null)}
          onSaved={() => {
            refresh();
            reloadLists();
          }}
          onDuplicate={() => void duplicate()}
        />
      )}
      {panel === "share" && (
        <ShareDialog
          room={room}
          open
          onOpenChange={(open) => !open && setPanel(null)}
          onChanged={() => {
            refresh();
            reloadLists();
          }}
        />
      )}
      {panel === "provenance" && ctx.edition && (
        <ProvenanceDrawer
          edition={ctx.edition}
          members={room.members ?? []}
          open
          onOpenChange={(open) => !open && setPanel(null)}
        />
      )}
      {panel === "notify" && (
        <RoomNotifyDialog roomId={room.id} open onOpenChange={(open) => !open && setPanel(null)} />
      )}
      {panel === "archive" && (
        <ArchiveConfirm
          title={room.title}
          busy={busy}
          onCancel={() => setPanel(null)}
          onConfirm={() =>
            void act(() => archiveRoom(room.id), "archive it", () => {
              setPanel(null);
              refresh();
              reloadLists();
            })
          }
        />
      )}
      {panel === "preview" && preview && (
        <PreviewPanel roomId={room.id} editionId={preview.id} startError={preview.error} onClose={() => setPanel(null)} />
      )}
    </>
  );
}

function HeaderButton({
  testId,
  icon: Icon,
  label,
  onClick,
  disabled,
  describedBy,
}: {
  testId: string;
  icon: typeof Play;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  describedBy?: string;
}) {
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      onClick={onClick}
      disabled={disabled}
      aria-describedby={describedBy}
      title={label}
      className="h-8 gap-1.5 px-2.5 text-xs"
      data-testid={testId}
    >
      <Icon className="size-3.5" aria-hidden />
      {/* Icon-only on a phone; the name stays for screen readers. */}
      <span className="sr-only sm:not-sr-only">{label}</span>
    </Button>
  );
}

interface MenuItem {
  id: string;
  label: string;
  icon: typeof Play;
  onSelect: () => void;
}

/** A small menu: arrow keys move, Escape and a click outside close it. */
function OverflowMenu({ items, disabled }: { items: MenuItem[]; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    root.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const onKeyDown = (event: React.KeyboardEvent) => {
    const entries = Array.from(root.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    const at = entries.indexOf(document.activeElement as HTMLElement);
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      button.current?.focus();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      entries[(at + step + entries.length) % entries.length]?.focus();
    }
  };

  return (
    <div className="relative" ref={root} onKeyDown={onKeyDown}>
      <Button
        ref={button}
        type="button"
        variant="outline"
        size="icon-sm"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label="More actions"
        title="More actions"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className="size-8"
        data-testid="room-more"
      >
        <MoreHorizontal className="size-4" aria-hidden />
      </Button>
      {open && (
        <div
          id={menuId}
          role="menu"
          aria-label="More actions"
          className="absolute right-0 top-full z-40 mt-1 min-w-[13rem] rounded-lg border border-border bg-popover p-1 text-popover-foreground shadow-lg"
        >
          {items.map((item) => (
            <button
              key={item.id}
              type="button"
              role="menuitem"
              tabIndex={-1}
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
              className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm text-foreground transition-colors hover:bg-accent focus:bg-accent focus:outline-none"
              data-testid={`room-menu-${item.id}`}
            >
              <item.icon className="size-4 text-muted-foreground" aria-hidden />
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ArchiveConfirm({
  title,
  busy,
  onCancel,
  onConfirm,
}: {
  title: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog open onOpenChange={(open) => !open && onCancel()}>
      <DialogContent className="sm:max-w-md" data-testid="room-archive-confirm">
        <DialogHeader>
          <DialogTitle>Archive “{title}”?</DialogTitle>
          <DialogDescription>
            It leaves everyone&apos;s rail and its schedule stops. Its history, editions and tasks are kept, and an
            admin can restore it.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button type="button" variant="destructive" onClick={onConfirm} disabled={busy}>
            Archive
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * A preview run's edition, for the person who asked for it and nobody else:
 * it is never put in the room's pinned place, and the bridge only returns it
 * to its requester.
 */
function PreviewPanel({
  roomId,
  editionId,
  startError,
  onClose,
}: {
  roomId: string;
  editionId: string | null;
  startError: string | null;
  onClose: () => void;
}) {
  const edition = useEditionWatch(roomId, editionId);
  let body: ReactNode;
  if (startError) {
    body = (
      <p className="text-sm text-destructive" role="alert" data-testid="room-preview-failed">
        {startError}
      </p>
    );
  } else if (edition?.status === "failed") {
    body = (
      <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm" role="alert" data-testid="room-preview-failed">
        <p className="font-medium text-foreground">The preview failed</p>
        <p className="mt-1 text-muted-foreground">{editionError(edition) ?? "The run did not say why."}</p>
      </div>
    );
  } else if (edition?.status === "published") {
    body = <EditionCard edition={edition as Edition} />;
  } else {
    body = (
      <p className="inline-flex items-center gap-2 text-sm text-muted-foreground" aria-live="polite">
        <Loader2 className="size-4 animate-spin" aria-hidden />
        Building the preview…
      </p>
    );
  }
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl" data-testid="room-preview">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            Preview run <PreviewBadge />
          </DialogTitle>
          <DialogDescription>
            Only you can see this. Nobody is notified, nothing is posted to the conversation, and no tasks are
            created.
          </DialogDescription>
        </DialogHeader>
        {body}
      </DialogContent>
    </Dialog>
  );
}

function PreviewBadge() {
  return (
    <span
      className="rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-[10.5px] font-medium text-warning"
      data-testid="room-preview-badge"
    >
      Preview
    </span>
  );
}

/**
 * The slots lane 10 fills: the header's controls, and report rooms' period
 * selector. The shell passes the lazily loaded copy in `room-controls-lazy.tsx`.
 */
export const ROOM_CONTROL_SLOTS: RoomViewSlots = {
  headerActions: (ctx) => <RoomHeaderActions ctx={ctx} />,
  periodSelector: (ctx) => <PeriodSelector ctx={ctx} />,
};
