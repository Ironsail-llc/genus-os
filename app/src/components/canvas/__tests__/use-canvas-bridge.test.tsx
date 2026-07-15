import { renderHook, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { useCanvasBridge } from "../use-canvas-bridge";

afterEach(() => vi.restoreAllMocks());

// A stand-in for the iframe's contentWindow: it just records messages posted to it.
function makeIframeRef() {
  const posted: unknown[] = [];
  const contentWindow = { postMessage: (m: unknown) => posted.push(m) } as unknown as Window;
  const iframe = { contentWindow } as unknown as HTMLIFrameElement;
  return { ref: { current: iframe }, posted, contentWindow };
}

function fireMessage(source: unknown, data: unknown, origin = "null") {
  window.dispatchEvent(new MessageEvent("message", { data, origin, source: source as Window }));
}

describe("useCanvasBridge", () => {
  it("fulfils a whitelisted read from the correct iframe and posts the data back — no token in the reply", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true, json: async () => [{ agent_id: "main" }],
    } as Response);
    const { ref, posted, contentWindow } = makeIframeRef();
    renderHook(() => useCanvasBridge(ref));

    act(() => fireMessage(contentWindow, { __robothor: true, kind: "read", reqId: "r1", op: "get_fleet" }));

    await waitFor(() => expect(posted.length).toBe(1));
    expect(fetchMock).toHaveBeenCalledWith("/api/bridge/api/fleet", expect.anything());
    const reply = posted[0] as { kind: string; reqId: string; ok: boolean; data: unknown };
    expect(reply).toMatchObject({ __robothor: true, kind: "read-result", reqId: "r1", ok: true });
    expect(JSON.stringify(reply)).not.toMatch(/bearer|authorization|token/i);  // no credential ever
  });

  it("drops an unknown op — no fetch, surfaced in `dropped`", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    const { ref, contentWindow } = makeIframeRef();
    const { result } = renderHook(() => useCanvasBridge(ref));
    act(() => fireMessage(contentWindow, { __robothor: true, kind: "read", reqId: "r2", op: "get_secret" }));
    await waitFor(() => expect(result.current.dropped.length).toBe(1));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("ignores a message whose origin is not 'null' or whose source is not the iframe", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    const { ref, contentWindow } = makeIframeRef();
    renderHook(() => useCanvasBridge(ref));
    act(() => fireMessage(contentWindow, { __robothor: true, kind: "read", reqId: "r3", op: "get_fleet" }, "https://evil.example"));
    act(() => fireMessage({ postMessage() {} }, { __robothor: true, kind: "read", reqId: "r4", op: "get_fleet" }, "null"));
    await new Promise((r) => setTimeout(r, 10));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a propose becomes a pending proposal built from PARENT data, and does NOT execute until confirmed", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    const { ref, contentWindow } = makeIframeRef();
    const { result } = renderHook(() => useCanvasBridge(ref));

    // hostile: label says "Refresh" but the action turns injection scanning off
    act(() => fireMessage(contentWindow, {
      __robothor: true, kind: "propose", reqId: "p1",
      action: "set_flag", args: { name: "ROBOTHOR_INJECTION_SCAN_MODE", value: "off" }, label: "Refresh",
    }));
    await waitFor(() => expect(result.current.pendingProposal).not.toBeNull());
    // the confirm text is the REAL action, not the "Refresh" label
    expect(result.current.pendingProposal!.describe).toBe("Set ROBOTHOR_INJECTION_SCAN_MODE → off");
    expect(result.current.pendingProposal!.describe).not.toMatch(/refresh/i);
    expect(fetchMock).not.toHaveBeenCalled();  // nothing executed yet

    // confirm → the write fires exactly once, PATCH to the resolved path
    await act(async () => { await result.current.confirmProposal(); });
    expect(fetchMock).toHaveBeenCalledWith("/api/bridge/api/controls/ROBOTHOR_INJECTION_SCAN_MODE",
      expect.objectContaining({ method: "PATCH" }));
  });

  it("confirmProposal guards against re-entrant double-submit (fast double-click)", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    const { ref, contentWindow } = makeIframeRef();
    const { result } = renderHook(() => useCanvasBridge(ref));
    act(() => fireMessage(contentWindow, {
      __robothor: true, kind: "propose", reqId: "p3", action: "set_flag",
      args: { name: "ROBOTHOR_INJECTION_SCAN_MODE", value: "off" }, label: "x",
    }));
    await waitFor(() => expect(result.current.pendingProposal).not.toBeNull());

    // Fire two confirms back-to-back before either has settled — simulates a
    // fast double-click racing ahead of the UI's own disabled-button guard.
    await act(async () => {
      await Promise.all([result.current.confirmProposal(), result.current.confirmProposal()]);
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("cancel clears the pending proposal without executing", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    const { ref, contentWindow } = makeIframeRef();
    const { result } = renderHook(() => useCanvasBridge(ref));
    act(() => fireMessage(contentWindow, {
      __robothor: true, kind: "propose", reqId: "p2", action: "set_flag",
      args: { name: "ROBOTHOR_RBAC_MODE", value: "off" }, label: "x",
    }));
    await waitFor(() => expect(result.current.pendingProposal).not.toBeNull());
    act(() => result.current.cancelProposal());
    expect(result.current.pendingProposal).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
