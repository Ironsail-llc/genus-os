import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * Steer and interrupt reach the run of the conversation the browser names by
 * AGENT, never by session key, exactly like send and abort: the key comes from
 * `resolveChatAgent`, and a body's own `session_key` is ignored.
 */

const steer = {
  chatSteer: vi.fn(),
  chatInterrupt: vi.fn(),
  chatSteerEnabled: vi.fn(),
};
const resolveChatAgent = vi.fn();

vi.mock("@/lib/engine/steer-client", () => steer);
vi.mock("@/lib/chat/agent-guard", () => ({
  resolveChatAgent: (agent: unknown) => resolveChatAgent(agent),
}));

const KEY = "agent:scheduler:primary";

function post(body: unknown) {
  return new Request("http://helm.test/x", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

describe("steer and interrupt routes", () => {
  beforeEach(() => {
    resolveChatAgent.mockImplementation(async (agent: unknown) =>
      agent === "scheduler" ? { ok: true, key: KEY } : agent === undefined ? { ok: true, key: "" } : { ok: false, status: 403, error: "not yours" });
    steer.chatSteer.mockResolvedValue({ ok: true, found: true });
    steer.chatInterrupt.mockResolvedValue({ ok: true, found: true });
    steer.chatSteerEnabled.mockResolvedValue({ enabled: true });
  });

  afterEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  it("steers the named agent's run and ignores a browser session key", async () => {
    const { POST } = await import("../steer/route");
    const res = await POST(post({ agent: "scheduler", text: "Use the EU region.", session_key: "someone-else", request_id: "request-1" }));
    expect(steer.chatSteer).toHaveBeenCalledWith(KEY, "Use the EU region.", "request-1");
    expect(await res.json()).toEqual({ ok: true, found: true });
  });

  it("refuses an agent the caller may not address, and steers nothing", async () => {
    const { POST } = await import("../steer/route");
    const res = await POST(post({ agent: "other", text: "x" }));
    expect(res.status).toBe(403);
    expect(steer.chatSteer).not.toHaveBeenCalled();
  });

  it("refuses an empty steer", async () => {
    const { POST } = await import("../steer/route");
    const res = await POST(post({ text: "  " }));
    expect(res.status).toBe(400);
    expect(steer.chatSteer).not.toHaveBeenCalled();
  });

  it("reports whether steer mode is on", async () => {
    const { GET } = await import("../steer/route");
    const res = await GET();
    expect(await res.json()).toEqual({ enabled: true });
  });

  it("reports steer mode off when the engine cannot be asked", async () => {
    steer.chatSteerEnabled.mockRejectedValueOnce(new Error("down"));
    const { GET } = await import("../steer/route");
    expect(await (await GET()).json()).toEqual({ enabled: false });
  });

  it("interrupts the main conversation's run when no agent is named", async () => {
    const { POST } = await import("../interrupt/route");
    const res = await POST(post({ request_id: "request-1" }));
    expect(steer.chatInterrupt).toHaveBeenCalledWith("", "request-1");
    expect(await res.json()).toEqual({ ok: true, found: true });
  });

  it("says so when the engine cannot confirm the interrupt", async () => {
    steer.chatInterrupt.mockRejectedValueOnce(new Error("down"));
    const { POST } = await import("../interrupt/route");
    const res = await POST(post({ agent: "scheduler" }));
    expect(res.status).toBe(502);
  });
});
