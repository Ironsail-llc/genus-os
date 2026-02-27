import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock config module
vi.mock("@/lib/config", () => ({
  HELM_AGENT_ID: "helm-user",
  OWNER_NAME: "there",
  AI_NAME: "Robothor",
  SESSION_KEY: "agent:main:webchat-user",
}));

// Mock the engine client module
const mockChatHistory = vi.fn();
const mockChatInject = vi.fn();
vi.mock("@/lib/gateway/server-client", () => ({
  getEngineClient: () => ({
    chatHistory: mockChatHistory,
    chatInject: mockChatInject,
  }),
}));

import { GET } from "@/app/api/chat/history/route";

describe("GET /api/chat/history", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockChatInject.mockResolvedValue({ ok: true });
  });

  it("returns messages from engine", async () => {
    const messages = [
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ];
    mockChatHistory.mockResolvedValue({
      sessionKey: "agent:main:webchat-user",
      messages,
    });

    const req = new Request("http://localhost:3004/api/chat/history");
    const res = await GET(req);
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.messages).toEqual(messages);
    expect(body.sessionKey).toBe("agent:main:webchat-user");
  });

  it("returns 502 when engine is unreachable", async () => {
    mockChatHistory.mockRejectedValue(new Error("Connection refused"));

    const req = new Request("http://localhost:3004/api/chat/history");
    const res = await GET(req);

    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.messages).toEqual([]);
  });

  it("passes limit parameter", async () => {
    mockChatHistory.mockResolvedValue({ sessionKey: "s", messages: [] });

    const req = new Request("http://localhost:3004/api/chat/history?limit=10");
    await GET(req);

    expect(mockChatHistory).toHaveBeenCalledWith(
      "agent:main:webchat-user",
      10
    );
  });
});
