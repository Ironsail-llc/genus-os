import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * The exact bytes each chat call puts on the wire, with and without a chosen
 * agent.
 *
 * This is the test the whole feature is balanced on. The engine's
 * `_effective_session_key` treats an ABSENT `session_key` and an empty one
 * identically — both resolve to the main session — but it treats a PRESENT
 * non-empty one as the caller naming a conversation. So the rule the Helm has
 * to keep is not "send the right key"; it is "send no key at all unless the
 * person picked somebody else". These assertions are on the parsed body, key by
 * key, because `toMatchObject` would pass on a body that carried
 * `session_key: ""` into every request the operator has ever made.
 */

vi.mock("@/lib/bridge-auth", () => ({
  bridgeAuthHeaders: async () => ({ Authorization: "Bearer test-token" }),
}));

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => body,
  };
}

function lastCall(): { url: string; init: RequestInit } {
  const calls = (global.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
  const [url, init] = calls[calls.length - 1] as [string, RequestInit];
  return { url, init };
}

function lastBody(): Record<string, unknown> {
  return JSON.parse(String(lastCall().init.body));
}

describe("EngineClient — the session key, or the absence of one", () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockResolvedValue(jsonResponse({ ok: true, messages: [] }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  async function client() {
    const { getEngineClient } = await import("../server-client");
    return getEngineClient();
  }

  it.each(["", "agent:scheduler:primary"])("recovers the original request with authenticated read-only access: %s", async (key) => {
    await (await client()).chatOutcome("original-request", key);
    const { url, init } = lastCall();
    const query = new URL(url).searchParams;
    expect(query.get("request_id")).toBe("original-request");
    expect(query.get("session_key")).toBe(key || null);
    expect(init.body).toBeUndefined();
    expect(init.method ?? "GET").toBe("GET");
    expect(init.cache).toBe("no-store");
    expect(init.headers).toEqual({ Authorization: "Bearer test-token" });
  });

  it("omits session_key from /chat/send when no agent was chosen", async () => {
    await (await client()).chatSend("hello");

    expect(Object.keys(lastBody())).toEqual(["message"]);
  });

  it("asks /chat/send to join the running turn only when told to", async () => {
    await (await client()).chatSend("also Y", "", undefined, true);
    expect(lastBody()).toEqual({ message: "also Y", join_running: true });

    await (await client()).chatSend("hi", "", "request-1");
    expect(lastBody()).toEqual({ message: "hi", request_id: "request-1" });
  });

  it("names the session on /chat/send when an agent was chosen", async () => {
    await (await client()).chatSend("hello", "agent:scheduler:primary");

    expect(lastBody()).toEqual({ message: "hello", session_key: "agent:scheduler:primary" });
  });

  it("omits session_key from the /chat/history query when no agent was chosen", async () => {
    await (await client()).chatHistory(50);

    expect(lastCall().url).toContain("/chat/history?limit=50");
    expect(lastCall().url).not.toContain("session_key");
  });

  it("names the session on the /chat/history query when an agent was chosen", async () => {
    await (await client()).chatHistory(50, "agent:scheduler:primary");

    expect(lastCall().url).toContain("session_key=agent%3Ascheduler%3Aprimary");
  });

  it("omits session_key from /chat/clear and /chat/abort when no agent was chosen", async () => {
    const engine = await client();

    await engine.chatClear();
    expect(lastBody()).toEqual({});

    await engine.chatAbort();
    expect(lastBody()).toEqual({});
  });

  it("names the session on /chat/clear and /chat/abort when an agent was chosen", async () => {
    const engine = await client();

    await engine.chatClear("agent:scheduler:primary");
    expect(lastBody()).toEqual({ session_key: "agent:scheduler:primary" });

    await engine.chatAbort("agent:scheduler:primary");
    expect(lastBody()).toEqual({ session_key: "agent:scheduler:primary" });
  });

  it("carries the key through the whole plan round-trip", async () => {
    const engine = await client();
    const key = "agent:scheduler:primary";

    await engine.planStart("do it", false, key);
    expect(lastBody()).toEqual({ message: "do it", deep_plan: false, session_key: key });

    await engine.planApprove("plan-1", key);
    expect(lastBody()).toEqual({ plan_id: "plan-1", session_key: key });

    await engine.planReject("plan-1", "no", key);
    expect(lastBody()).toEqual({ plan_id: "plan-1", feedback: "no", session_key: key });

    await engine.planStatus(key);
    expect(lastCall().url).toContain("session_key=agent%3Ascheduler%3Aprimary");
  });

  it("leaves the plan round-trip unkeyed when no agent was chosen", async () => {
    const engine = await client();

    await engine.planStart("do it", false);
    expect(lastBody()).toEqual({ message: "do it", deep_plan: false });

    await engine.planApprove("plan-1");
    expect(lastBody()).toEqual({ plan_id: "plan-1" });

    await engine.planStatus();
    expect(lastCall().url).not.toContain("session_key");
  });

  it("carries the key through deep mode, and omits it otherwise", async () => {
    const engine = await client();

    await engine.deepStart("why", "agent:scheduler:primary");
    expect(lastBody()).toEqual({ query: "why", session_key: "agent:scheduler:primary" });

    await engine.deepStart("why");
    expect(lastBody()).toEqual({ query: "why" });

    await engine.deepStatus("agent:scheduler:primary");
    expect(lastCall().url).toContain("session_key=agent%3Ascheduler%3Aprimary");

    await engine.deepStatus();
    expect(lastCall().url).not.toContain("session_key");
  });

  it("treats an empty key exactly as no key — never as a named session", async () => {
    await (await client()).chatSend("hello", "");

    expect(Object.keys(lastBody())).toEqual(["message"]);
  });
});
