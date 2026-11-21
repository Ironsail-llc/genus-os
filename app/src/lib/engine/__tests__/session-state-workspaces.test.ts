/**
 * The workspace rules reach the main agent only where workspaces are served:
 * the injected prompt is built with `workspaces` set from the rooms list.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockChatInject = vi.fn().mockResolvedValue({ ok: true });
const mockPrompt = vi.fn((opts?: { workspaces?: boolean }) => (opts?.workspaces ? "canvas + workspaces" : "canvas"));
const mockServed = vi.fn();

vi.mock("@/lib/engine/server-client", () => ({ getEngineClient: () => ({ chatInject: mockChatInject }) }));
vi.mock("@/lib/system-prompt", () => ({ getVisualCanvasPrompt: mockPrompt }));
vi.mock("@/lib/rooms/served", () => ({ roomsServed: mockServed }));
vi.mock("@/lib/auth", () => ({ auth: vi.fn().mockResolvedValue({ user: { id: "alice" } }) }));

describe("ensureCanvasPromptInjected and workspaces", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
  });

  it("injects the workspace rules where workspaces are served", async () => {
    mockServed.mockResolvedValue(true);
    const { ensureCanvasPromptInjected } = await import("../session-state");
    await ensureCanvasPromptInjected();
    expect(mockChatInject).toHaveBeenCalledWith("canvas + workspaces", "visual-canvas-init");
  });

  it("leaves them out where they are not", async () => {
    mockServed.mockResolvedValue(false);
    const { ensureCanvasPromptInjected } = await import("../session-state");
    await ensureCanvasPromptInjected();
    expect(mockChatInject).toHaveBeenCalledWith("canvas", "visual-canvas-init");
  });
});
