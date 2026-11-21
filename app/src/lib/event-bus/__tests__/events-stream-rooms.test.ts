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
// The session at connect time. Looking it up again mid-stream would run the
// jwt callback, which past its skew rotates the single-use refresh token into
// a cookie nobody can write back any more.
const session = vi.hoisted(() => ({ current: { bridgeAccess: "caller-token", tenantId: "test-tenant" } as Record<string, unknown> }));
const authMock = vi.hoisted(() => vi.fn(async () => session.current));
const bridgeAuthHeaders = vi.hoisted(() => vi.fn(async () => ({ Authorization: "Bearer from-a-later-lookup" })));
vi.mock("@/lib/auth", () => ({ auth: authMock }));
vi.mock("@/lib/bridge-auth", () => ({ bridgeAuthHeaders }));

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

/** An unsigned JWT carrying only `exp`: the route reads its expiry, never trusts it. */
function tokenExpiringIn(seconds: number): string {
  const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64({ sub: "u-alice", exp: Math.floor(Date.now() / 1000) + seconds })}.sig`;
}

/** Read the whole body until the server closes it (or `ms` passes). */
async function readToEnd(res: Response, ms = 4000) {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let text = "";
  const deadline = Date.now() + ms;
  for (;;) {
    const next = await Promise.race([reader.read(), new Promise<null>((r) => setTimeout(() => r(null), Math.max(0, deadline - Date.now())))]);
    if (next === null) {
      await reader.cancel();
      return { text, closed: false };
    }
    if (next.done) return { text, closed: true };
    text += decoder.decode(next.value);
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  session.current = { bridgeAccess: "caller-token", tenantId: "test-tenant" };
  mocked.openStreamReader.mockImplementation(() => ({
    readSince: vi.fn(() => new Promise((resolve) => setTimeout(() => resolve([]), 5))),
    close: vi.fn(async () => {}),
  }));
  // The bridge: the caller's rooms, and 404 for a room they cannot open.
  fetchMock = vi.fn(async (url: string) =>
    String(url).endsWith("/api/rooms") ? Response.json({ rooms: [{ id: MINE }] }) : Response.json({ error: "Not found" }, { status: 404 }),
  );
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

  it("uses the token it was opened with, and never looks the session up again", async () => {
    mocked.readRecent.mockResolvedValue([roomEvent("1-0", MINE), roomEvent("2-0", NOT_MINE)]);
    const res = await GET(new NextRequest("http://localhost/api/events/stream?streams=agent"));
    const { reader } = await readUntil(res, "event: sync");
    await reader.cancel();
    expect(authMock).toHaveBeenCalledTimes(1);
    expect(bridgeAuthHeaders).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.length).toBeGreaterThan(0);
    for (const [, init] of fetchMock.mock.calls) expect(init.headers).toEqual({ Authorization: "Bearer caller-token" });
  });

  it("closes before the token it was opened with expires, so the browser reconnects with a fresh one", async () => {
    session.current = { bridgeAccess: tokenExpiringIn(20), tenantId: "test-tenant" };
    mocked.readRecent.mockResolvedValue([]);
    const res = await GET(new NextRequest("http://localhost/api/events/stream?streams=agent"));
    // Well inside the ~3 s the idle test reader takes to exhaust the loop on its own.
    const { closed } = await readToEnd(res, 1500);
    expect(closed).toBe(true);
  });

  it("stays open while the token has long to run", async () => {
    session.current = { bridgeAccess: tokenExpiringIn(900), tenantId: "test-tenant" };
    mocked.readRecent.mockResolvedValue([]);
    const res = await GET(new NextRequest("http://localhost/api/events/stream?streams=agent"));
    const { closed } = await readToEnd(res, 1500);
    expect(closed).toBe(false);
  });

  it("closes when the bridge answers 401, and a new connection gets the room's events again", async () => {
    fetchMock.mockImplementation(async () => Response.json({ error: "expired" }, { status: 401 }));
    mocked.readRecent.mockResolvedValue([roomEvent("1-0", MINE)]);
    const first = await readToEnd(await GET(new NextRequest("http://localhost/api/events/stream?streams=agent")), 1500);
    expect(first.closed).toBe(true);
    expect(first.text).not.toContain(MINE);

    // The browser reconnects through the proxy with a refreshed session.
    session.current = { bridgeAccess: "refreshed-token", tenantId: "test-tenant" };
    fetchMock.mockImplementation(async (url: string) =>
      String(url).endsWith("/api/rooms") ? Response.json({ rooms: [{ id: MINE }] }) : Response.json({}, { status: 404 }),
    );
    const res = await GET(new NextRequest("http://localhost/api/events/stream?streams=agent"));
    const { reader, text } = await readUntil(res, "event: sync");
    await reader.cancel();
    expect(text).toContain(MINE);
    expect(fetchMock.mock.calls.at(-1)?.[1].headers).toEqual({ Authorization: "Bearer refreshed-token" });
  });
});
