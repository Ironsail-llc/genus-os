import { describe, it, expect, vi, beforeEach } from "vitest";

/**
 * The history route pages: it forwards the engine's cursor (`before`, a stored
 * row id) and `offset`, and says whether there is more. Without either it asks
 * the engine exactly what it always asked.
 */

const engine = { chatHistory: vi.fn() };
vi.mock("@/lib/engine/server-client", () => ({ getEngineClient: () => engine }));
vi.mock("@/lib/engine/session-state", () => ({
  ensureCanvasPromptInjected: () => Promise.resolve(),
}));
vi.mock("@/lib/chat/agent-guard", () => ({
  resolveChatAgent: async (agent: unknown) =>
    agent ? { ok: true, key: `agent:${String(agent)}:primary` } : { ok: true, key: "" },
}));

async function GET(url: string) {
  const { GET: handler } = await import("../history/route");
  return handler(new Request(url));
}

describe("GET /api/chat/history paging", () => {
  beforeEach(() => {
    engine.chatHistory.mockReset();
    engine.chatHistory.mockResolvedValue({ messages: [], sessionKey: "" });
  });

  it("forwards the cursor and the limit", async () => {
    await GET("http://helm.test/api/chat/history?limit=20&before=51&agent=scheduler");
    expect(engine.chatHistory).toHaveBeenCalledWith(20, "agent:scheduler:primary", { before: 51 });
  });

  it("forwards an offset", async () => {
    await GET("http://helm.test/api/chat/history?offset=100");
    expect(engine.chatHistory).toHaveBeenCalledWith(50, "", { offset: 100 });
  });

  it("ignores a cursor or offset that is not a positive whole number", async () => {
    await GET("http://helm.test/api/chat/history?before=abc&offset=-3");
    expect(engine.chatHistory).toHaveBeenCalledWith(50, "");
  });

  it("passes the engine's hasMore through, and says false when the engine is silent", async () => {
    engine.chatHistory.mockResolvedValueOnce({ messages: [{ role: "user", content: "x" }], hasMore: true });
    const more = await (await GET("http://helm.test/api/chat/history")).json();
    expect(more.hasMore).toBe(true);
    expect(more.messages).toHaveLength(1);

    const silent = await (await GET("http://helm.test/api/chat/history")).json();
    expect(silent.hasMore).toBe(false);
  });
});
