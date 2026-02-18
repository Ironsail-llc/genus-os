import { describe, it, expect } from "vitest";
import { extractText } from "@/lib/gateway/types";
import type { ChatMessage } from "@/lib/gateway/types";

describe("extractText", () => {
  it("extracts text from string content", () => {
    const msg: ChatMessage = { role: "assistant", content: "Hello world" };
    expect(extractText(msg)).toBe("Hello world");
  });

  it("extracts text from content blocks", () => {
    const msg: ChatMessage = {
      role: "assistant",
      content: [
        { type: "text", text: "Hello " },
        { type: "text", text: "world" },
      ],
    };
    expect(extractText(msg)).toBe("Hello world");
  });

  it("ignores non-text blocks", () => {
    const msg: ChatMessage = {
      role: "assistant",
      content: [
        { type: "text", text: "Hello" },
        { type: "tool_use", id: "t1", name: "test", input: {} },
        { type: "text", text: " world" },
      ],
    };
    expect(extractText(msg)).toBe("Hello world");
  });

  it("handles empty content array", () => {
    const msg: ChatMessage = { role: "assistant", content: [] };
    expect(extractText(msg)).toBe("");
  });

  it("handles empty string content", () => {
    const msg: ChatMessage = { role: "assistant", content: "" };
    expect(extractText(msg)).toBe("");
  });
});
