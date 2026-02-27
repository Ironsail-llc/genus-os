import { describe, it, expect } from "vitest";
import type { ChatMessage } from "@/lib/gateway/types";

describe("ChatMessage", () => {
  it("has role and content fields", () => {
    const msg: ChatMessage = { role: "assistant", content: "Hello world" };
    expect(msg.role).toBe("assistant");
    expect(msg.content).toBe("Hello world");
  });

  it("accepts user role", () => {
    const msg: ChatMessage = { role: "user", content: "Hi" };
    expect(msg.role).toBe("user");
  });

  it("accepts system role", () => {
    const msg: ChatMessage = { role: "system", content: "You are helpful" };
    expect(msg.role).toBe("system");
  });

  it("handles empty string content", () => {
    const msg: ChatMessage = { role: "assistant", content: "" };
    expect(msg.content).toBe("");
  });
});
