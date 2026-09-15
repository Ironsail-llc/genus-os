import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * The server-side half of "which agent may this caller talk to".
 *
 * `chattable` and the `require_operator` gate on `GET /api/agent-manifests` are
 * UI. The BFF forwards to `chat.py`, which takes `agent_id` straight out of
 * `parts[1]` of the session key and runs that agent — and `_require_chat_auth`
 * asks only for authentication. Before this guard, `POST /api/chat/send
 * {"agent":"x"}` from any signed-in caller reached any agent on the appliance.
 * That is not a tool-permission escalation (tool RBAC still travels with
 * `user_role`), but it is a capability non-operators did not have, and the
 * first report of this work claimed they did not have it.
 *
 * So the same listing that draws the switcher is the authority the BFF checks
 * against, with the CALLER's own credentials — a member's listing is refused,
 * therefore a member's `agent` is refused. The check is skipped entirely when
 * no agent was named, which is every request the default chat makes.
 */

const bridgeAuthHeaders = vi.fn(async () => ({ Authorization: "Bearer caller-token" }));

vi.mock("@/lib/bridge-auth", () => ({
  bridgeAuthHeaders: () => bridgeAuthHeaders(),
}));

vi.mock("@/lib/services/registry", () => ({
  getServiceUrl: () => "http://bridge.test:9100",
}));

const LISTING = {
  default_agent: "main",
  agents: [
    { id: "main", name: "Robothor", chattable: true, session_target: "isolated" },
    { id: "scheduler", name: "Scheduler", chattable: true, session_target: "persistent" },
    { id: "invoice-worker", name: "Invoice Worker", chattable: false, session_target: "isolated" },
    { id: "acme.bot", name: "Acme Bot", chattable: true, session_target: "persistent" },
  ],
};

function listing(status: number, body: unknown = LISTING) {
  global.fetch = vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    json: async () => body,
  });
}

async function resolve(agent: unknown) {
  const { resolveChatAgent } = await import("../agent-guard");
  return resolveChatAgent(agent);
}

describe("resolveChatAgent", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listing(200);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("allows a chattable agent and returns its key", async () => {
    await expect(resolve("scheduler")).resolves.toEqual({
      ok: true,
      key: "agent:scheduler:primary",
    });
  });

  it("allows an id the old client regex refused, because the listing offers it", async () => {
    await expect(resolve("acme.bot")).resolves.toEqual({
      ok: true,
      key: "agent:acme.bot:primary",
    });
  });

  it("never asks the bridge anything when no agent was named", async () => {
    await expect(resolve(undefined)).resolves.toEqual({ ok: true, key: "" });
    await expect(resolve("")).resolves.toEqual({ ok: true, key: "" });
    await expect(resolve("   ")).resolves.toEqual({ ok: true, key: "" });

    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("sends the main agent's own id with no key", async () => {
    // A client is free to name the default explicitly. The answer is still "no
    // key" — the point of the rule is the session that gets written to, not
    // which spelling the browser used.
    await expect(resolve("main")).resolves.toEqual({ ok: true, key: "" });
  });

  it("refuses an agent the listing does not mark chattable", async () => {
    const result = await resolve("invoice-worker");

    expect(result.ok).toBe(false);
    expect(result).toMatchObject({ status: 403 });
  });

  it("refuses an agent that is not in the listing at all", async () => {
    const result = await resolve("ghost");

    expect(result.ok).toBe(false);
    expect(result).toMatchObject({ status: 403 });
  });

  it("refuses every agent for a caller whose listing is refused", async () => {
    // The member case. Their listing 403s, so there is no set of agents they
    // have been shown, so there is no agent they may name.
    listing(403, { detail: "operator only" });

    const result = await resolve("scheduler");

    expect(result.ok).toBe(false);
    expect(result).toMatchObject({ status: 403 });
  });

  it("refuses rather than guessing when the bridge cannot be reached", async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error("connect ECONNREFUSED"));

    const result = await resolve("scheduler");

    expect(result.ok).toBe(false);
    expect(result).toMatchObject({ status: 403 });
  });

  it("refuses a named agent whose id cannot be keyed, without consulting anything", async () => {
    // 400, not 403: the caller is not being denied an agent, they have sent a
    // value this route cannot turn into a session key at all. What must NOT
    // happen is the old behaviour — key `""`, i.e. the operator's main session.
    const result = await resolve("a:primary:b");

    expect(result).toMatchObject({ ok: false, status: 400 });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("refuses a non-string agent the same way", async () => {
    await expect(resolve(42)).resolves.toMatchObject({ ok: false, status: 400 });
    await expect(resolve({ id: "scheduler" })).resolves.toMatchObject({ ok: false, status: 400 });
  });

  it("asks the bridge as the CALLER, never as the app", async () => {
    await resolve("scheduler");

    expect(bridgeAuthHeaders).toHaveBeenCalledTimes(1);
    const [url, init] = (global.fetch as unknown as { mock: { calls: unknown[][] } }).mock
      .calls[0] as [string, RequestInit];
    expect(url).toBe("http://bridge.test:9100/api/agent-manifests");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer caller-token");
  });
});
