/**
 * The signed-in person's own notifications, for the Inbox.
 *
 * Read off the existing routes, not a new one: `GET /api/auth/me` names the
 * account, then `GET /api/notifications?toAgent={id}` lists what was sent to
 * it. The id is always sent, operator or not: with no filter an operator's
 * listing is the whole tenant's, and the Inbox is "mine". A member asking for
 * their own id is what the route already allows (`notifications.py`).
 *
 * Marking read is `POST /api/notifications/{id}/read` on one row, and a row
 * has one recipient, so it changes nothing for anybody else.
 */
import { FetchRefused } from "@/lib/query/fetch-refused";
import { isRoomId } from "@/lib/rooms/room-id";

const BRIDGE = "/api/bridge/api";
const ME_ENDPOINT = `${BRIDGE}/auth/me`;
export const NOTIFICATIONS_ENDPOINT = `${BRIDGE}/notifications`;

/** One row as `notification_to_dict` writes it. */
export interface InboxNotification {
  id: string;
  fromAgent: string;
  notificationType: string;
  subject: string;
  body: string;
  metadata: Record<string, unknown>;
  readAt: string | null;
  createdAt: string | null;
}

async function getJson(endpoint: string, signal?: AbortSignal): Promise<unknown> {
  let res: Response;
  try {
    res = await fetch(endpoint, { signal, cache: "no-store" });
  } catch (err) {
    if (signal?.aborted) throw err;
    throw new FetchRefused(endpoint, 0);
  }
  if (!res.ok) throw new FetchRefused(endpoint, res.status);
  try {
    return await res.json();
  } catch {
    throw new FetchRefused(endpoint, 0);
  }
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export async function fetchMyNotifications(signal?: AbortSignal): Promise<InboxNotification[]> {
  const me = (await getJson(ME_ENDPOINT, signal)) as { id?: unknown } | null;
  const id = text(me?.id);
  if (!id) throw new FetchRefused(ME_ENDPOINT, 0);
  const params = new URLSearchParams({ toAgent: id, limit: "50" });
  const data = (await getJson(`${NOTIFICATIONS_ENDPOINT}?${params}`, signal)) as { notifications?: unknown } | null;
  const rows = Array.isArray(data?.notifications) ? data.notifications : [];
  return rows
    .filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === "object" && Boolean(text((row as Record<string, unknown>).id)))
    .map((row) => ({
      id: text(row.id),
      fromAgent: text(row.fromAgent),
      notificationType: text(row.notificationType),
      subject: text(row.subject),
      body: text(row.body),
      metadata:
        row.metadata && typeof row.metadata === "object" && !Array.isArray(row.metadata)
          ? (row.metadata as Record<string, unknown>)
          : {},
      readAt: text(row.readAt) || null,
      createdAt: text(row.createdAt) || null,
    }));
}

/** True when the bridge took it. */
export async function markNotificationRead(id: string): Promise<boolean> {
  try {
    const res = await fetch(`${NOTIFICATIONS_ENDPOINT}/${encodeURIComponent(id)}/read`, { method: "POST" });
    return res.ok;
  } catch {
    return false;
  }
}

/** The workspace a note is about (`metadata.room_id`), or null. */
export function roomOfNotification(note: Pick<InboxNotification, "metadata">): string | null {
  const id = note.metadata.room_id ?? note.metadata.roomId;
  return isRoomId(id) ? id : null;
}
