"use client";

/**
 * One EventSource per tab, shared by every useEventStream subscriber.
 *
 * Subscribers register the streams they want; the hub keeps a single
 * connection to /api/events/stream for the union of them, reopening it only
 * when that union changes (not when a caller re-renders). Lost connections
 * reconnect with exponential backoff.
 */

export interface EventStreamSubscriber {
  streams: readonly string[];
  onMessage: (data: string) => void;
  onOpen: () => void;
  onClose: () => void;
  onSync: () => void;
}

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30_000;

const subscribers = new Set<EventStreamSubscriber>();
let source: EventSource | null = null;
let sourceStreams: string[] = [];
let isOpen = false;
let isSynced = false;
let attempt = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
let reconcileQueued = false;

/** The union of subscribed streams, in first-subscribed order. */
function unionStreams(): string[] {
  const out: string[] = [];
  for (const sub of subscribers) {
    for (const s of sub.streams) if (!out.includes(s)) out.push(s);
  }
  return out;
}

/** Order-independent identity of a set of streams. */
export function streamsKey(streams: readonly string[]): string {
  return [...new Set(streams)].sort().join(",");
}

/** Whether the open connection already carries every stream in `streams`. */
function covers(streams: readonly string[]): boolean {
  return source !== null && streams.every((s) => sourceStreams.includes(s));
}

function teardown() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = undefined;
  if (source) source.close();
  source = null;
  sourceStreams = [];
  if (isOpen) for (const sub of subscribers) sub.onClose();
  isOpen = false;
  isSynced = false;
}

function open(streams: string[]) {
  const es = new EventSource(`/api/events/stream?streams=${streams.join(",")}`);
  source = es;
  sourceStreams = streams;

  es.addEventListener("message", (e) => {
    const data = (e as MessageEvent).data as string;
    for (const sub of subscribers) sub.onMessage(data);
  });
  es.addEventListener("sync", () => {
    isSynced = true;
    for (const sub of subscribers) sub.onSync();
  });
  es.onopen = () => {
    if (source !== es) return;
    attempt = 0;
    isOpen = true;
    for (const sub of subscribers) sub.onOpen();
  };
  es.onerror = () => {
    if (source !== es) return;
    teardown();
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS);
    attempt++;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = undefined;
      reconcile();
    }, delay);
  };
}

function reconcile() {
  const streams = unionStreams();
  if (streams.length === 0) {
    teardown();
    attempt = 0;
    return;
  }
  if (reconnectTimer) return; // backing off; the timer reopens
  // A connection that already carries every wanted stream is kept, even when
  // the union shrinks: reopening would only replay recent events again.
  if (covers(streams)) return;
  teardown();
  open(streams);
}

function queueReconcile() {
  if (reconcileQueued) return;
  reconcileQueued = true;
  queueMicrotask(() => {
    reconcileQueued = false;
    reconcile();
  });
}

export function subscribe(sub: EventStreamSubscriber): () => void {
  subscribers.add(sub);
  if (covers(sub.streams)) {
    // Joining a connection that is already up: report its state.
    if (isOpen) sub.onOpen();
    if (isSynced) sub.onSync();
  } else {
    queueReconcile();
  }
  return () => {
    subscribers.delete(sub);
    if (subscribers.size === 0) {
      teardown();
      attempt = 0;
    }
  };
}
