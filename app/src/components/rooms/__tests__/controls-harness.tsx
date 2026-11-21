/**
 * Shared set-up for the workspace-controls tests: a stand-in bridge that
 * answers by method and path (and records what it was sent), a query client
 * per test, and a slot context like the one `RoomView` hands its slots.
 */
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { vi } from "vitest";

import type { RoomSlotContext } from "@/components/rooms/room-slots";
import { can } from "@/lib/rooms/access";
import { EDITIONS, reply, room } from "@/lib/rooms/__tests__/fixtures";
import type { Access, Room } from "@/lib/rooms/types";

type Handler = (body: unknown, url: string) => Response | Promise<Response>;

export interface SentRequest {
  method: string;
  url: string;
  body: unknown;
}

/** A fetch stub. `on("PATCH", "/rooms/<id>", fn)`; paths are after `/api/bridge/api`. */
export function mockBridge() {
  const routes: Array<{ method: string; path: string; handler: Handler }> = [];
  const sent: SentRequest[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    sent.push({ method, url, body });
    const path = url.replace(/^\/api\/bridge\/api/, "");
    // Last registered wins, so a test can override a default.
    for (let i = routes.length - 1; i >= 0; i--) {
      const route = routes[i];
      if (route.method === method && route.path === path) return route.handler(body, url);
    }
    return reply({ detail: "Not found" }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    fetchMock,
    sent,
    on(method: string, path: string, handler: Handler | unknown) {
      routes.push({
        method,
        path,
        handler: typeof handler === "function" ? (handler as Handler) : () => reply(handler),
      });
    },
    /** Requests sent with this method to this path (after `/api/bridge/api`). */
    calls(method: string, path: string) {
      return sent.filter((r) => r.method === method && r.url === `/api/bridge/api${path}`);
    },
  };
}

export function withClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
  return {
    ...utils,
    client,
    /** Re-render under the same client, so the tree (and its state) is kept. */
    rerender: (next: ReactNode) => utils.rerender(<QueryClientProvider client={client}>{next}</QueryClientProvider>),
  };
}

export function slotCtx(overrides: Partial<Room> = {}, extra: Partial<RoomSlotContext> = {}): RoomSlotContext {
  const r = room(overrides);
  const access: Access = r.access;
  return {
    room: r,
    access,
    can: (action) => can(access, action),
    edition: EDITIONS[0],
    editions: EDITIONS,
    showEdition: vi.fn(),
    refresh: vi.fn(),
    ...extra,
  };
}
