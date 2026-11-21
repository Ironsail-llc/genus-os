/**
 * A room's outbox: the ONE place a room message waits, and the ONE sender
 * that sends it.
 *
 * - Every room message gets its `seq` when it is typed (`enqueue`) and joins
 *   the outbox in that order. Nothing reorders it afterwards.
 * - Exactly one send is in flight at a time, and it is always the head.
 * - The head leaves only when its send is settled: admitted (`ok`) or
 *   refused for good (`failed`, which the panel shows). A `busy` refusal
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
  /** Stable across panels: the bubble that shows it uses this id. */
  id: string;
  /** Typing order, assigned when it was typed. */
  seq: number;
  text: string;
  files?: AttachmentPayload[];
}

export type SendResult =
  | { outcome: "ok" }
  | { outcome: "failed" }
  | { outcome: "busy"; answering?: string };

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
}

export interface RoomOutbox {
  enqueue(text: string, files?: AttachmentPayload[]): OutboxItem;
  /** Become the sender. Returns a detach for when the panel goes away. */
  attach(sender: Sender): () => void;
  /** The room moved on (a live message): retry a refused head now. */
  roomFreed(): void;
  state(): OutboxState;
  subscribe(listener: () => void): () => void;
}

function createOutbox(roomId: string): RoomOutbox & { dispose(): void } {
  let items: OutboxItem[] = [];
  let sending: OutboxItem | null = null;
  let blocked = false;
  let answering = "";
  let seq = 0;
  let sender: Sender | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let snapshot: OutboxState = { items, sending, blocked, answering };
  const listeners = new Set<() => void>();

  function notify() {
    snapshot = { items, sending, blocked, answering };
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
    sending = head;
    notify();
    let result: SendResult;
    try {
      result = await sender(head);
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
        id: `room-${roomId.slice(0, 8)}-${Date.now()}-${seq}`,
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
    box = createOutbox(roomId);
    outboxes.set(roomId, box);
  }
  return box;
}

/** Drop every outbox (tests, and nothing else). */
export function forgetRoomOutboxes(): void {
  for (const box of outboxes.values()) box.dispose();
  outboxes.clear();
}
