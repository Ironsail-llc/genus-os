import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock config module
vi.mock("@/lib/config", () => ({
  HELM_AGENT_ID: "helm-user",
  OWNER_NAME: "there",
  AI_NAME: "Robothor",
  SESSION_KEY: "agent:main:webchat-user",
}));

const { mockDashboardCompletion } = vi.hoisted(() => ({
  mockDashboardCompletion: vi.fn(),
}));

vi.mock("@/lib/engine/server-client", () => ({
  getEngineClient: () => ({ dashboardCompletion: mockDashboardCompletion }),
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
    mockDashboardCompletion.mockResolvedValue(
      '<div class="glass">Test Dashboard</div>',
    );

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
    expect(mockDashboardCompletion).toHaveBeenCalledWith(
      "render",
      expect.stringContaining("read-only Genus OS dashboard"),
      expect.stringContaining("health"),
    );
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("returns 204 for trivial conversation messages", async () => {
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
    expect(mockDashboardCompletion).not.toHaveBeenCalled();
  });

  it("triages then generates for substantive conversation", async () => {
    mockDashboardCompletion
      .mockResolvedValueOnce(
        '{"shouldUpdate": true, "dataNeeds": ["health"], "summary": "Service health dashboard"}',
      )
      .mockResolvedValueOnce("<div>Health Dashboard</div>");
    mockFetch.mockImplementation(() => {
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
    expect(mockDashboardCompletion).toHaveBeenNthCalledWith(
      1,
      "triage",
      expect.any(String),
      expect.stringContaining("How are the services running?"),
    );
    expect(mockDashboardCompletion).toHaveBeenNthCalledWith(
      2,
      "render",
      expect.any(String),
      expect.any(String),
    );
  });

  it("uses agentData and skips satisfied data needs", async () => {
    mockDashboardCompletion
      .mockResolvedValueOnce(
        '{"shouldUpdate": true, "dataNeeds": ["web:weather NYC"], "summary": "Weather dashboard"}',
      )
      .mockResolvedValueOnce("<div>Weather Dashboard</div>");
    const fetchCalls: string[] = [];
    mockFetch.mockImplementation((url: string) => {
      fetchCalls.push(url);
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

    expect(fetchCalls).not.toContainEqual(
      expect.stringMatching(/openrouter|anthropic|openai\.com/i),
    );
  });

  it("fetches unsatisfied needs when agentData is partial", async () => {
    mockDashboardCompletion
      .mockResolvedValueOnce(
        '{"shouldUpdate": true, "dataNeeds": ["web:weather", "health"], "summary": "Overview"}',
      )
      .mockResolvedValueOnce("<div>Combined Dashboard</div>");
    mockFetch.mockImplementation(() => {
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

  it("returns generic error JSON when Engine completion fails", async () => {
    mockDashboardCompletion.mockRejectedValue(
      new Error("provider leaked sk-sensitive-value"),
    );

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
    expect(text).not.toContain("sk-sensitive-value");
  });
});
