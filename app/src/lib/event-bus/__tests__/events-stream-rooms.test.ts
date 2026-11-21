/**
 * The events stream route and room access: a same-tenant `room.*` event for a
 * room this caller cannot access never reaches the response, on the replay or
 * on the live read. The caller's rooms come from the bridge with the caller's
 * own token; when that list cannot be read, no room event is sent at all.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

vi.mock("@/lib/event-bus/redis-client", async () => {
  const actual = await vi.importActual<typeof import("@/lib/event-bus/redis-client")>("@/lib/event-bus/redis-client");
  return { isValidStream: actual.isValidStream, readRecent: vi.fn(), readSince: vi.fn(), openStreamReader: vi.fn() };
});
vi.mock("@/lib/auth", () => ({ auth: vi.fn(async () => ({ bridgeAccess: "tok", tenantId: "test-tenant" })) }));
vi.mock("@/lib/bridge-auth", () => ({ bridgeAuthHeaders: vi.fn(async () => ({ Authorization: "Bearer caller-token" })) }));

import { GET } from "@/app/api/events/stream/route";
import * as redis from "@/lib/event-bus/redis-client";

const mocked = redis as unknown as {
  readRecent: ReturnType<typeof vi.fn>;
  openStreamReader: ReturnType<typeof vi.fn>;
};

const MINE = "11111111-1111-4111-8111-111111111111";
const NOT_MINE = "22222222-2222-4222-8222-222222222222";

function roomEvent(id: string, roomId: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    timestamp: "t",
    type: "room.message",
    source: "engine",
    actor: "main",
    payload: { room_id: roomId, tenant_id: "test-tenant", message_id: 1, ...extra },
    correlation_id: "",
    tenant_id: "test-tenant",
  };
}

async function readUntil(res: Response, marker: string) {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let text = "";
  while (!text.includes(marker)) {
    const { value, done } = await reader.read();
    if (done) break;
    text += decoder.decode(value);
  }
  return { reader, text };
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  mocked.openStreamReader.mockImplementation(() => ({
    readSince: vi.fn(() => new Promise((resolve) => setTimeout(() => resolve([]), 5))),
    close: vi.fn(async () => {}),
  }));
  fetchMock = vi.fn(async () => Response.json({ rooms: [{ id: MINE }] }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => vi.unstubAllGlobals());

describe("GET /api/events/stream and room access", () => {
  it("never sends a same-tenant room event for a room the caller cannot access", async () => {
    mocked.readRecent.mockResolvedValue([roomEvent("2-0", NOT_MINE), roomEvent("1-0", MINE, { body: "the text" })]);
    const res = await GET(new NextRequest("http://localhost/api/events/stream?streams=agent"));
    const { reader, text } = await readUntil(res, "event: sync");
    await reader.cancel();
    expect(text).toContain(MINE);
    expect(text).not.toContain(NOT_MINE);
    expect(text).not.toContain("the text");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/rooms$/);
    expect(init.headers).toMatchObject({ Authorization: "Bearer caller-token" });
  });

  it("filters the live read the same way", async () => {
    mocked.readRecent.mockResolvedValue([]);
    mocked.openStreamReader.mockImplementation(() => {
      const readSince = vi.fn(() => new Promise((resolve) => setTimeout(() => resolve([]), 5)));
      readSince.mockResolvedValueOnce([
        { stream: "agent", events: [roomEvent("3-0", NOT_MINE), roomEvent("4-0", MINE)] },
      ] as never);
      return { readSince, close: vi.fn(async () => {}) };
    });
    const res = await GET(new NextRequest("http://localhost/api/events/stream?streams=agent"));
    const { reader, text } = await readUntil(res, '"id":"4-0"');
    await reader.cancel();
    expect(text).not.toContain('"id":"3-0"');
  });

  it("sends no room event when the caller's rooms cannot be read", async () => {
    fetchMock.mockImplementation(async () => new Response("no", { status: 502 }));
    mocked.readRecent.mockResolvedValue([roomEvent("1-0", MINE)]);
    const res = await GET(new NextRequest("http://localhost/api/events/stream?streams=agent"));
    const { reader, text } = await readUntil(res, "event: sync");
    await reader.cancel();
    expect(text).not.toContain(MINE);
  });

  it("sends a room's events to an admin who can open it without being a member", async () => {
    fetchMock.mockImplementation(async (url: string) =>
      String(url).endsWith(`/api/rooms/${NOT_MINE}`) ? Response.json({ id: NOT_MINE }) : Response.json({ rooms: [{ id: MINE }] }),
    );
    mocked.readRecent.mockResolvedValue([roomEvent("2-0", NOT_MINE)]);
    const res = await GET(new NextRequest("http://localhost/api/events/stream?streams=agent"));
    const { reader, text } = await readUntil(res, "event: sync");
    await reader.cancel();
    expect(text).toContain(NOT_MINE);
    const check = fetchMock.mock.calls.find(([url]) => String(url).endsWith(`/api/rooms/${NOT_MINE}`));
    expect(check?.[1].headers).toMatchObject({ Authorization: "Bearer caller-token" });
  });
});
