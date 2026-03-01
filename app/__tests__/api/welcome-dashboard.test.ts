import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock config module
vi.mock("@/lib/config", () => ({
  HELM_AGENT_ID: "helm-user",
  OWNER_NAME: "there",
  AI_NAME: "Robothor",
  SESSION_KEY: "agent:main:webchat-user",
}));

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

vi.mock("@/lib/dashboard/welcome-context", () => ({
  fetchWelcomeContext: vi.fn().mockResolvedValue({
    hour: 9,
    health: { status: "ok", services: [] },
    inbox: { openCount: 0, unreadCount: 0 },
  }),
}));

import { POST } from "@/app/api/dashboard/welcome/route";

describe("POST /api/dashboard/welcome", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns JSON with html and type on success", async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"choices":[{"delta":{"content":"<div class=\\"glass\\">Welcome</div>"}}]}\n\n'
          )
        );
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
        controller.close();
      },
    });

    mockFetch.mockResolvedValue({ ok: true, body: stream });

    const res = await POST();
    expect(res.status).toBe(200);

    const text = await res.text();
    const body = JSON.parse(text.trim());
    expect(body.html).toContain("Welcome");
    expect(body.type).toBeTruthy();
  });

  it("returns error JSON on OpenRouter failure", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 500,
      text: () => Promise.resolve("Server error"),
    });

    const res = await POST();
    expect(res.status).toBe(200);

    const text = await res.text();
    const body = JSON.parse(text.trim());
    expect(body.error).toBe("Dashboard service temporarily unavailable");
    expect(body.error).not.toContain("OpenRouter");
  });

  it("returns error JSON if generated code fails validation", async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"choices":[{"delta":{"content":"eval(\\"alert(1)\\")"}}]}\n\n'
          )
        );
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
        controller.close();
      },
    });

    mockFetch.mockResolvedValue({ ok: true, body: stream });

    const res = await POST();
    expect(res.status).toBe(200);

    const text = await res.text();
    const body = JSON.parse(text.trim());
    expect(body.error).toBe("Generated dashboard failed quality check");
  });

  it("returns error JSON on unexpected error with sanitized message", async () => {
    mockFetch.mockRejectedValue(new Error("Network timeout"));

    const res = await POST();
    expect(res.status).toBe(200);

    const text = await res.text();
    const body = JSON.parse(text.trim());
    expect(body.error).toBe("Dashboard generation failed");
    expect(body.error).not.toContain("Network timeout");
  });
});
