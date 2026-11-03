import { afterEach, describe, expect, it, vi } from "vitest";

const bridgeAuthHeaders = vi.fn();
vi.mock("@/lib/bridge-auth", () => ({ bridgeAuthHeaders }));

const { EngineClient } = await import("@/lib/engine/server-client");

describe("EngineClient authentication", () => {
  afterEach(() => {
    bridgeAuthHeaders.mockReset();
    vi.unstubAllGlobals();
  });

  it("forwards the verified dashboard identity on chat mutations", async () => {
    bridgeAuthHeaders.mockResolvedValue({
      Authorization: "Bearer signed-bridge-user-token",
    });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("event: done\ndata: {}\n\n", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await new EngineClient().chatSend("hello");

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][1].headers).toEqual({
      Authorization: "Bearer signed-bridge-user-token",
      "Content-Type": "application/json",
    });
  });

  it("forwards the verified dashboard identity on Engine reads", async () => {
    bridgeAuthHeaders.mockResolvedValue({
      Authorization: "Bearer signed-bridge-user-token",
    });
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ sessionKey: "agent:main:primary", messages: [] }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await new EngineClient().chatHistory();

    expect(fetchMock.mock.calls[0][1].headers).toEqual({
      Authorization: "Bearer signed-bridge-user-token",
    });
  });

  it("forwards verified identity to the internal completion boundary only", async () => {
    bridgeAuthHeaders.mockResolvedValue({
      Authorization: "Bearer signed-bridge-user-token",
    });
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ content: "<div>safe</div>" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const content = await new EngineClient().dashboardCompletion(
      "render",
      "system prompt",
      "user prompt",
    );

    expect(content).toBe("<div>safe</div>");
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:18800/api/dashboard/completions");
    expect(url).not.toMatch(/^https?:\/\/(?:api\.)?(?:openrouter|anthropic|openai)/i);
    expect(options.headers).toEqual({
      Authorization: "Bearer signed-bridge-user-token",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(options.body)).toEqual({
      purpose: "render",
      system_prompt: "system prompt",
      user_prompt: "user prompt",
    });
    expect(options.body).not.toContain("model");
    expect(options.body).not.toContain("api_key");
  });

  it("fails before fetch when no verified bearer identity exists", async () => {
    bridgeAuthHeaders.mockResolvedValue({});
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      new EngineClient().dashboardCompletion("triage", "system", "user"),
    ).rejects.toThrow("Dashboard authentication required");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not expose backend or provider response details", async () => {
    bridgeAuthHeaders.mockResolvedValue({
      Authorization: "Bearer signed-bridge-user-token",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          { detail: "provider failure with sk-sensitive-value" },
          { status: 503 },
        ),
      ),
    );

    await expect(
      new EngineClient().dashboardCompletion("render", "system", "user"),
    ).rejects.toThrow("Dashboard completion unavailable");
  });
});

/** A bridge-shaped JWT: only the payload matters, the bridge verified it. */
function bearer(claims: Record<string, unknown>): string {
  const part = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64url");
  return `Bearer ${part({ alg: "HS256", typ: "JWT" })}.${part(claims)}.signature`;
}

describe("EngineClient routes to the acting tenant's engine", () => {
  afterEach(() => {
    bridgeAuthHeaders.mockReset();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("sends an acting session to its tenant's engine", async () => {
    vi.stubEnv("ROBOTHOR_ENGINE_URL", "http://engine.root.test:18800");
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", "http://genus-engine-{tenant}:18800");
    bridgeAuthHeaders.mockResolvedValue({
      Authorization: bearer({ tid: "acme-child-a", home_tid: "acme-parent", act: true }),
    });
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ messages: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await new EngineClient().chatHistory();

    expect(fetchMock.mock.calls[0][0]).toMatch(
      /^http:\/\/genus-engine-acme-child-a:18800\/chat\/history/,
    );
  });

  it("keeps a home session on the root engine", async () => {
    vi.stubEnv("ROBOTHOR_ENGINE_URL", "http://engine.root.test:18800");
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", "http://genus-engine-{tenant}:18800");
    bridgeAuthHeaders.mockResolvedValue({
      Authorization: bearer({ tid: "default", home_tid: "default" }),
    });
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await new EngineClient().chatClear();

    expect(fetchMock.mock.calls[0][0]).toBe("http://engine.root.test:18800/chat/clear");
  });

  it("reads ROBOTHOR_ENGINE_URL at call time, not at import", async () => {
    vi.stubEnv("ROBOTHOR_ENGINE_URL", "http://engine.later.test:18800");
    bridgeAuthHeaders.mockResolvedValue({});
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ messages: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await new EngineClient().chatHistory();

    expect(fetchMock.mock.calls[0][0]).toMatch(/^http:\/\/engine\.later\.test:18800\//);
  });
});
