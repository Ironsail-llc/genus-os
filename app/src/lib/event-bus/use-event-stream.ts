"use client";

import { useEffect, useRef, useState, useCallback } from "react";

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

interface UseEventStreamOptions {
  streams?: string[];
  maxEvents?: number;
  enabled?: boolean;
}

/**
 * React hook for consuming the event bus SSE stream.
 *
 * Returns live events from Redis Streams via /api/events/stream.
 * Automatically reconnects on connection loss.
 */
export function useEventStream(options: UseEventStreamOptions = {}) {
  const {
    streams = ["email", "crm", "health", "agent"],
    maxEvents = 100,
    enabled = true,
  } = options;

  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [synced, setSynced] = useState(false);
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  const connect = useCallback(() => {
    if (!enabled) return;

    const streamsParam = streams.join(",");
    const es = new EventSource(`/api/events/stream?streams=${streamsParam}`);
    eventSourceRef.current = es;

    es.addEventListener("message", (e) => {
      try {
        const event: StreamEvent = JSON.parse(e.data);
        setEvents((prev) => {
          const next = [event, ...prev];
          return next.slice(0, maxEvents);
        });
      } catch {
        // Invalid JSON — skip
      }
    });

    es.addEventListener("sync", () => {
      setSynced(true);
    });

    es.addEventListener("heartbeat", () => {
      // Connection alive
    });

    es.addEventListener("error", () => {
      // Will auto-reconnect
    });

    es.onopen = () => {
      setConnected(true);
    };

    es.onerror = () => {
      setConnected(false);
      es.close();
      // Reconnect after 3 seconds
      reconnectTimeoutRef.current = setTimeout(connect, 3000);
    };
  }, [enabled, streams, maxEvents]);

  useEffect(() => {
    connect();
    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
      }
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
    };
  }, [connect]);

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
