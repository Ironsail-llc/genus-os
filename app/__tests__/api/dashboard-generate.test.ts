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

  it("returns chunked JSON on successful legacy generation", async () => {
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

    const text = await res.text();
    const body = JSON.parse(text.trim());
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

    const text = await res.text();
    const body = JSON.parse(text.trim());
    expect(body.html).toContain("Health Dashboard");
  });

  it("uses agentData and skips satisfied data needs", async () => {
    const encoder = new TextEncoder();
    const openRouterStream = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"choices":[{"delta":{"content":"<div>Weather Dashboard</div>"}}]}\n\n'
          )
        );
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
        controller.close();
      },
    });

    const fetchCalls: string[] = [];
    let callCount = 0;
    mockFetch.mockImplementation((url: string) => {
      fetchCalls.push(url);
      callCount++;
      if (typeof url === "string" && url.includes("openrouter.ai")) {
        if (callCount === 1) {
          // Triage call — requests web search
          return Promise.resolve({
            ok: true,
            json: () =>
              Promise.resolve({
                choices: [
                  {
                    message: {
                      content:
                        '{"shouldUpdate": true, "dataNeeds": ["web:weather NYC"], "summary": "Weather dashboard"}',
                    },
                  },
                ],
              }),
          });
        }
        // Generate call
        return Promise.resolve({ ok: true, body: openRouterStream });
      }
      // Should NOT be called for web search since agentData satisfies it
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
          { role: "user", content: "What's the weather in NYC?" },
          { role: "assistant", content: "It's sunny and 72F in NYC." },
        ],
        agentData: {
          web: { query: "weather NYC", results: [{ title: "NYC Weather", snippet: "Sunny, 72F" }] },
        },
      }),
    });

    const res = await POST(req);
    expect(res.status).toBe(200);

    const text = await res.text();
    const body = JSON.parse(text.trim());
    expect(body.html).toContain("Weather Dashboard");

    // Verify no SearXNG call was made (only OpenRouter calls)
    const nonOpenRouterCalls = fetchCalls.filter(
      (url) => !url.includes("openrouter.ai")
    );
    expect(nonOpenRouterCalls).toHaveLength(0);
  });

  it("fetches unsatisfied needs when agentData is partial", async () => {
    const encoder = new TextEncoder();
    const openRouterStream = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"choices":[{"delta":{"content":"<div>Combined Dashboard</div>"}}]}\n\n'
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
          // Triage requests web + health
          return Promise.resolve({
            ok: true,
            json: () =>
              Promise.resolve({
                choices: [
                  {
                    message: {
                      content:
                        '{"shouldUpdate": true, "dataNeeds": ["web:weather", "health"], "summary": "Overview"}',
                    },
                  },
                ],
              }),
          });
        }
        return Promise.resolve({ ok: true, body: openRouterStream });
      }
      // Health endpoint calls (web is satisfied by agentData)
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
          { role: "user", content: "Weather and service health?" },
          { role: "assistant", content: "Here's the info." },
        ],
        agentData: {
          web: { query: "weather", results: [{ title: "Weather", snippet: "Sunny" }] },
        },
      }),
    });

    const res = await POST(req);
    expect(res.status).toBe(200);
  });

  it("returns error JSON when OpenRouter fails", async () => {
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
    expect(res.status).toBe(200);

    const text = await res.text();
    const body = JSON.parse(text.trim());
    expect(body.error).toBe("Dashboard service temporarily unavailable");
  });
});
