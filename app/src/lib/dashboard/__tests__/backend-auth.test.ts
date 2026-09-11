import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * Server-side dashboard data fetches run inside a route handler, on behalf of
 * the signed-in operator. They must carry that caller's bridge bearer token —
 * without it the bridge answers 401 and the generated dashboard is built from
 * nothing (observed live: `GET /api/conversations 401` from the speculative
 * fetch in /api/dashboard/generate).
 */

const mockAuth = vi.fn();
vi.mock("@/lib/auth", () => ({ auth: mockAuth }));

// The welcome context also reads Redis for event-bus counts. Left real, it
// builds an ioredis client and retries against a server the runner does not
// have — which is exactly how this file passed on a box with Redis running
// and timed out in CI without one. No spec here is about Redis, so it is a
// stub, and any dependency that escapes its stub must fail, not hang.
const mockStreamLengths = vi.fn(async () => ({ agent: 1, email: 0 }));
vi.mock("@/lib/event-bus/redis-client", () => ({ streamLengths: mockStreamLengths }));

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

// Every backend path these helpers are allowed to touch. A fetch to anything
// else is a dependency that escaped the test's control: it throws immediately
// and is reported after the spec, instead of reaching a real network (or
// hanging until the runner's timeout).
const EXPECTED_PATHS = [
  /\/health$/,
  /\/api\/conversations/,
  /\/api\/people/,
  /\/api\/companies/,
  /\/query$/,
  /\/search\?/,
];

let unexpectedFetches: string[] = [];

function guardedFetch(respond: (url: string) => Response) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (!EXPECTED_PATHS.some((pattern) => pattern.test(url))) {
      unexpectedFetches.push(url);
      throw new Error(`unexpected fetch: ${url}`);
    }
    return respond(url);
  });
}

function headersOf(call: unknown[]): Record<string, string> {
  const init = call[1] as RequestInit | undefined;
  return (init?.headers ?? {}) as Record<string, string>;
}

function callsTo(fetchMock: ReturnType<typeof vi.fn>, needle: string) {
  return fetchMock.mock.calls.filter(([input]) => String(input).includes(needle));
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  vi.resetModules();
  unexpectedFetches = [];
  mockAuth.mockResolvedValue({
    user: { id: "user-1" },
    bridgeAccess: "bridge-token-abc",
  });
  mockStreamLengths.mockResolvedValue({ agent: 1, email: 0 });
  fetchMock = guardedFetch(() => jsonResponse({ data: { payload: [] } }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  expect(unexpectedFetches).toEqual([]);
});

describe("fetchDataForNeeds", () => {
  it("sends the caller's bearer token to the bridge", async () => {
    const { fetchDataForNeeds, clearDataCache } = await import("../conversation-context");
    clearDataCache();

    await fetchDataForNeeds(["conversations"]);

    const calls = callsTo(fetchMock, "/api/conversations");
    expect(calls.length).toBeGreaterThan(0);
    expect(headersOf(calls[0]).Authorization).toBe("Bearer bridge-token-abc");
  });

  it("does not leak the bearer token to the search backend", async () => {
    const { fetchDataForNeeds, clearDataCache } = await import("../conversation-context");
    clearDataCache();

    // Keep the guard: replace the response, not the interception.
    fetchMock.mockImplementation(guardedFetch(() => jsonResponse({ results: [] })));
    await fetchDataForNeeds(["web:anything"]);

    const calls = callsTo(fetchMock, "/search");
    expect(calls.length).toBeGreaterThan(0);
    expect(headersOf(calls[0]).Authorization).toBeUndefined();
  });

  it("keeps one caller's fetched data out of another caller's dashboard", async () => {
    const { fetchDataForNeeds, clearDataCache } = await import("../conversation-context");
    clearDataCache();

    await fetchDataForNeeds(["conversations"]);
    const firstCallCount = callsTo(fetchMock, "/api/conversations").length;

    mockAuth.mockResolvedValue({
      user: { id: "user-2" },
      bridgeAccess: "bridge-token-xyz",
    });
    await fetchDataForNeeds(["conversations"]);

    const calls = callsTo(fetchMock, "/api/conversations");
    expect(calls.length).toBeGreaterThan(firstCallCount);
    expect(headersOf(calls[calls.length - 1]).Authorization).toBe("Bearer bridge-token-xyz");
  });
});

describe("the per-caller data cache", () => {
  const asCaller = (n: number) =>
    mockAuth.mockResolvedValue({ user: { id: `user-${n}` }, bridgeAccess: `token-${n}` });

  it("serves a repeat need for the same caller without asking the bridge again", async () => {
    const { fetchDataForNeeds, clearDataCache } = await import("../conversation-context");
    clearDataCache();

    asCaller(1);
    await fetchDataForNeeds(["conversations"]);
    const afterFirst = callsTo(fetchMock, "/api/conversations").length;
    await fetchDataForNeeds(["conversations"]);

    expect(callsTo(fetchMock, "/api/conversations").length).toBe(afterFirst);
  });

  it("evicts the oldest partition once the cap is reached, so it cannot grow without bound", async () => {
    const { fetchDataForNeeds, clearDataCache } = await import("../conversation-context");
    clearDataCache();

    asCaller(0);
    await fetchDataForNeeds(["conversations"]);
    const afterFirstCaller = callsTo(fetchMock, "/api/conversations").length;

    // Fill past the cap with distinct callers.
    for (let i = 1; i <= 200; i++) {
      asCaller(i);
      await fetchDataForNeeds(["conversations"]);
    }

    // The first caller's entry is gone: asking again reaches the bridge.
    asCaller(0);
    await fetchDataForNeeds(["conversations"]);

    const calls = callsTo(fetchMock, "/api/conversations");
    expect(calls.length).toBe(afterFirstCaller + 201);
    expect(headersOf(calls[calls.length - 1]).Authorization).toBe("Bearer token-0");
  });
});

describe("fetchWelcomeContext", () => {
  it("sends the caller's bearer token to the bridge", async () => {
    const { fetchWelcomeContext } = await import("../welcome-context");

    await fetchWelcomeContext();

    const calls = callsTo(fetchMock, "/api/conversations");
    expect(calls.length).toBeGreaterThan(0);
    expect(headersOf(calls[0]).Authorization).toBe("Bearer bridge-token-abc");
  });

  it("gives up on an event bus that never answers instead of holding the dashboard open", async () => {
    // Every other source here is bounded — the HTTP calls carry an
    // AbortSignal.timeout. The Redis read was not, so an unreachable Redis
    // (its client retries with backoff) held the welcome route open behind a
    // keepalive with nothing to show for it.
    mockStreamLengths.mockImplementation(() => new Promise(() => {}));
    vi.useFakeTimers();

    const { fetchWelcomeContext } = await import("../welcome-context");
    const pending = fetchWelcomeContext();
    await vi.advanceTimersByTimeAsync(5_000);
    const context = await pending;

    expect(context.eventBus).toBeNull();
    // The sources that did answer are still there.
    expect(context.inbox).not.toBeNull();
  });
});
