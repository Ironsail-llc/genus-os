// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { _resetCache } from "@/lib/services/registry";

/**
 * /api/health probes every core service at the path the service manifest
 * declares as its health/readiness endpoint — the bridge and engine publish
 * an unauthenticated /ready with per-dependency checks, and their /health is
 * behind the auth wall — and a service that is switched off never degrades
 * the overall status.
 */
const ENV = {
  ROBOTHOR_ENGINE_URL: "http://127.0.0.1:18800",
  BRIDGE_URL: "http://127.0.0.1:9100",
  ORCHESTRATOR_URL: "http://127.0.0.1:9099",
  VISION_URL: "http://127.0.0.1:8600",
};
const saved: Record<string, string | undefined> = {};

function resp(status: number, body: unknown): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => JSON.parse(text),
    text: async () => text,
  } as Response;
}

function fetchByUrl(answers: Record<string, Response>) {
  const calls: string[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = input.toString();
    calls.push(url);
    const hit = Object.entries(answers).find(([prefix]) => url.startsWith(prefix));
    return hit ? hit[1] : resp(404, { error: "no such probe" });
  });
  return { fn, calls };
}

beforeEach(() => {
  for (const [k, v] of Object.entries(ENV)) {
    saved[k] = process.env[k];
    process.env[k] = v;
  }
  _resetCache();
});

afterEach(() => {
  for (const k of Object.keys(ENV)) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
  vi.unstubAllGlobals();
  _resetCache();
});

describe("GET /api/health", () => {
  it("probes engine and bridge on /ready, orchestrator and vision on /health", async () => {
    const { fn, calls } = fetchByUrl({
      "http://127.0.0.1:18800/ready": resp(200, { status: "ok", checks: { database: "ok" } }),
      "http://127.0.0.1:9100/ready": resp(200, { status: "ok", checks: { crm: "ok", memory: "ok" } }),
      "http://127.0.0.1:9099/health": resp(200, { status: "ok" }),
      "http://127.0.0.1:8600/health": resp(200, { status: "ok", running: true, available: true, mode: "active" }),
    });
    vi.stubGlobal("fetch", fn);
    const { GET } = await import("@/app/api/health/route");
    const body = await (await GET()).json();

    expect(calls.sort()).toEqual([
      "http://127.0.0.1:18800/ready",
      "http://127.0.0.1:8600/health",
      "http://127.0.0.1:9099/health",
      "http://127.0.0.1:9100/ready",
    ]);
    expect(body.status).toBe("ok");
    expect(body.services.map((s: { name: string }) => s.name).sort()).toEqual([
      "bridge",
      "engine",
      "orchestrator",
      "vision",
    ]);
    expect(body.services.every((s: { status: string }) => s.status === "healthy")).toBe(true);
  });

  it("stays ok when a service is switched off, and says so per service", async () => {
    const { fn } = fetchByUrl({
      "http://127.0.0.1:18800/ready": resp(200, { status: "ok", checks: {} }),
      "http://127.0.0.1:9100/ready": resp(200, { status: "ok", checks: {} }),
      "http://127.0.0.1:9099/health": resp(200, { status: "ok" }),
      "http://127.0.0.1:8600/health": resp(200, { running: true, available: false, mode: "disabled" }),
    });
    vi.stubGlobal("fetch", fn);
    const { GET } = await import("@/app/api/health/route");
    const body = await (await GET()).json();
    expect(body.status).toBe("ok");
    const vision = body.services.find((s: { name: string }) => s.name === "vision");
    expect(vision.status).toBe("disabled");
  });

  it("is degraded when any service reports a failing check or is down", async () => {
    const { fn } = fetchByUrl({
      "http://127.0.0.1:18800/ready": resp(200, { status: "ok", checks: {} }),
      "http://127.0.0.1:9100/ready": resp(503, { status: "degraded", checks: { crm: "ok", memory: "error:503" } }),
      "http://127.0.0.1:9099/health": resp(200, { status: "ok" }),
      "http://127.0.0.1:8600/health": resp(200, { running: true, available: false, mode: "disabled" }),
    });
    vi.stubGlobal("fetch", fn);
    const { GET } = await import("@/app/api/health/route");
    const body = await (await GET()).json();
    expect(body.status).toBe("degraded");
    const bridge = body.services.find((s: { name: string }) => s.name === "bridge");
    expect(bridge.status).toBe("degraded");
    expect(bridge.detail).toContain("memory");
  });
});
