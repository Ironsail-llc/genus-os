/**
 * Where `EventSource` does not exist (server rendering, an old browser, jsdom),
 * subscribing to the event stream does nothing: no connection, and no error
 * thrown into the page, which a test run counts as a failure.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { subscribe } from "@/lib/event-bus/event-stream-hub";

function collectUncaught() {
  const errors: unknown[] = [];
  const onProcess = (err: unknown) => errors.push(err);
  const onWindow = (event: ErrorEvent) => {
    errors.push(event.error ?? event.message);
    event.preventDefault();
  };
  process.on("uncaughtException", onProcess);
  window.addEventListener("error", onWindow);
  return {
    errors,
    stop() {
      process.off("uncaughtException", onProcess);
      window.removeEventListener("error", onWindow);
    },
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("the event stream hub without EventSource", () => {
  it("subscribes and unsubscribes without throwing, and opens nothing", async () => {
    vi.stubGlobal("EventSource", undefined);
    const caught = collectUncaught();
    const sub = { streams: ["agent"], onMessage: vi.fn(), onOpen: vi.fn(), onClose: vi.fn(), onSync: vi.fn() };
    let unsubscribe: () => void = () => {};
    expect(() => (unsubscribe = subscribe(sub))).not.toThrow();
    await new Promise((r) => setTimeout(r, 20));
    unsubscribe();
    caught.stop();
    expect(caught.errors).toEqual([]);
    expect(sub.onOpen).not.toHaveBeenCalled();
  });
});
