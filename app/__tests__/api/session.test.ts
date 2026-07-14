/**
 * Tests for the Session Persistence API (/api/session).
 * Phase 4: Interactive Helm.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import type { NextRequest } from "next/server";

// Mock config module
vi.mock("@/lib/config", () => ({
  HELM_AGENT_ID: "helm-user",
  OWNER_NAME: "there",
  AI_NAME: "Robothor",
  SESSION_KEY: "agent:main:webchat-user",
}));
vi.mock("@/lib/bridge-auth", () => ({
  bridgeAuthHeaders: async () => ({ Authorization: "Bearer test-bridge-token" }),
}));

// Mock fetch globally
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

// Import after mocking
const { GET, POST } = await import("@/app/api/session/route");

function makeRequest(body: unknown) {
  return {
    json: () => Promise.resolve(body),
  } as unknown as NextRequest;
}

describe("GET /api/session", () => {
  beforeEach(() => {
    mockFetch.mockReset();
  });

  it("returns saved HTML from memory block", async () => {
    const state = JSON.stringify({ html: "<div>Dashboard</div>", savedAt: "2026-02-22T12:00:00Z" });
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ content: state }),
    });

    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.html).toBe("<div>Dashboard</div>");
    expect(body.savedAt).toBe("2026-02-22T12:00:00Z");
  });

  it("returns null html when block not found (404)", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: () => Promise.resolve({ error: "Not found" }),
    });

    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.html).toBeNull();
  });

  it("returns null html when content is empty", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ content: null }),
    });

    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.html).toBeNull();
  });

  it("handles raw HTML content (backward compat)", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ content: "<div>raw</div>" }),
    });

    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.html).toBe("<div>raw</div>");
  });

  it("handles bridge errors", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: () => Promise.resolve({ error: "DB down" }),
    });

    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(502);
    expect(body.error).toBe("Session backend unavailable");
  });

  it("handles fetch exceptions", async () => {
    mockFetch.mockRejectedValueOnce(new Error("Connection refused"));

    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(502);
    expect(body.error).toBe("Session backend unavailable");
    expect(body.error).not.toContain("Connection refused");
  });
});

describe("POST /api/session", () => {
  beforeEach(() => {
    mockFetch.mockReset();
  });

  it("saves dashboard HTML to memory block", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ success: true }),
    });

    const res = await POST(makeRequest({ html: "<div>Saved</div>" }));
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.success).toBe(true);

    // Check the fetch call
    expect(mockFetch).toHaveBeenCalledOnce();
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/memory-blocks/helm_state");
    expect(opts.method).toBe("PUT");
    expect(opts.headers.Authorization).toBe("Bearer test-bridge-token");

    const sentBody = JSON.parse(opts.body);
    const state = JSON.parse(sentBody.content);
    expect(state.html).toBe("<div>Saved</div>");
    expect(state.savedAt).toBeDefined();
  });

  it("rejects missing html field", async () => {
    const res = await POST(makeRequest({}));
    const body = await res.json();

    expect(res.status).toBe(400);
    expect(body.error).toContain("Missing 'html'");
  });

  it("rejects non-string html field", async () => {
    const res = await POST(makeRequest({ html: 42 }));
    const body = await res.json();

    expect(res.status).toBe(400);
    expect(body.error).toContain("Missing 'html'");
  });

  it("rejects oversized dashboard (>100KB)", async () => {
    const bigHtml = "x".repeat(100_001);
    const res = await POST(makeRequest({ html: bigHtml }));
    const body = await res.json();

    expect(res.status).toBe(413);
    expect(body.error).toContain("too large");
  });

  it("does not forward bridge error details", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      text: () => Promise.resolve("Internal Server Error"),
    });

    const res = await POST(makeRequest({ html: "<div>Test</div>" }));
    const body = await res.json();

    expect(res.status).toBe(502);
    expect(body.error).toBe("Session backend unavailable");
    expect(body.error).not.toContain("Internal Server Error");
  });
});
