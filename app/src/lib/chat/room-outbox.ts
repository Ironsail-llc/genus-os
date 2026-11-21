/**
 * A room's outbox: the ONE place a room message waits, and the ONE sender
 * that sends it.
 *
 * - Every room message gets its `seq` when it is typed (`enqueue`) and joins
 *   the outbox in that order. Nothing reorders it afterwards.
 * - Exactly one send is in flight at a time, and it is always the head.
 * - The head leaves only when its send is settled: admitted (`ok`) or
 *   refused for good (`failed`). A failure is shown by the panel that sent
 *   it; if that panel has gone (the reader left mid-send), the entry moves to
 *   `unsent` and stays there until a panel has shown it as not sent and
 *   acknowledged it. A message is never dropped without the reader being told. A `busy` refusal
 *   (the room's agent is answering somebody else) leaves it at the head. The
 *   outbox then waits `ROOM_BUSY_RETRY_MS`, or less if the room is freed
 *   sooner (`roomFreed`, on a live room message).
 * - Nothing else ever sends a room message. The panel offers no steering or
 *   joining of a running turn in a room; what is typed meanwhile waits here.
 *
 * One outbox per room, at module level, so it outlives the panel. Leaving a
 * workspace detaches the panel's sender: what is queued stays queued, a send
 * already in flight settles as usual, and the next panel to attach carries
 * on from the head. In memory, per tab, like the main conversation's pending
 * follow-ups.
 */
import type { AttachmentPayload } from "@/lib/chat/attachments";

export const ROOM_BUSY_RETRY_MS = 4000;

export interface OutboxItem {
  /**
   * The message's client id: a UUID, stable across panels. Its bubble's key,
   * and sent with the message (`client_id`) so the stored row can name it.
   */
  id: string;
  /** Typing order, assigned when it was typed. */
  seq: number;
  text: string;
  files?: AttachmentPayload[];
}

export type SendResult =
  | { outcome: "ok" }
  /**
   * `rejected`: refused before it was admitted, so certainly not sent.
   * Otherwise the outcome is unknown: it may have been admitted and stored
   * (a gateway timeout, a stream cut mid-answer), and is never resent.
   */
  | { outcome: "failed"; rejected?: boolean }
  | { outcome: "busy"; answering?: string };

/** A send that failed with no panel left to say so, and whether it was refused or its outcome is unknown. */
export type UnsentItem = OutboxItem & { rejected: boolean };

export type Sender = (item: OutboxItem) => Promise<SendResult>;

export interface OutboxState {
  /** Waiting or in flight, oldest first; the head is the next (or current) send. */
  items: readonly OutboxItem[];
  /** The head while its send is in flight. */
  sending: OutboxItem | null;
  /** The last send was refused as busy, and the retry wait is running. */
  blocked: boolean;
  /** Who the room's agent was answering when it refused. */
  answering: string;
  /** Sends that failed after the panel that offered them had gone: to be shown, then acknowledged. */
  unsent: readonly UnsentItem[];
}

export interface RoomOutbox {
  enqueue(text: string, files?: AttachmentPayload[]): OutboxItem;
  /** Become the sender. Returns a detach for when the panel goes away. */
  attach(sender: Sender): () => void;
  /** The room moved on (a live message): retry a refused head now. */
  roomFreed(): void;
  /** A panel has shown these unsent entries as not sent: forget them. */
  acknowledge(ids: readonly string[]): void;
  state(): OutboxState;
  subscribe(listener: () => void): () => void;
}

function createOutbox(): RoomOutbox & { dispose(): void } {
  let items: OutboxItem[] = [];
  let sending: OutboxItem | null = null;
  let blocked = false;
  let answering = "";
  let unsent: UnsentItem[] = [];
  let seq = 0;
  let sender: Sender | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let snapshot: OutboxState = { items, sending, blocked, answering, unsent };
  const listeners = new Set<() => void>();

  function notify() {
    snapshot = { items, sending, blocked, answering, unsent };
    for (const listener of [...listeners]) listener();
  }

  function unblock() {
    if (timer) clearTimeout(timer);
    timer = undefined;
    blocked = false;
  }

  async function drain() {
    if (!sender || sending || blocked || !items.length) return;
    const head = items[0];
    const by = sender;
    sending = head;
    notify();
    let result: SendResult;
    try {
      result = await by(head);
    } catch {
      result = { outcome: "failed" };
    }
    sending = null;
    if (result.outcome === "busy") {
      blocked = true;
      answering = result.answering ?? "";
      timer = setTimeout(() => {
        unblock();
        notify();
        void drain();
      }, ROOM_BUSY_RETRY_MS);
    } else {
      items = items.filter((item) => item.id !== head.id);
      // Failed with nobody left to say so: kept until a panel shows it.
      if (result.outcome === "failed" && sender !== by) unsent = [...unsent, { ...head, rejected: result.rejected === true }];
      unblock();
      answering = "";
    }
    notify();
    void drain();
  }

  return {
    enqueue(text, files) {
      seq += 1;
      const item: OutboxItem = {
        id: crypto.randomUUID(),
        seq,
        text,
        ...(files?.length ? { files } : {}),
      };
      items = [...items, item];
      notify();
      void drain();
      return item;
    },
    attach(next) {
      sender = next;
      void drain();
      return () => {
        if (sender === next) sender = null;
      };
    },
    roomFreed() {
      if (!blocked) return;
      unblock();
      notify();
      void drain();
    },
    acknowledge(ids) {
      const shown = new Set(ids);
      if (!unsent.some((item) => shown.has(item.id))) return;
      unsent = unsent.filter((item) => !shown.has(item.id));
      notify();
    },
    state: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dispose() {
      if (timer) clearTimeout(timer);
      listeners.clear();
      sender = null;
    },
  };
}

