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

    await new EngineClient().chatSend("agent:main:primary", "hello");

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

    await new EngineClient().chatHistory("agent:main:primary");

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
