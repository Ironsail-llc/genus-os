// @vitest-environment node
/**
 * The guard's listing is cached per caller, briefly.
 *
 * Every send that names an agent used to fetch the bridge's whole manifest
 * listing, which the bridge builds by rescanning the manifest directory. The
 * listing is fetched with the CALLER's credentials and it is the authority, so
 * the cache is keyed by those credentials: one caller's listing never answers
 * for another. Only a listing the bridge served is kept; a refusal or an
 * unreachable bridge is asked again next time.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { NextRequest } from "next/server";

const auth = vi.hoisted(() => ({ token: "caller-a" }));
vi.mock("@/lib/bridge-auth", () => ({
  bridgeAuthHeaders: async () => ({ Authorization: `Bearer ${auth.token}` }),
}));
vi.mock("@/lib/services/registry", () => ({ getServiceUrl: () => "http://bridge.test:9100" }));

const LISTING = {
  default_agent: "main",
  agents: [
    { id: "main", chattable: true },
    { id: "scheduler", chattable: true },
  ],
};

let fetcher: ReturnType<typeof vi.fn>;
function listingCalls() {
  return fetcher.mock.calls.filter(([url]) => String(url) === "http://bridge.test:9100/api/agent-manifests").length;
}

async function guard() {
  return import("../agent-guard");
}

describe("resolveChatAgent listing cache", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers({ toFake: ["Date"] });
    auth.token = "caller-a";
    fetcher = vi.fn(async () => Response.json(LISTING));
    vi.stubGlobal("fetch", fetcher);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("asks the bridge once for two sends by the same caller", async () => {
    const { resolveChatAgent } = await guard();
    await expect(resolveChatAgent("scheduler")).resolves.toEqual({ ok: true, key: "agent:scheduler:primary" });
    await expect(resolveChatAgent("scheduler")).resolves.toEqual({ ok: true, key: "agent:scheduler:primary" });
    expect(listingCalls()).toBe(1);
  });

  it("asks again once 30 seconds have passed", async () => {
    const { resolveChatAgent } = await guard();
    await resolveChatAgent("scheduler");
    vi.setSystemTime(Date.now() + 29_000);
    await resolveChatAgent("scheduler");
    expect(listingCalls()).toBe(1);
    vi.setSystemTime(Date.now() + 2_000);
    await resolveChatAgent("scheduler");
    expect(listingCalls()).toBe(2);
  });

  it("never answers one caller from another caller's listing", async () => {
    const { resolveChatAgent } = await guard();
    await resolveChatAgent("scheduler");
    auth.token = "caller-b";
    fetcher.mockImplementation(async () => Response.json({ detail: "operator only" }, { status: 403 }));
    await expect(resolveChatAgent("scheduler")).resolves.toMatchObject({ ok: false, status: 403 });
    expect(listingCalls()).toBe(2);
  });

  it("does not keep a refused listing", async () => {
    fetcher.mockImplementation(async () => Response.json({ detail: "operator only" }, { status: 403 }));
    const { resolveChatAgent } = await guard();
    await expect(resolveChatAgent("scheduler")).resolves.toMatchObject({ ok: false, status: 403 });
    fetcher.mockImplementation(async () => Response.json(LISTING));
    await expect(resolveChatAgent("scheduler")).resolves.toEqual({ ok: true, key: "agent:scheduler:primary" });
    expect(listingCalls()).toBe(2);
  });

  it("does not keep an unreachable bridge", async () => {
    fetcher.mockImplementation(async () => {
      throw new Error("connect ECONNREFUSED");
    });
    const { resolveChatAgent } = await guard();
    await expect(resolveChatAgent("scheduler")).resolves.toMatchObject({ ok: false, status: 403 });
    fetcher.mockImplementation(async () => Response.json(LISTING));
    await expect(resolveChatAgent("scheduler")).resolves.toMatchObject({ ok: true });
    expect(listingCalls()).toBe(2);
  });

  it("is off when GENUS_CHAT_AGENT_CACHE_TTL_MS is 0", async () => {
    vi.stubEnv("GENUS_CHAT_AGENT_CACHE_TTL_MS", "0");
    const { resolveChatAgent } = await guard();
    await resolveChatAgent("scheduler");
    await resolveChatAgent("scheduler");
    expect(listingCalls()).toBe(2);
  });

  it("forgets every listing when a manifest changes through the bridge proxy", async () => {
    const { resolveChatAgent } = await guard();
    const { PUT } = await import("@/app/api/bridge/[...path]/route");
    await resolveChatAgent("scheduler");
    await PUT(
      new NextRequest("http://dashboard.example.invalid/api/bridge/api/agent-manifests/scheduler", {
        method: "PUT",
        body: JSON.stringify({ yaml: "id: scheduler" }),
      }),
      { params: Promise.resolve({ path: ["api", "agent-manifests", "scheduler"] }) },
    );
    await resolveChatAgent("scheduler");
    expect(listingCalls()).toBe(2);
  });

  it("keeps the listing across a read through the proxy", async () => {
    const { resolveChatAgent } = await guard();
    const { GET } = await import("@/app/api/bridge/[...path]/route");
    await resolveChatAgent("scheduler");
    await GET(
      new NextRequest("http://dashboard.example.invalid/api/bridge/api/agent-manifests/scheduler"),
      { params: Promise.resolve({ path: ["api", "agent-manifests", "scheduler"] }) },
    );
    await resolveChatAgent("scheduler");
    expect(listingCalls()).toBe(1);
  });
});
