/**
 * The workspace rules reach the main agent only where workspaces are served,
 * and only a definite answer is remembered: "served" for good, "not served"
 * for a while (so switching workspaces on needs no restart), and a timeout or
 * error not at all (it means "not served" for that request only).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockChatInject = vi.fn().mockResolvedValue({ ok: true });
const mockServed = vi.fn();

vi.mock("@/lib/engine/server-client", () => ({ getEngineClient: () => ({ chatInject: mockChatInject }) }));
vi.mock("@/lib/system-prompt", () => ({
  getVisualCanvasPrompt: (opts?: { workspaces?: boolean }) => (opts?.workspaces ? "canvas + workspaces" : "canvas"),
  getWorkspaceRulesPrompt: () => "workspaces",
}));
vi.mock("@/lib/rooms/served", () => ({ roomsServed: mockServed }));
vi.mock("@/lib/auth", () => ({ auth: vi.fn().mockResolvedValue({ user: { id: "alice" } }) }));

async function load() {
  return (await import("../session-state")).ensureCanvasPromptInjected;
}

function injected(): string[] {
  return mockChatInject.mock.calls.map(([text]) => text as string);
}

describe("ensureCanvasPromptInjected and workspaces", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-21T09:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("injects the workspace rules where workspaces are served, and asks once", async () => {
    mockServed.mockResolvedValue("served");
    const ensure = await load();
    await ensure();
    await ensure();
    expect(injected()).toEqual(["canvas + workspaces"]);
    expect(mockServed).toHaveBeenCalledTimes(1);
  });

  it("leaves them out where they are not served, and does not ask again for a while", async () => {
    mockServed.mockResolvedValue("not_served");
    const ensure = await load();
    await ensure();
    await ensure();
    expect(injected()).toEqual(["canvas"]);
    expect(mockServed).toHaveBeenCalledTimes(1);
  });

  it("adds the rules alone once a 'not served' answer has expired and workspaces are on", async () => {
    mockServed.mockResolvedValue("not_served");
    const ensure = await load();
    await ensure();
    vi.setSystemTime(new Date("2026-11-21T10:00:00Z"));
    mockServed.mockResolvedValue("served");
    await ensure();
    await ensure();
    expect(injected()).toEqual(["canvas", "workspaces"]);
    expect(mockServed).toHaveBeenCalledTimes(2);
  });

  it("never remembers a timeout or error: it asks again on the next call", async () => {
    mockServed.mockResolvedValue("unknown");
    const ensure = await load();
    await ensure();
    expect(injected()).toEqual(["canvas"]);
    mockServed.mockResolvedValue("served");
    await ensure();
    expect(mockServed).toHaveBeenCalledTimes(2);
    expect(injected()).toEqual(["canvas", "workspaces"]);
  });

  it("does not inject the canvas prompt twice while waiting for a definite answer", async () => {
    mockServed.mockResolvedValue("unknown");
    const ensure = await load();
    await ensure();
    await ensure();
    expect(injected()).toEqual(["canvas"]);
    expect(mockServed).toHaveBeenCalledTimes(2);
  });
});
