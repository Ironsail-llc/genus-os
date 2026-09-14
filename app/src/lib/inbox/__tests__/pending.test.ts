import { describe, it, expect } from "vitest";

import {
  absoluteTime,
  kindLabel,
  normalizePending,
  readAnswerMessage,
  relativeTime,
  shortRunId,
  whoRaised,
  type PendingItem,
} from "../pending";

const NOW = Date.parse("2026-06-15T12:00:00Z");

function item(overrides: Partial<PendingItem> = {}): PendingItem {
  return {
    kind: "question",
    id: "11111111-1111-4111-8111-111111111111",
    run_id: "abcdef12-3456-4789-8abc-def012345678",
    agent_id: "invoice-chaser",
    question: "Which vendor should I chase first?",
    detail: "",
    options: [],
    expires_at: "2026-06-15T13:00:00Z",
    created_at: "2026-06-15T11:30:00Z",
    ...overrides,
  };
}

describe("kindLabel", () => {
  it("names the two durable kinds the way the operator reads them", () => {
    expect(kindLabel("workflow")).toBe("Approval");
    expect(kindLabel("question")).toBe("Question");
  });
});

describe("whoRaised", () => {
  it("names the agent that asked", () => {
    expect(whoRaised(item({ agent_id: "invoice-chaser" }))).toBe("invoice-chaser");
  });

  it("says workflow when no agent owns the row", () => {
    // The approvals route sends `agent_id: ""` for every workflow item.
    expect(whoRaised(item({ kind: "workflow", agent_id: "" }))).toBe("workflow");
  });
});

describe("shortRunId", () => {
  it("is the first eight characters, which is what the Runs view shows", () => {
    expect(shortRunId("abcdef12-3456-4789-8abc-def012345678")).toBe("abcdef12");
  });

  it("is empty for a missing run", () => {
    expect(shortRunId("")).toBe("");
  });
});

describe("relativeTime", () => {
  it("reads a past instant as an age", () => {
    expect(relativeTime("2026-06-15T11:30:00Z", NOW)).toBe("30 minutes ago");
    expect(relativeTime("2026-06-15T09:00:00Z", NOW)).toBe("3 hours ago");
    expect(relativeTime("2026-06-13T12:00:00Z", NOW)).toBe("2 days ago");
  });

  it("reads a future instant as a deadline", () => {
    expect(relativeTime("2026-06-15T13:00:00Z", NOW)).toBe("in 1 hour");
    expect(relativeTime("2026-06-16T12:00:00Z", NOW)).toBe("in 1 day");
  });

  it("does not put a number on the last minute either way", () => {
    expect(relativeTime("2026-06-15T11:59:40Z", NOW)).toBe("just now");
    expect(relativeTime("2026-06-15T12:00:20Z", NOW)).toBe("in under a minute");
  });

  it("says nothing at all when there is no instant", () => {
    // `expires_at` is nullable on both tables behind the route.
    expect(relativeTime(null, NOW)).toBe("");
    expect(relativeTime("not a date", NOW)).toBe("");
  });
});

describe("absoluteTime", () => {
  it("spells the instant out for the hover title", () => {
    expect(absoluteTime("2026-06-15T13:00:00Z")).toContain("2026");
  });

  it("is empty when there is no instant, so no empty tooltip is offered", () => {
    expect(absoluteTime(null)).toBe("");
    expect(absoluteTime("not a date")).toBe("");
  });
});

describe("normalizePending", () => {
  it("reads the route's answer", () => {
    const rows = normalizePending({
      count: 1,
      pending: [
        {
          kind: "workflow",
          id: "22222222-2222-4222-8222-222222222222",
          run_id: "r1",
          agent_id: "",
          question: "Send the quote to Alice?",
          detail: "The draft is attached to the run.",
          options: [],
          expires_at: "2026-06-15T13:00:00Z",
          created_at: "2026-06-15T11:00:00Z",
        },
      ],
    });

    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("workflow");
    expect(rows[0].question).toBe("Send the quote to Alice?");
  });

  it("drops rows of a kind this screen must never answer", () => {
    // `escalation` is settled in the engine's RAM, not here. A card offering
    // to answer one would leave the agent waiting behind a green tick.
    const rows = normalizePending({
      pending: [
        { kind: "escalation", id: "deadbeef", question: "Run a destructive command?" },
        { kind: "question", id: "33333333-3333-4333-8333-333333333333", question: "Which one?" },
      ],
    });

    expect(rows.map((row) => row.kind)).toEqual(["question"]);
  });

  it("survives a body that is not the shape it promised", () => {
    expect(normalizePending(null)).toEqual([]);
    expect(normalizePending({ pending: "nope" })).toEqual([]);
    expect(normalizePending({ pending: [{ kind: "question" }] })).toEqual([]);
  });

  it("fills the optional fields rather than rendering undefined", () => {
    const [full] = normalizePending({
      pending: [
        { kind: "question", id: "44444444-4444-4444-8444-444444444444", question: "Well?" },
      ],
    });
    expect(full.detail).toBe("");
    expect(full.options).toEqual([]);
    expect(full.agent_id).toBe("");
    expect(full.expires_at).toBeNull();
  });
});

describe("readAnswerMessage", () => {
  function res(body: unknown, status = 400): Response {
    return {
      status,
      json: () => Promise.resolve(body),
    } as unknown as Response;
  }

  it("prefers the route's own sentence", async () => {
    // `_refuse` answers with `message`, not `detail`.
    expect(await readAnswerMessage(res({ settled: false, message: "malformed id" }))).toBe(
      "malformed id"
    );
  });

  it("falls back to a FastAPI detail", async () => {
    expect(await readAnswerMessage(res({ detail: "answer is required" }))).toBe(
      "answer is required"
    );
  });

  it("reads a validation detail list", async () => {
    expect(await readAnswerMessage(res({ detail: [{ msg: "field required" }] }, 422))).toBe(
      "field required"
    );
  });

  it("names the status when the body says nothing", async () => {
    const message = await readAnswerMessage({
      status: 503,
      json: () => Promise.reject(new Error("not json")),
    } as unknown as Response);
    expect(message).toContain("503");
  });
});
