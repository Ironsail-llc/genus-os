import { describe, it, expect, vi, beforeEach } from "vitest";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

import { POST } from "@/app/api/dashboard/generate/route";

describe("POST /api/dashboard/generate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns 400 when no intent provided (legacy path)", async () => {
    const req = new Request("http://localhost:3004/api/dashboard/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });

    const res = await POST(req);
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toContain("intent required");
  });

  it("returns buffered JSON on successful legacy generation", async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"choices":[{"delta":{"content":"<div class=\\"glass\\">Test Dashboard</div>"}}]}\n\n'
          )
        );
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
        controller.close();
      },
    });

    mockFetch.mockResolvedValue({
      ok: true,
      body: stream,
    });

    const req = new Request("http://localhost:3004/api/dashboard/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ intent: "health" }),
    });

    const res = await POST(req);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/json");

    const body = await res.json();
    expect(body.html).toContain("Test Dashboard");
    expect(body.type).toBeTruthy();
  });

  it("returns 204 for trivial conversation messages", async () => {
    // Mock triage to return shouldUpdate: false
    mockFetch.mockImplementation((url: string) => {
      if (typeof url === "string" && url.includes("openrouter.ai")) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              choices: [
                {
                  message: {
                    content: '{"shouldUpdate": false, "dataNeeds": [], "summary": ""}',
                  },
                },
              ],
            }),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({}),
      });
    });

    const req = new Request("http://localhost:3004/api/dashboard/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: [
          { role: "user", content: "thanks" },
          { role: "assistant", content: "You're welcome!" },
        ],
      }),
    });

    const res = await POST(req);
    expect(res.status).toBe(204);
  });

  it("triages then generates for substantive conversation", async () => {
    const encoder = new TextEncoder();
    const openRouterStream = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"choices":[{"delta":{"content":"<div>Health Dashboard</div>"}}]}\n\n'
          )
        );
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
        controller.close();
      },
    });

    let callCount = 0;
    mockFetch.mockImplementation((url: string) => {
      callCount++;
      if (typeof url === "string" && url.includes("openrouter.ai")) {
        if (callCount === 1) {
          // Triage call
          return Promise.resolve({
            ok: true,
            json: () =>
              Promise.resolve({
                choices: [
                  {
                    message: {
                      content:
                        '{"shouldUpdate": true, "dataNeeds": ["health"], "summary": "Service health dashboard"}',
                    },
                  },
                ],
              }),
          });
        }
        // Generate call
        return Promise.resolve({ ok: true, body: openRouterStream });
      }
      // Data fetch (health checks)
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ status: "ok" }),
      });
    });

    const req = new Request("http://localhost:3004/api/dashboard/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: [
          { role: "user", content: "How are the services running?" },
          { role: "assistant", content: "All services are healthy and operational." },
        ],
      }),
    });

    const res = await POST(req);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/json");

    const body = await res.json();
    expect(body.html).toContain("Health Dashboard");
  });

  it("returns 502 when OpenRouter fails", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 500,
      text: () => Promise.resolve("Server error"),
    });

    const req = new Request("http://localhost:3004/api/dashboard/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ intent: "health" }),
    });

    const res = await POST(req);
    expect(res.status).toBe(502);
  });
});
