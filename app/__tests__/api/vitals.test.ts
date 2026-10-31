import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const mockWriteVital = vi.fn();
vi.mock("@/lib/event-bus/redis-client", () => ({
  writeVital: (...args: unknown[]) => mockWriteVital(...args),
}));

import { POST } from "@/app/api/vitals/route";

function post(body: unknown): Request {
  return new Request("http://localhost:3004/api/vitals", {
    method: "POST",
    headers: { "content-type": "text/plain" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

const LCP = { name: "LCP", value: 1234.5, rating: "good", path: "/", view: "chat", id: "v1-1", navigation_type: "navigate" };

function webVitalLines(spy: ReturnType<typeof vi.spyOn>): Record<string, unknown>[] {
  return spy.mock.calls
    .map((call: unknown[]) => {
      try {
        return JSON.parse(String(call[0]));
      } catch {
        return null;
      }
    })
    .filter((line: Record<string, unknown> | null) => line?.event === "web_vital");
}

describe("POST /api/vitals", () => {
  let logSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    delete process.env.GENUS_DASHBOARD_WEB_VITALS;
    mockWriteVital.mockResolvedValue(undefined);
    logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
  });

  afterEach(() => {
    logSpy.mockRestore();
  });

  it("writes one stream entry and logs one structured line per metric", async () => {
    const res = await POST(post(LCP));

    expect(res.status).toBe(204);
    expect(mockWriteVital).toHaveBeenCalledTimes(1);
    expect(mockWriteVital).toHaveBeenCalledWith({
      name: "LCP",
      value: 1234.5,
      rating: "good",
      path: "/",
      view: "chat",
      id: "v1-1",
      navigation_type: "navigate",
    });
    const lines = webVitalLines(logSpy);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatchObject({ event: "web_vital", name: "LCP", value: 1234.5, rating: "good", path: "/", view: "chat" });
  });

  it("never records a tenant, user or query string", async () => {
    await POST(post({ ...LCP, path: "/chat?token=secret#frag", tenant: "t1", user: "alice@example.com" }));

    const stored = mockWriteVital.mock.calls[0][0];
    expect(stored.path).toBe("/chat");
    expect(stored).not.toHaveProperty("tenant");
    expect(stored).not.toHaveProperty("user");
    const line = JSON.stringify(webVitalLines(logSpy)[0]);
    expect(line).not.toContain("secret");
    expect(line).not.toContain("alice");
    expect(line).not.toContain("t1");
  });

  it.each([
    ["an unknown metric", { ...LCP, name: "BOGUS" }],
    ["a non-finite value", { ...LCP, value: "NaN" }],
    ["a negative value", { ...LCP, value: -1 }],
    ["a bad rating", { ...LCP, rating: "excellent" }],
    ["a path that is not a path", { ...LCP, path: "https://elsewhere.example.com/" }],
    ["a malformed view", { ...LCP, view: "<script>" }],
    ["a body that is not JSON", "not json"],
  ])("refuses %s with 400 and writes and logs nothing", async (_label, body) => {
    const res = await POST(post(body));

    expect(res.status).toBe(400);
    expect(mockWriteVital).not.toHaveBeenCalled();
    expect(webVitalLines(logSpy)).toHaveLength(0);
  });

  it("does nothing when the operator switched vitals off", async () => {
    process.env.GENUS_DASHBOARD_WEB_VITALS = "false";

    const res = await POST(post(LCP));

    expect(res.status).toBe(204);
    expect(mockWriteVital).not.toHaveBeenCalled();
    expect(webVitalLines(logSpy)).toHaveLength(0);
  });

  it("still logs and answers 204 when Redis is down", async () => {
    mockWriteVital.mockRejectedValue(new Error("ECONNREFUSED"));

    const res = await POST(post(LCP));

    expect(res.status).toBe(204);
    expect(webVitalLines(logSpy)).toHaveLength(1);
  });
  it("drops a well-formed view the dashboard does not have", async () => {
    await POST(post({ ...LCP, view: "made-up-view" }));

    expect(mockWriteVital.mock.calls[0][0]).not.toHaveProperty("view");
  });
});
