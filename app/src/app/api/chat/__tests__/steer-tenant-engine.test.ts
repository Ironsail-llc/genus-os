import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * Steer and Stop go to the engine of the tenant the session acts in, like
 * every other dashboard chat call: a child tenant's session that reached the
 * root engine was refused there, and the UI fell back to a queued follow-up
 * and a plain abort.
 */

const CHILD_ENGINE = "http://engine-child.test:18800";
const BEARER = "Bearer child-session-token";
const engineUrlForAuthorization = vi.fn();

vi.mock("@/lib/bridge-auth", () => ({
  bridgeAuthHeaders: async () => ({ Authorization: BEARER }),
}));
vi.mock("@/lib/services/registry", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/services/registry")>()),
  engineUrlForAuthorization: (authorization: string | undefined) =>
    engineUrlForAuthorization(authorization),
}));

import { chatInterrupt, chatSteer } from "@/lib/engine/steer-client";

describe("steer and stop reach the tenant's engine", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    engineUrlForAuthorization.mockReturnValue(CHILD_ENGINE);
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ ok: true, found: true }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("posts a steer to the acting tenant's engine with the session's bearer", async () => {
    await chatSteer("agent:main:primary", "also check the logs", "req-1");

    expect(engineUrlForAuthorization).toHaveBeenCalledWith(BEARER);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${CHILD_ENGINE}/chat/steer`);
    expect((init.headers as Record<string, string>).Authorization).toBe(BEARER);
  });

  it("posts a stop to the acting tenant's engine with the session's bearer", async () => {
    await chatInterrupt("agent:main:primary", "req-2");

    expect(engineUrlForAuthorization).toHaveBeenCalledWith(BEARER);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${CHILD_ENGINE}/chat/interrupt`);
    expect((init.headers as Record<string, string>).Authorization).toBe(BEARER);
  });
});
