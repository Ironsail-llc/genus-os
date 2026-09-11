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

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
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
  mockAuth.mockResolvedValue({
    user: { id: "user-1" },
    bridgeAccess: "bridge-token-abc",
  });
  fetchMock = vi.fn(async () => jsonResponse({ data: { payload: [] } }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
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

    fetchMock.mockResolvedValue(jsonResponse({ results: [] }));
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
});
