import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock the gateway client module
const mockChatSend = vi.fn();
const mockChatInject = vi.fn();
const mockEnsureConnected = vi.fn();
vi.mock("@/lib/gateway/server-client", () => ({
  getGatewayClient: () => ({
    chatSend: mockChatSend,
    chatInject: mockChatInject,
    ensureConnected: mockEnsureConnected,
  }),
}));

import { POST } from "@/app/api/chat/send/route";

describe("POST /api/chat/send", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockEnsureConnected.mockResolvedValue(undefined);
    mockChatInject.mockResolvedValue({ ok: true, messageId: "test" });
  });

  it("returns 400 when no message provided", async () => {
    const req = new Request("http://localhost:3004/api/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });

    const res = await POST(req);
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toContain("message required");
  });

  it("returns SSE stream on successful send", async () => {
    // Create async iterable that yields one delta and one final
    const events = {
      async *[Symbol.asyncIterator]() {
        yield {
          runId: "r1",
          sessionKey: "agent:main:webchat-philip",
          seq: 0,
          state: "delta" as const,
          message: { role: "assistant" as const, content: "Hello" },
        };
        yield {
          runId: "r1",
          sessionKey: "agent:main:webchat-philip",
          seq: 1,
          state: "final" as const,
          message: { role: "assistant" as const, content: "Hello Philip" },
        };
      },
    };

    mockChatSend.mockResolvedValue({ runId: "r1", events });

    const req = new Request("http://localhost:3004/api/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "hi" }),
    });

    const res = await POST(req);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("text/event-stream");

    // Read the stream
    const text = await res.text();
    expect(text).toContain("event: delta");
    expect(text).toContain("event: done");
    expect(text).toContain("Hello");
  });

  it("intercepts DASHBOARD markers and emits as separate events", async () => {
    const events = {
      async *[Symbol.asyncIterator]() {
        yield {
          runId: "r1",
          sessionKey: "agent:main:webchat-philip",
          seq: 0,
          state: "delta" as const,
          message: {
            role: "assistant" as const,
            content: 'Here are your contacts. [DASHBOARD:{"intent":"contacts"}] Let me know.',
          },
        };
        yield {
          runId: "r1",
          sessionKey: "agent:main:webchat-philip",
          seq: 1,
          state: "final" as const,
          message: { role: "assistant" as const, content: "" },
        };
      },
    };

    mockChatSend.mockResolvedValue({ runId: "r1", events });

    const req = new Request("http://localhost:3004/api/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "show contacts" }),
    });

    const res = await POST(req);
    const text = await res.text();

    // Should have a clean delta with text (no marker)
    expect(text).toContain("event: delta");
    expect(text).toContain("Here are your contacts.");
    expect(text).not.toContain("[DASHBOARD:");

    // Should have a separate dashboard event
    expect(text).toContain("event: dashboard");
    expect(text).toContain('"intent":"contacts"');

    // Done event should have clean text
    expect(text).toContain("event: done");
  });

  it("returns 502 when gateway is unreachable", async () => {
    mockEnsureConnected.mockRejectedValue(new Error("Connection refused"));

    const req = new Request("http://localhost:3004/api/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "hi" }),
    });

    const res = await POST(req);
    expect(res.status).toBe(502);
  });
});