const outboxes = new Map<string, ReturnType<typeof createOutbox>>();

/** The outbox for `roomId`, created on first use. */
export function roomOutbox(roomId: string): RoomOutbox {
  let box = outboxes.get(roomId);
  if (!box) {
    box = createOutbox();
    outboxes.set(roomId, box);
  }
  return box;
}

/** Drop every outbox (tests, and nothing else). */
export function forgetRoomOutboxes(): void {
  for (const box of outboxes.values()) box.dispose();
  outboxes.clear();
}

/**
 * One row of a room's transcript as the panel draws it: a stored row from
 * history (`serverId`, and `clientId` when the engine names the outbox entry
 * it came from), or a local bubble keyed by its outbox entry's client id.
 */
export interface RoomRow {
  id: string;
  role: "user" | "assistant";
  content: string;
  serverId?: number;
  clientId?: string;
  recovery?: unknown;
  note?: string;
  attachments?: unknown[];
}

function isLocal(row: RoomRow): boolean {
  return row.serverId === undefined && !row.recovery;
}

/**
 * Stored rows merged into what is drawn. A stored row REPLACES the local
 * bubble it is the stored copy of, in place, and is never added beside it:
 * matched by client id when the engine names one, and only when it does not,
 * by role and text against a local bubble. A replaced bubble's client id is
 * kept on the stored row, so the outbox entry it came from is never drawn
 * again. Rows already drawn are left alone.
 */
export function mergeRoomRows<T extends RoomRow>(prev: T[], stored: readonly T[]): T[] {
  const shown = new Set(prev.map((m) => m.id));
  const next = [...prev];
  const added: T[] = [];
  for (const row of stored) {
    if (shown.has(row.id)) continue;
    shown.add(row.id);
    const at = row.clientId
      ? next.findIndex((m) => m.id === row.clientId)
      : next.findIndex((m) => isLocal(m) && m.role === row.role && m.content.trim() === row.content.trim());
    if (at < 0) {
      added.push(row);
      continue;
    }
    const was = next[at];
    next[at] = {
      ...was,
      ...row,
      clientId: row.clientId ?? (isLocal(was) ? was.id : undefined),
      note: undefined,
      attachments: was.attachments ?? row.attachments,
    };
  }
  return added.length || next.some((m, i) => m !== prev[i]) ? [...next, ...added] : prev;
}

/**
 * The outbox entries that need a bubble: not drawn yet (by id, or as a stored
 * row naming them), and not a send that ANOTHER panel has in flight — that one
 * was either admitted, and its stored copy arrives with history, or it is
 * refused, and it is drawn as queued once it is no longer in flight.
 */
export function pendingBubbles(
  state: OutboxState,
  shown: ReadonlyArray<Pick<RoomRow, "id" | "clientId">>,
  startedHere: ReadonlySet<string>,
): OutboxItem[] {
  const ids = new Set(shown.map((r) => r.id));
  const named = new Set(shown.map((r) => r.clientId).filter((id): id is string => Boolean(id)));
  return state.items.filter(
    (item) => !ids.has(item.id) && !named.has(item.id) && !(state.sending?.id === item.id && !startedHere.has(item.id)),
  );
}

/**
 * The unsent entries that need a bubble: not drawn yet, and not a message
 * history already holds — a stored row naming it by client id or, where the
 * engine names none, a stored row with its text. A send whose answer was lost
 * after it was stored is that stored row, never a second copy.
 */
export function unsentBubbles(
  state: OutboxState,
  shown: ReadonlyArray<Pick<RoomRow, "id" | "clientId" | "serverId" | "role" | "content">>,
): UnsentItem[] {
  return state.unsent.filter(
    (item) =>
      !shown.some(
        (r) =>
          r.id === item.id ||
          r.clientId === item.id ||
          (r.serverId !== undefined && !r.clientId && r.role === "user" && r.content.trim() === item.text.trim()),
      ),
  );
}
