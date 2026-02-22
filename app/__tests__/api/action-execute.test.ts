/**
 * Tests for the Action Execute API (/api/actions/execute).
 * Phase 4: Interactive Helm.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock fetch globally
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

// Import after mocking
const { POST } = await import("@/app/api/actions/execute/route");

function makeRequest(body: unknown) {
  return {
    json: () => Promise.resolve(body),
    headers: new Headers({ "x-forwarded-for": "127.0.0.1" }),
    nextUrl: new URL("http://localhost/api/actions/execute"),
  } as any;
}

describe("POST /api/actions/execute", () => {
  beforeEach(() => {
    mockFetch.mockReset();
  });

  it("returns 400 for missing tool", async () => {
    const res = await POST(makeRequest({ params: {} }));
    const body = await res.json();
    expect(res.status).toBe(400);
    expect(body.error).toContain("Missing 'tool'");
  });

  it("returns 400 for unknown tool", async () => {
    const res = await POST(makeRequest({ tool: "delete_everything", params: {} }));
    const body = await res.json();
    expect(res.status).toBe(400);
    expect(body.error).toContain("Unknown tool");
  });

  it("proxies GET tool to bridge", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ people: [] }),
    });

    const res = await POST(makeRequest({ tool: "list_people", params: { limit: 5 } }));
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.success).toBe(true);
    expect(mockFetch).toHaveBeenCalledOnce();

    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/people?limit=5");
    expect(opts.headers["X-Agent-Id"]).toBe("helm-user");
  });

  it("proxies POST tool to bridge with body keys", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ id: "123" }),
    });

    const res = await POST(
      makeRequest({
        tool: "create_note",
        params: { title: "Test", body: "Content", extraField: "ignored" },
      })
    );
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.success).toBe(true);

    const [, opts] = mockFetch.mock.calls[0];
    const sent = JSON.parse(opts.body);
    expect(sent.title).toBe("Test");
    expect(sent.body).toBe("Content");
    expect(sent.extraField).toBeUndefined(); // Not in bodyKeys
  });

  it("forwards bridge errors", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 403,
      json: () =>
        Promise.resolve({ error: "Agent 'helm-user' not authorized" }),
    });

    const res = await POST(makeRequest({ tool: "list_people", params: {} }));
    const body = await res.json();

    expect(res.status).toBe(403);
    expect(body.error).toContain("not authorized");
  });

  it("handles fetch errors gracefully", async () => {
    mockFetch.mockRejectedValueOnce(new Error("Connection refused"));

    const res = await POST(makeRequest({ tool: "crm_health", params: {} }));
    const body = await res.json();

    expect(res.status).toBe(500);
    expect(body.error).toContain("Connection refused");
  });

  it("rate limits after 10 requests", async () => {
    // Use a unique IP to avoid cross-test contamination
    const ip = `rate-limit-test-${Date.now()}`;
    const request = {
      json: () => Promise.resolve({ tool: "crm_health", params: {} }),
      headers: new Headers({ "x-forwarded-for": ip }),
      nextUrl: new URL("http://localhost/api/actions/execute"),
    } as any;

    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: "ok" }),
    });

    // First 10 should succeed
    for (let i = 0; i < 10; i++) {
      const res = await POST(request);
      expect(res.status).toBe(200);
    }

    // 11th should be rate limited
    const res = await POST(request);
    expect(res.status).toBe(429);
  });
});
