import { describe, it, expect, vi, beforeEach } from "vitest";
import { CANVAS_SHIM_SOURCE } from "../canvas-shim";

describe("CANVAS_SHIM_SOURCE", () => {
  beforeEach(() => {
    // fresh window.robothor per test
    delete (window as unknown as { robothor?: unknown }).robothor;
  });

  it("defines window.robothor.read/propose when evaluated", () => {
    const parentPost = vi.fn();
    // simulate the iframe: parent is the mock, self is window
    const fn = new Function("parent", "self", CANVAS_SHIM_SOURCE);
    fn({ postMessage: parentPost }, window);
    const robothor = (window as unknown as { robothor: { read: unknown; propose: unknown } }).robothor;
    expect(typeof robothor.read).toBe("function");
    expect(typeof robothor.propose).toBe("function");
  });

  it("read posts a tagged read message and resolves when the matching result arrives", async () => {
    const posted: unknown[] = [];
    const parent = { postMessage: (m: unknown) => posted.push(m) };
    const fn = new Function("parent", "self", CANVAS_SHIM_SOURCE);
    fn(parent, window);
    const robothor = (window as unknown as { robothor: { read: (op: string, a?: unknown) => Promise<unknown> } }).robothor;

    const p = robothor.read("get_fleet");
    const sent = posted[0] as { __robothor: boolean; kind: string; reqId: string; op: string };
    expect(sent).toMatchObject({ __robothor: true, kind: "read", op: "get_fleet" });
    expect(typeof sent.reqId).toBe("string");

    // deliver the matching result
    window.dispatchEvent(new MessageEvent("message", {
      data: { __robothor: true, kind: "read-result", reqId: sent.reqId, ok: true, data: [{ agent_id: "main" }] },
    }));
    await expect(p).resolves.toEqual([{ agent_id: "main" }]);
  });

  it("propose posts a tagged propose message (fire-and-forget)", () => {
    const posted: unknown[] = [];
    const parent = { postMessage: (m: unknown) => posted.push(m) };
    const fn = new Function("parent", "self", CANVAS_SHIM_SOURCE);
    fn(parent, window);
    const robothor = (window as unknown as { robothor: { propose: (a: string, args: unknown, l?: string) => void } }).robothor;
    robothor.propose("set_flag", { name: "ROBOTHOR_RBAC_MODE", value: "enforce" }, "Enforce RBAC");
    expect(posted[0]).toMatchObject({ __robothor: true, kind: "propose", action: "set_flag" });
  });
});
