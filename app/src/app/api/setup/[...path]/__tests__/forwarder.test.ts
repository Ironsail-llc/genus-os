/**
 * The wizard's session-free path to the bridge.
 *
 * Two properties, and the second one is not cosmetic.
 *
 * It forwards the claim and nothing else — no cookie, no other header — which
 * is what keeps the routes that create the owner account unreachable with a
 * browser session.
 *
 * And it forwards `X-Client-IP`, because without it the bridge's claim limiter
 * is inert in the deployment this ships in: the call is made server-side, so
 * `request.client.host` on the bridge is the dashboard pod for every claim
 * attempt on Earth — one bucket of five a minute that anyone who can reach
 * `/setup` could spend continuously, keeping the real operator's claim at 429
 * for the life of their token. On a fresh appliance the wizard is the only way
 * in.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/services/registry", () => ({
  getServiceUrl: () => "http://bridge.test:9100",
}));

const { POST, GET } = await import("../route");

type Captured = { url: string; headers: Record<string, string>; body?: string };

function stubBridge(): Captured[] {
  const calls: Captured[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(input),
        headers: (init?.headers ?? {}) as Record<string, string>,
        body: init?.body as string | undefined,
      });
      return {
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        json: async () => ({ ok: true }),
        text: async () => "",
      } as Response;
    }),
  );
  return calls;
}

function request(
  path: string,
  {
    forwardedFor,
    authorization,
  }: { forwardedFor?: string; authorization?: string } = {},
) {
  const headers: Record<string, string> = {
    "content-type": "application/json",
  };
  if (forwardedFor) headers["x-forwarded-for"] = forwardedFor;
  if (authorization) headers.authorization = authorization;
  return new NextRequest(`https://genus.example/api/setup/${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify({ token: "printed" }),
  });
}

const params = (path: string) => ({
  params: Promise.resolve({ path: path.split("/") }),
});

describe("the setup forwarder", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("forwards to the bridge's setup prefix", async () => {
    const calls = stubBridge();

    await POST(request("claim"), params("claim"));

    expect(calls[0].url).toBe("http://bridge.test:9100/api/setup/claim");
  });

  it("forwards a bearer claim and no cookie", async () => {
    const calls = stubBridge();

    await POST(
      request("operator", { authorization: "Bearer claim-token-value" }),
      params("operator"),
    );

    expect(calls[0].headers.Authorization).toBe("Bearer claim-token-value");
    expect(
      Object.keys(calls[0].headers).map((k) => k.toLowerCase()),
    ).not.toContain("cookie");
  });

  it("ignores an Authorization header that is not a bearer", async () => {
    const calls = stubBridge();

    await POST(
      request("claim", { authorization: "Basic abc123" }),
      params("claim"),
    );

    expect(calls[0].headers.Authorization).toBeUndefined();
  });

  it("asserts no client address when this deployment names no edge", async () => {
    // The default. Nothing is vouched for, and the bridge uses its own peer
    // address — which is the honest answer, not a spoofable one.
    const calls = stubBridge();

    await POST(
      request("claim", { forwardedFor: "203.0.113.9" }),
      params("claim"),
    );

    expect(calls[0].headers["X-Client-IP"]).toBeUndefined();
  });

  it("forwards the end user's address when the edge is configured", async () => {
    vi.stubEnv("GENUS_DASHBOARD_TRUSTED_PROXIES", "10.0.0.0/8");
    const calls = stubBridge();

    // Left-most is what a client can write; the real client is the right-most
    // hop that is not one of ours.
    await POST(
      request("claim", { forwardedFor: "198.51.100.7, 203.0.113.9, 10.1.2.3" }),
      params("claim"),
    );

    expect(calls[0].headers["X-Client-IP"]).toBe("203.0.113.9");
  });

  it("refuses a path that walks out of the setup prefix", async () => {
    const calls = stubBridge();

    const response = await POST(
      request("../people", { authorization: "Bearer claim-token-value" }),
      params("../people"),
    );

    expect(response.status).toBe(404);
    expect(calls).toHaveLength(0);
  });

  it("refuses a body over the cap without forwarding it", async () => {
    const calls = stubBridge();
    const oversized = new NextRequest(
      "https://genus.example/api/setup/provider",
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "x".repeat(9 * 1024),
      },
    );

    const response = await POST(oversized, params("provider"));

    expect(response.status).toBe(413);
    expect(calls).toHaveLength(0);
  });

  it("answers 502 rather than throwing when the bridge is down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connection refused");
      }),
    );

    const response = await GET(request("status"), params("status"));

    expect(response.status).toBe(502);
  });
});
