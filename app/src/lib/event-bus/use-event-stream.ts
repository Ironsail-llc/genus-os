"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { streamsKey, subscribe } from "./event-stream-hub";

export interface StreamEvent {
  id: string;
  stream: string;
  timestamp: string;
  type: string;
  source: string;
  actor: string;
  payload: Record<string, unknown>;
  correlation_id: string;
}

const DEFAULT_STREAMS = ["email", "crm", "health", "agent"];

interface UseEventStreamOptions {
  streams?: string[];
  maxEvents?: number;
  enabled?: boolean;
}

/** How many event ids each subscriber remembers to drop replays. */
const SEEN_IDS_LIMIT = 1000;

/**
 * React hook for consuming the event bus SSE stream.
 *
 * Returns live events from Redis Streams via /api/events/stream. Every caller
 * in a tab shares one EventSource (see event-stream-hub.ts); the connection
 * is keyed by the set of streams, so passing a new array literal on each
 * render does not reconnect. Reconnects back off exponentially, and events the
 * server replays on a new connection are delivered once.
 */
export function useEventStream(options: UseEventStreamOptions = {}) {
  const {
    streams = DEFAULT_STREAMS,
    maxEvents = 100,
    enabled = true,
  } = options;

  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [synced, setSynced] = useState(false);
  const maxEventsRef = useRef(maxEvents);
  const streamsRef = useRef(streams);
  const seenRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    maxEventsRef.current = maxEvents;
    streamsRef.current = streams;
  });

  // Stable across renders for the same set of streams, whatever the order.
  const key = streamsKey(streams);

  useEffect(() => {
    if (!enabled || !key) return;
    // The caller's order, for the request URL; `key` decides when it changes.
    const wanted = [...new Set(streamsRef.current)];
    const seen = seenRef.current;

    const unsubscribe = subscribe({
      streams: wanted,
      onMessage: (data) => {
        let event: StreamEvent;
        try {
          event = JSON.parse(data);
        } catch {
          return; // Invalid JSON — skip
        }
        if (event.stream && !wanted.includes(event.stream)) return;
        const seenId = `${event.stream}:${event.id}`;
        if (seen.has(seenId)) return;
        seen.add(seenId);
        if (seen.size > SEEN_IDS_LIMIT) {
          seen.delete(seen.values().next().value as string);
        }
        setEvents((prev) => [event, ...prev].slice(0, maxEventsRef.current));
      },
      onOpen: () => setConnected(true),
      onClose: () => setConnected(false),
      onSync: () => setSynced(true),
    });
    return () => {
      unsubscribe();
      setConnected(false);
    };
  }, [enabled, key]);

  const clearEvents = useCallback(() => {
    setEvents([]);
  }, []);

  const eventsByStream = useCallback(
    (stream: string) => events.filter((e) => e.stream === stream),
    [events]
  );

  return {
    events,
    connected,
    synced,
    clearEvents,
    eventsByStream,
    eventCount: events.length,
  };
}
