// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { checkService } from "@/lib/services/health";

/**
 * The Helm's service probe must read what a service SAYS, not just whether the
 * socket answered: a readiness body with a failing check is degraded, a service
 * that reports itself switched off is "disabled" (neither healthy nor down),
 * and an authentication wall on the probe path is named rather than reported
 * as an outage. Production defect 2026-09-11: the bridge showed "unhealthy" on
 * the home screen because its probe path answered 401, while the operator had
 * just signed in through that same bridge.
 */
function resp(status: number, body: unknown): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => JSON.parse(text),
    text: async () => text,
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("checkService", () => {
  it("reads a readiness body whose checks all pass as healthy and lists the checks", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => resp(200, { status: "ok", checks: { database: "ok", redis: "ok" } })),
    );
    const r = await checkService("engine", "http://127.0.0.1:18800/ready");
    expect(r.status).toBe("healthy");
    expect(r.label).toBe("Agent engine");
    expect(r.detail).toContain("database ok");
    expect(r.detail).toContain("redis ok");
  });

  it("reports a failing readiness check as degraded and names the check", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => resp(503, { status: "degraded", checks: { crm: "ok", memory: "error:503" } })),
    );
    const r = await checkService("bridge", "http://127.0.0.1:9100/ready");
    expect(r.status).toBe("degraded");
    expect(r.label).toBe("API bridge");
    expect(r.detail).toContain("memory");
    expect(r.detail).not.toContain("crm");
  });

  it("treats a service that says it is switched off as disabled, not healthy and not down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => resp(200, { running: true, available: false, mode: "disabled", people_present: [] })),
    );
    const r = await checkService("vision", "http://127.0.0.1:8600/health");
    expect(r.status).toBe("disabled");
    expect(r.label).toBe("Vision (camera)");
    expect(r.detail).toContain("disabled");
  });

  it("names an authentication wall on the probe path instead of calling the service down", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => resp(401, { error: "authentication required" })));
    const r = await checkService("bridge", "http://127.0.0.1:9100/health");
    expect(r.status).toBe("unhealthy");
    expect(r.detail).toContain("401");
    expect(r.detail).toContain("authentication");
  });

  it("is unhealthy and says unreachable when the socket does not answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("ECONNREFUSED");
      }),
    );
    const r = await checkService("orchestrator", "http://127.0.0.1:9099/health");
    expect(r.status).toBe("unhealthy");
    expect(r.detail).toContain("unreachable");
  });

  it("is disabled, naming the variable to set, when no URL is configured", async () => {
    const r = await checkService("engine", null);
    expect(r.status).toBe("disabled");
    expect(r.detail).toContain("ROBOTHOR_ENGINE_URL");
  });

  it("still reads a plain 200 with no JSON body as healthy", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => {
          throw new Error("not json");
        },
        text: async () => "OK",
      }) as unknown as Response),
    );
    const r = await checkService("orchestrator", "http://127.0.0.1:9099/health");
    expect(r.status).toBe("healthy");
  });
});
