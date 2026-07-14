import { describe, it, expect, vi, beforeEach } from "vitest";
import { GET } from "@/app/api/health/route";

// Mock global fetch
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

describe("GET /api/health", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    process.env.BRIDGE_URL = "http://localhost:9100";
    process.env.ORCHESTRATOR_URL = "http://localhost:9099";
    process.env.VISION_URL = "http://localhost:8600";
  });

  it("returns ok when all services healthy", async () => {
    mockFetch.mockResolvedValue({ ok: true });

    const response = await GET();
    const body = await response.json();

    expect(body.status).toBe("ok");
    expect(body.services).toHaveLength(3);
    expect(body.services.every((s: { status: string }) => s.status === "healthy")).toBe(true);
    expect(body.timestamp).toBeDefined();
  });

  it("returns degraded when a service is down", async () => {
    mockFetch
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({ ok: false })
      .mockResolvedValueOnce({ ok: true });

    const response = await GET();
    const body = await response.json();

    expect(body.status).toBe("degraded");
  });

  it("returns degraded when a service errors", async () => {
    mockFetch
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error("Connection refused"))
      .mockResolvedValueOnce({ ok: true });

    const response = await GET();
    const body = await response.json();

    expect(body.status).toBe("degraded");
    expect(body.services[1].status).toBe("unhealthy");
  });

  it("checks bridge, orchestrator, and vision services", async () => {
    mockFetch.mockResolvedValue({ ok: true });

    await GET();

    const urls = mockFetch.mock.calls.map((c: unknown[]) => String(c[0]));
    expect(urls).toContain("http://localhost:9100/health");
    expect(urls).toContain("http://localhost:9099/health");
    expect(urls).toContain("http://localhost:8600/health");
  });

  it("includes response times", async () => {
    mockFetch.mockResolvedValue({ ok: true });

    const response = await GET();
    const body = await response.json();

    body.services.forEach((s: { responseTime: number }) => {
      expect(typeof s.responseTime).toBe("number");
      expect(s.responseTime).toBeGreaterThanOrEqual(0);
    });
  });

  it("rejects non-HTTP service targets without making a request", async () => {
    process.env.BRIDGE_URL = "file:///etc/passwd";
    process.env.ORCHESTRATOR_URL = "file:///etc/passwd";
    process.env.VISION_URL = "file:///etc/passwd";

    const response = await GET();
    const body = await response.json();

    expect(body.status).toBe("degraded");
    expect(body.services.every((s: { status: string }) => s.status === "unhealthy")).toBe(true);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("does not follow backend redirects during a health probe", async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 302 });

    await GET();

    for (const [, options] of mockFetch.mock.calls) {
      expect(options).toMatchObject({ redirect: "manual" });
    }
  });
});
