import { describe, it, expect } from "vitest";
import { historyToMessages, oldestServerId } from "../history-messages";

describe("historyToMessages", () => {
  it("keeps the time each message was stored, and its server id", () => {
    const [msg] = historyToMessages([
      { role: "user", content: "hi", id: 7, timestamp: "2026-10-31T09:00:00+00:00" },
    ]);
    expect(msg.id).toBe("hist-7");
    expect(msg.serverId).toBe(7);
    expect(msg.timestamp?.toISOString()).toBe("2026-10-31T09:00:00.000Z");
  });

  it("invents no time when the history carries none", () => {
    const [msg] = historyToMessages([{ role: "assistant", content: "hello" }]);
    expect(msg.timestamp).toBeUndefined();
    expect(msg.id).toBe("hist-0");
  });

  it("drops an unparseable time instead of showing Invalid Date", () => {
    const [msg] = historyToMessages([{ role: "user", content: "x", timestamp: "yesterday-ish" }]);
    expect(msg.timestamp).toBeUndefined();
  });

  it("keeps only user and assistant rows, and joins text blocks", () => {
    const msgs = historyToMessages([
      { role: "system", content: "canvas prompt" },
      { role: "user", content: [{ type: "text", text: "a" }, { type: "image" }, { type: "text", text: "b" }] },
      { role: "tool", content: "result" },
    ]);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toBe("ab");
  });

  it("strips render markers the way live messages are stripped", () => {
    const [msg] = historyToMessages([
      { role: "assistant", content: 'Done. [RENDER:dashboard:{"a":[1]}]' },
    ]);
    expect(msg.content).toBe("Done.");
  });

  it("keeps a user turn's files and drops the engine's note about them", () => {
    const [msg] = historyToMessages([
      {
        role: "user",
        content: "see this\n\n[1 file attached — kept on disk; use the paths below]\n- /w/inbox/a.png",
        extras: { attachments: [{ original_name: "a.png", size: 12, kind: "image" }] },
        id: 4,
      },
    ]);
    expect(msg.content).toBe("see this");
    expect(msg.attachments).toEqual([{ name: "a.png", size: 12, kind: "image" }]);
  });

  it("tolerates a payload that is not a list", () => {
    expect(historyToMessages(undefined)).toEqual([]);
    expect(historyToMessages({} as unknown)).toEqual([]);
  });
});

describe("oldestServerId", () => {
  it("is the smallest server id on screen, ignoring live messages", () => {
    expect(
      oldestServerId([{ serverId: 12 }, { serverId: undefined }, { serverId: 9 }, { serverId: 30 }]),
    ).toBe(9);
  });

  it("is undefined when nothing came from the server", () => {
    expect(oldestServerId([{ serverId: undefined }])).toBeUndefined();
  });
});
