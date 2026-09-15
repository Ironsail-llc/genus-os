import { describe, it, expect, afterEach, vi } from "vitest";

import {
  CHAT_AGENT_STORAGE_KEY,
  readStoredChatAgent,
  resolveAgentSessionKey,
  sessionKeyForAgent,
  storeChatAgent,
} from "../agent-session";

/**
 * The one place a chosen agent becomes a session key, and the one place the
 * choice is remembered.
 *
 * Both halves exist for the same reason: the DEFAULT agent must produce no key
 * at all. The engine resolves an empty `session_key` to `main_session_key`,
 * which is the session the operator's webchat and Telegram deliberately share.
 * A browser that sent `agent:main:primary` "because that is what it resolves
 * to" would be right today and wrong the moment an instance sets
 * `ROBOTHOR_DEFAULT_CHAT_AGENT` — and the operator's whole history would be
 * sitting in a session nobody opens any more.
 */

describe("sessionKeyForAgent", () => {
  it("gives a real agent the key the engine parses", () => {
    expect(sessionKeyForAgent("scheduler")).toBe("agent:scheduler:primary");
  });

  it("gives an empty choice NO key, so the engine picks the main session", () => {
    expect(sessionKeyForAgent("")).toBe("");
    expect(sessionKeyForAgent("   ")).toBe("");
  });

  it("refuses only what breaks the key's SHAPE, not what looks unusual", () => {
    // `agent:a:primary:b:primary` is not the shape `_effective_session_key`
    // parses, and neither is a key with a newline in the middle of it.
    expect(sessionKeyForAgent("a:primary:b")).toBe("");
    expect(sessionKeyForAgent("has space")).toBe("");
    expect(sessionKeyForAgent("two\nlines")).toBe("");
    expect(sessionKeyForAgent("x".repeat(129))).toBe("");
  });

  it("keys an id the appliance itself would list, however it is spelled", () => {
    // Nothing validates manifest ids on the READ path, so `acme.bot` is a real
    // listable agent. Refusing it here returned `""` — which means "the main
    // session" — so a private message to a worker was appended to the
    // operator's shared history. Failing closed is a refusal; this used to fail
    // open, in the one direction that MERGES two conversations.
    expect(sessionKeyForAgent("acme.bot")).toBe("agent:acme.bot:primary");
    expect(sessionKeyForAgent("team/ops")).toBe("agent:team/ops:primary");
    expect(sessionKeyForAgent("_internal")).toBe("agent:_internal:primary");
  });

  it("tells an absent choice apart from an unusable one", () => {
    // The whole reason `resolveAgentSessionKey` exists: both produce `key: ""`,
    // and only one of them may be sent as "no key".
    expect(resolveAgentSessionKey(undefined)).toEqual({ chosen: false, key: "" });
    expect(resolveAgentSessionKey("")).toEqual({ chosen: false, key: "" });
    expect(resolveAgentSessionKey("   ")).toEqual({ chosen: false, key: "" });
    expect(resolveAgentSessionKey("a:primary:b")).toEqual({ chosen: true, key: "" });
    expect(resolveAgentSessionKey("scheduler")).toEqual({
      chosen: true,
      key: "agent:scheduler:primary",
    });
  });

  it("refuses anything that is not a string", () => {
    expect(sessionKeyForAgent(undefined)).toBe("");
    expect(sessionKeyForAgent(null)).toBe("");
    expect(sessionKeyForAgent(42)).toBe("");
  });
});

describe("the remembered agent", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    try {
      window.localStorage.clear();
    } catch {
      // Nothing to clear if the stub took it away.
    }
  });

  it("round-trips through localStorage", () => {
    storeChatAgent("scheduler");

    expect(window.localStorage.getItem(CHAT_AGENT_STORAGE_KEY)).toBe("scheduler");
    expect(readStoredChatAgent()).toBe("scheduler");
  });

  it("forgets the choice when it is cleared back to the default", () => {
    storeChatAgent("scheduler");
    storeChatAgent("");

    expect(window.localStorage.getItem(CHAT_AGENT_STORAGE_KEY)).toBeNull();
    expect(readStoredChatAgent()).toBe("");
  });

  it("never returns a stored value that is not a usable agent id", () => {
    window.localStorage.setItem(CHAT_AGENT_STORAGE_KEY, "a:primary:b");

    expect(readStoredChatAgent()).toBe("");
  });

  it("reads as the default when storage throws", () => {
    // A private window, blocked site data, a browser that throws on access —
    // every one of them must leave the chat working, on the main session.
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
      removeItem: () => {
        throw new Error("blocked");
      },
    });

    expect(readStoredChatAgent()).toBe("");
    expect(() => storeChatAgent("scheduler")).not.toThrow();
  });
});
