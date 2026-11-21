/**
 * A room's history carries who wrote each message and which edition a message
 * announced, so attribution and charts survive a reload. Render markers are
 * still stripped: the edition id, not a marker, brings the charts back.
 */
import { describe, expect, it } from "vitest";

import { historyToMessages } from "@/lib/chat/history-messages";

const EDITION = "aaaaaaaa-0000-4000-8000-000000000002";

describe("historyToMessages in a room", () => {
  it("keeps the author and the edition on each message", () => {
    const messages = historyToMessages([
      { id: 10, role: "user", content: "How did we do?", authorUserId: "u-bob", authorName: "Bob" },
      {
        id: 11,
        role: "assistant",
        content: 'Week 47 is out. [RENDER:{"component":"render_bar_chart","props":{"data":[1]}}]',
        authorName: "Analyst",
        editionId: EDITION,
      },
    ]);
    expect(messages[0]).toMatchObject({ authorUserId: "u-bob", authorName: "Bob" });
    expect(messages[0].editionId).toBeUndefined();
    expect(messages[1]).toMatchObject({ authorName: "Analyst", editionId: EDITION, content: "Week 47 is out." });
  });

  it("accepts the engine's snake_case spelling too", () => {
    const [message] = historyToMessages([
      { id: 12, role: "user", content: "hi", author_user_id: "u-alice", author_name: "Alice", edition_id: EDITION },
    ]);
    expect(message).toMatchObject({ authorUserId: "u-alice", authorName: "Alice", editionId: EDITION });
  });

  it("adds no fields to a main-conversation row", () => {
    const [message] = historyToMessages([{ id: 1, role: "user", content: "hello" }]);
    expect(message).toEqual({ id: "hist-1", role: "user", content: "hello", timestamp: undefined, serverId: 1 });
  });

  it("ignores author and edition values that are not strings", () => {
    const [message] = historyToMessages([
      { id: 2, role: "assistant", content: "x", authorName: 7, editionId: { id: EDITION } },
    ]);
    expect(message.authorName).toBeUndefined();
    expect(message.editionId).toBeUndefined();
  });
});
