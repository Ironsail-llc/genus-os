import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock config module
vi.mock("@/lib/config", () => ({
  HELM_AGENT_ID: "helm-user",
  OWNER_NAME: "there",
  AI_NAME: "Robothor",
  SESSION_KEY: "agent:main:webchat-user",
}));

// Mock the engine client module
const mockChatSend = vi.fn();
const mockChatInject = vi.fn();
vi.mock("@/lib/engine/server-client", () => ({
  getEngineClient: () => ({
    chatSend: mockChatSend,
    chatInject: mockChatInject,
  }),
}));

import { POST } from "@/app/api/chat/send/route";

/** Helper: create a Response with SSE body from events */
function makeSseResponse(events: Array<{ event: string; data: Record<string, unknown> }>, status = 200): Response {
  const encoder = new TextEncoder();
  const body = events
    .map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`)
    .join("");
  return new Response(encoder.encode(body), {
    status,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("POST /api/chat/send", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockChatInject.mockResolvedValue({ ok: true });
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
    const engineRes = makeSseResponse([
      { event: "delta", data: { text: "Hello" } },
      { event: "delta", data: { text: " Philip" } },
      { event: "done", data: { text: "Hello Philip" } },
    ]);
    mockChatSend.mockResolvedValue(engineRes);

    const req = new Request("http://localhost:3004/api/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "hi" }),
    });

    const res = await POST(req);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("text/event-stream");

    const text = await res.text();
    expect(text).toContain("event: delta");
    expect(text).toContain("event: done");
    expect(text).toContain("Hello");
  });

  it("intercepts DASHBOARD markers and emits as separate events", async () => {
    const engineRes = makeSseResponse([
      { event: "delta", data: { text: 'Here are your contacts. [DASHBOARD:{"intent":"contacts"}] Let me know.' } },
      { event: "done", data: { text: 'Here are your contacts. [DASHBOARD:{"intent":"contacts"}] Let me know.' } },
    ]);
    mockChatSend.mockResolvedValue(engineRes);

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

  it("returns 502 when engine is unreachable", async () => {
    mockChatSend.mockRejectedValue(new Error("Connection refused"));

    const req = new Request("http://localhost:3004/api/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "hi" }),
    });

    const res = await POST(req);
    expect(res.status).toBe(502);
  });

  it("returns 409 when session is busy", async () => {
    const busyRes = new Response(JSON.stringify({ error: "Session is busy" }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    });
    mockChatSend.mockResolvedValue(busyRes);

    const req = new Request("http://localhost:3004/api/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "hi" }),
    });

    const res = await POST(req);
    expect(res.status).toBe(409);
  });
});
