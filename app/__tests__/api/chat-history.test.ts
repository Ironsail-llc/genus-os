import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock the gateway client module
const mockChatHistory = vi.fn();
const mockEnsureConnected = vi.fn();
vi.mock("@/lib/gateway/server-client", () => ({
  getGatewayClient: () => ({
    chatHistory: mockChatHistory,
    ensureConnected: mockEnsureConnected,
  }),
}));

import { GET } from "@/app/api/chat/history/route";

describe("GET /api/chat/history", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockEnsureConnected.mockResolvedValue(undefined);
  });

  it("returns messages from gateway", async () => {
    const messages = [
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ];
    mockChatHistory.mockResolvedValue({
      sessionKey: "agent:main:webchat-philip",
      messages,
    });

    const req = new Request("http://localhost:3004/api/chat/history");
    const res = await GET(req);
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.messages).toEqual(messages);
    expect(body.sessionKey).toBe("agent:main:webchat-philip");
  });

  it("returns 502 when gateway is unreachable", async () => {
    mockEnsureConnected.mockRejectedValue(new Error("Connection refused"));

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
      "agent:main:webchat-philip",
      10
    );
  });
});
