import { describe, it, expect, vi, beforeEach } from "vitest";

// Mocked before importing the module under test so its internal `import`s
// resolve to these.
const mockChatInject = vi.fn().mockResolvedValue({ ok: true });
const mockAuth = vi.fn();

vi.mock("@/lib/engine/server-client", () => ({
  getEngineClient: () => ({ chatInject: mockChatInject }),
}));

vi.mock("@/lib/system-prompt", () => ({
  getVisualCanvasPrompt: () => "you have a canvas",
}));

vi.mock("@/lib/auth", () => ({
  auth: mockAuth,
}));

describe("ensureCanvasPromptInjected", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
  });

  it("injects once for a single user (flag-off / single-user case unchanged)", async () => {
    mockAuth.mockResolvedValue({ user: { id: "alice" } });
    const { ensureCanvasPromptInjected } = await import("../session-state");

    await ensureCanvasPromptInjected();
    await ensureCanvasPromptInjected();
    await ensureCanvasPromptInjected();

    expect(mockChatInject).toHaveBeenCalledTimes(1);
  });

  it("injects again for a second, distinct user id", async () => {
    const { ensureCanvasPromptInjected } = await import("../session-state");

    mockAuth.mockResolvedValue({ user: { id: "alice" } });
    await ensureCanvasPromptInjected();

    mockAuth.mockResolvedValue({ user: { id: "bob" } });
    await ensureCanvasPromptInjected();

    expect(mockChatInject).toHaveBeenCalledTimes(2);
  });

  it("does not re-inject on a repeat call from the same user", async () => {
    const { ensureCanvasPromptInjected } = await import("../session-state");

    mockAuth.mockResolvedValue({ user: { id: "alice" } });
    await ensureCanvasPromptInjected();
    await ensureCanvasPromptInjected();

    mockAuth.mockResolvedValue({ user: { id: "bob" } });
    await ensureCanvasPromptInjected();
    await ensureCanvasPromptInjected();

    expect(mockChatInject).toHaveBeenCalledTimes(2);
  });

  it("falls back to the shared SESSION_KEY dedup bucket when auth() has no session", async () => {
    mockAuth.mockResolvedValue(null);
    const { ensureCanvasPromptInjected, SESSION_KEY } = await import("../session-state");

    await ensureCanvasPromptInjected();
    await ensureCanvasPromptInjected();

    expect(mockChatInject).toHaveBeenCalledTimes(1);
    expect(mockChatInject).toHaveBeenCalledWith(
      SESSION_KEY,
      "you have a canvas",
      "visual-canvas-init"
    );
  });

  it("falls back to the shared dedup bucket when auth() throws", async () => {
    mockAuth.mockRejectedValue(new Error("session lookup failed"));
    const { ensureCanvasPromptInjected } = await import("../session-state");

    await ensureCanvasPromptInjected();
    await ensureCanvasPromptInjected();

    expect(mockChatInject).toHaveBeenCalledTimes(1);
  });

  it("always sends the shared SESSION_KEY to the engine, not the dedup key", async () => {
    mockAuth.mockResolvedValue({ user: { id: "alice" } });
    const { ensureCanvasPromptInjected, SESSION_KEY } = await import("../session-state");

    await ensureCanvasPromptInjected();

    expect(mockChatInject).toHaveBeenCalledWith(
      SESSION_KEY,
      "you have a canvas",
      "visual-canvas-init"
    );
  });

  it("does not mark as injected when chatInject fails, so a later call retries", async () => {
    mockAuth.mockResolvedValue({ user: { id: "alice" } });
    mockChatInject.mockRejectedValueOnce(new Error("engine unavailable"));
    const { ensureCanvasPromptInjected } = await import("../session-state");

    await ensureCanvasPromptInjected();
    await ensureCanvasPromptInjected();

    expect(mockChatInject).toHaveBeenCalledTimes(2);
  });
});
