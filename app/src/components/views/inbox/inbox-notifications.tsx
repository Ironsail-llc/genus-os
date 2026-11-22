"use client";

import { ArrowUpRight, Bell } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useMyNotifications } from "@/hooks/use-inbox";
import { absoluteTime, relativeTime } from "@/lib/inbox/pending";
import { roomOfNotification, type InboxNotification } from "@/lib/inbox/notifications";
import { ROOM_LABEL } from "@/lib/rooms/labels";
import { isPlainClick, navigateTo } from "@/lib/rooms/navigate";
import { roomLink } from "@/lib/rooms/room-id";

/**
 * The signed-in person's own notifications, below what is waiting on them.
 * A workspace note links to its workspace, opened in place. Marking one read
 * is this user's row only.
 */
export function InboxNotifications({ visible }: { visible: boolean }) {
  const { notifications, isLoading, error, markRead } = useMyNotifications({ active: visible });

  return (
    <section aria-labelledby="inbox-notifications-label" className="flex flex-col gap-2 pt-2">
      <h2
        id="inbox-notifications-label"
        className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-[0.07em] text-muted-foreground/80"
      >
        <Bell className="size-3.5" aria-hidden />
        Notifications
      </h2>
      {error ? (
        <p className="text-xs text-muted-foreground" data-testid="inbox-notifications-error">
          Your notifications could not be read ({error.status || "no answer"}).
        </p>
      ) : isLoading ? (
        <div className="h-12 animate-pulse rounded-lg bg-muted/60" aria-busy="true" aria-label="Loading notifications" />
      ) : notifications.length === 0 ? (
        <p className="text-xs text-muted-foreground" data-testid="inbox-notifications-empty">
          No notifications for you.
        </p>
      ) : (
        <ul className="divide-y divide-border rounded-lg border border-border bg-card" data-testid="inbox-notifications">
          {notifications.map((note) => (
            <NotificationRow key={note.id} note={note} onRead={() => void markRead(note.id)} />
          ))}
        </ul>
      )}
    </section>
  );
}

function NotificationRow({ note, onRead }: { note: InboxNotification; onRead: () => void }) {
  const roomId = roomOfNotification(note);
  const unread = !note.readAt;
  const when = relativeTime(note.createdAt);
  return (
    <li
      className="flex flex-wrap items-start gap-x-3 gap-y-2 px-3 py-2.5"
      data-testid={`inbox-notification-${note.id}`}
      data-unread={unread ? "true" : undefined}
    >
      <span
        className={`mt-1.5 size-1.5 shrink-0 rounded-full ${unread ? "bg-primary" : "bg-transparent"}`}
        aria-label={unread ? "Unread" : undefined}
      />
      <div className="min-w-0 flex-1">
        <p className={`text-sm wrap-anywhere ${unread ? "font-medium text-foreground" : "text-foreground/80"}`}>
          {note.subject || "Notification"}
        </p>
        {note.body && <p className="mt-0.5 text-xs text-muted-foreground wrap-anywhere">{note.body}</p>}
        {when && (
          <p className="mt-0.5 text-[11px] text-muted-foreground" title={absoluteTime(note.createdAt)}>
            {when}
          </p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {roomId && (
          <a
            href={roomLink(roomId)}
            onClick={(event) => {
              if (!isPlainClick(event)) return;
              event.preventDefault();
              navigateTo(roomLink(roomId));
            }}
            className="inline-flex h-6 items-center gap-1 rounded-md border border-border px-2 text-xs text-foreground transition-colors hover:bg-accent focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            data-testid={`inbox-notification-open-${note.id}`}
          >
            Open {ROOM_LABEL.singular}
            <ArrowUpRight className="size-3" aria-hidden />
          </a>
        )}
        {unread && (
          <Button size="xs" variant="ghost" onClick={onRead} data-testid={`inbox-notification-read-${note.id}`}>
            Mark read
          </Button>
        )}
      </div>
    </li>
  );
}
