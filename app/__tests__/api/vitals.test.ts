import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const mockWriteVital = vi.fn();
vi.mock("@/lib/event-bus/redis-client", () => ({
  writeVital: (...args: unknown[]) => mockWriteVital(...args),
}));

import { POST } from "@/app/api/vitals/route";
import { VITALS_MAX_BODY_BYTES, VITALS_RATE_LIMIT } from "@/lib/web-vitals";

function post(body: unknown, clientIp?: string): Request {
  return new Request("http://localhost:3004/api/vitals", {
    method: "POST",
    headers: {
      "content-type": "text/plain",
      // The edge appends the real client; 10.0.0.1 is the trusted proxy.
      ...(clientIp ? { "x-forwarded-for": `${clientIp}, 10.0.0.1` } : {}),
    },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

const LCP = { name: "LCP", value: 1234.5, rating: "good", path: "/", view: "chat", id: "v1-1", navigation_type: "navigate" };

/**
 * The structured line goes to standard output as exactly one JSON object per
 * line, through the server logger -- not console.log, which the dashboard's
 * lint forbids and which the log pipeline would see as free text.
 */
function webVitalLines(spy: ReturnType<typeof vi.spyOn>): Record<string, unknown>[] {
  return spy.mock.calls
    .map((call: unknown[]) => {
      if (typeof call[0] !== "string" || !call[0].endsWith("\n")) return null;
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
    logSpy = vi.spyOn(process.stdout, "write").mockImplementation(() => true);
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

  describe("reachable signed out, so it is bounded", () => {
    beforeEach(() => {
      process.env.GENUS_DASHBOARD_TRUSTED_PROXIES = "10.0.0.1/32";
    });
    afterEach(() => {
      delete process.env.GENUS_DASHBOARD_TRUSTED_PROXIES;
    });

    it("answers 429 and records nothing once one client spends its window", async () => {
      for (let i = 0; i < VITALS_RATE_LIMIT; i += 1) {
        expect((await POST(post(LCP, "192.0.2.10"))).status).toBe(204);
      }
      mockWriteVital.mockClear();
      logSpy.mockClear();

      const res = await POST(post(LCP, "192.0.2.10"));

      expect(res.status).toBe(429);
      expect(res.headers.get("retry-after")).toMatch(/^\d+$/);
      expect(mockWriteVital).not.toHaveBeenCalled();
      expect(webVitalLines(logSpy)).toHaveLength(0);
    });

    it("limits each client separately", async () => {
      for (let i = 0; i < VITALS_RATE_LIMIT; i += 1) await POST(post(LCP, "192.0.2.20"));

      expect((await POST(post(LCP, "192.0.2.21"))).status).toBe(204);
    });

    it("refuses an oversized body with 413 before parsing it", async () => {
      const res = await POST(post({ ...LCP, pad: "x".repeat(VITALS_MAX_BODY_BYTES) }, "192.0.2.30"));

      expect(res.status).toBe(413);
      expect(mockWriteVital).not.toHaveBeenCalled();
    });
  });
});
