import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CHAT_DOCK_STORAGE_KEY,
  readChatDockOpen,
  writeChatDockOpen,
} from "../dock-state";

/**
 * Whether the docked chat panel is open is remembered per browser, so a
 * collapsed dock stays collapsed across reloads. Open is the default: a
 * browser that has never chosen, or cannot remember, sees the chat.
 */

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("readChatDockOpen", () => {
  it("is open when nothing was stored", () => {
    expect(readChatDockOpen()).toBe(true);
  });

  it("is closed when the browser stored a collapse", () => {
    window.localStorage.setItem(CHAT_DOCK_STORAGE_KEY, "false");
    expect(readChatDockOpen()).toBe(false);
  });

  it("is open for anything that is not a stored collapse", () => {
    window.localStorage.setItem(CHAT_DOCK_STORAGE_KEY, "garbage");
    expect(readChatDockOpen()).toBe(true);
  });

  it("is open when storage cannot be read", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(readChatDockOpen()).toBe(true);
  });
});

describe("writeChatDockOpen", () => {
  it("uses the documented key", () => {
    expect(CHAT_DOCK_STORAGE_KEY).toBe("genus.chatDockOpen");
  });

  it("remembers a collapse and a reopen", () => {
    writeChatDockOpen(false);
    expect(window.localStorage.getItem(CHAT_DOCK_STORAGE_KEY)).toBe("false");
    expect(readChatDockOpen()).toBe(false);
    writeChatDockOpen(true);
    expect(window.localStorage.getItem(CHAT_DOCK_STORAGE_KEY)).toBe("true");
    expect(readChatDockOpen()).toBe(true);
  });

  it("does not throw when storage refuses the write", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(() => writeChatDockOpen(false)).not.toThrow();
  });
});
