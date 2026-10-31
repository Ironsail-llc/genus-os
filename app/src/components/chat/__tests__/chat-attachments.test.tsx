import { describe, it, expect, vi, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useChatAttachments } from "../chat-attachments";

/** A FileReader that finishes only when the test says so. */
function slowReader() {
  const pending: Array<() => void> = [];
  class SlowReader {
    result: string | null = null;
    error: unknown = null;
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    readAsDataURL() {
      pending.push(() => {
        this.result = "data:text/plain;base64,aGk=";
        this.onload?.();
      });
    }
  }
  vi.stubGlobal("FileReader", SlowReader);
  return { finish: () => pending.splice(0).forEach((f) => f()) };
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("useChatAttachments and a read still in flight", () => {
  it.each(["clear", "drop"] as const)("a %s while a file is being read keeps it out", async (how) => {
    const reader = slowReader();
    const { result } = renderHook(() => useChatAttachments());
    let adding!: Promise<void>;
    act(() => { adding = result.current.add([new File(["hi"], "a.txt", { type: "text/plain" })]); });
    act(() => { if (how === "clear") result.current.clear(); else result.current.drop("Plan mode."); });
    await act(async () => { reader.finish(); await adding; });
    expect(result.current.files).toEqual([]);
  });

  it("a read that finishes with nothing in between is kept", async () => {
    const reader = slowReader();
    const { result } = renderHook(() => useChatAttachments());
    let adding!: Promise<void>;
    act(() => { adding = result.current.add([new File(["hi"], "a.txt", { type: "text/plain" })]); });
    await act(async () => { reader.finish(); await adding; });
    expect(result.current.files.map((f) => f.name)).toEqual(["a.txt"]);
  });
});
