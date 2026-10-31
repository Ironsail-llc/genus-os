import { describe, it, expect, vi, afterEach } from "vitest";

/**
 * The engine checks attachments again and can refuse what the route let
 * through (a "picture" whose bytes are not one). That refusal is the engine's
 * answer to the request, not an engine failure: `chatSend` hands back its
 * status and words so the route can say them, instead of "Engine error".
 */

vi.mock("@/lib/bridge-auth", () => ({
  bridgeAuthHeaders: async () => ({ Authorization: "Bearer test-token" }),
}));

afterEach(() => { vi.restoreAllMocks(); });

async function client() {
  const { getEngineClient } = await import("../server-client");
  return getEngineClient();
}

describe("chatSend and an engine refusal", () => {
  it.each([400, 413, 422])("raises an EngineRefusal carrying a %s and the engine's error", async (status) => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: "shot.png is not the picture it claims to be" }), { status }),
    );
    const { EngineRefusal } = await import("../engine-refusal");
    const err = await (await client()).chatSend("x", "", undefined, false, []).catch((e) => e);
    expect(err).toBeInstanceOf(EngineRefusal);
    expect(err.status).toBe(status);
    expect(err.message).toBe("shot.png is not the picture it claims to be");
  });

  it("still treats a 500 as an engine error", async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response("boom", { status: 500, statusText: "Internal Server Error" }));
    const { EngineRefusal } = await import("../engine-refusal");
    const err = await (await client()).chatSend("x").catch((e) => e);
    expect(err).not.toBeInstanceOf(EngineRefusal);
    expect(String(err)).toMatch(/Engine error: 500/);
  });
});
