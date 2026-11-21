/**
 * A workspace email (E3): the exact email an approval authorizes, and the
 * receipt turn that says what happened to it.
 */
import { describe, expect, it } from "vitest";

import { historyToMessages } from "@/lib/chat/history-messages";
import { normalizePending } from "@/lib/inbox/pending";
import { readEmail, readEmailReceipt } from "@/lib/rooms/email";

const EMAIL = {
  from: "assistant@example.com",
  replyTo: "bob@example.com",
  to: ["carol.client@example.org", "dave.client@example.org"],
  cc: ["erin.client@example.org"],
  subject: "Checking in",
  body: "Hello,\n\nJust checking in.",
  onBehalfOf: "Bob",
};

const RECEIPT = {
  action_id: "a-1",
  status: "sent",
  from: "assistant@example.com",
  reply_to: "bob@example.com",
  to: ["carol.client@example.org"],
  cc: [],
  subject: "Checking in",
  approved_by: { user_id: "u-bob", display_name: "Bob" },
  decided_by: { user_id: "u-bob", display_name: "Bob" },
  decided_at: "2026-11-21T10:00:00+00:00",
  sent_at: "2026-11-21T10:00:02+00:00",
  message_id: "msg-1",
  error: null,
  room_id: "r-1",
  task_id: "t-1",
};

describe("readEmail", () => {
  it("reads every field of the exact email", () => {
    expect(readEmail(EMAIL)).toEqual(EMAIL);
  });

  it("is null for anything that is not an email", () => {
    expect(readEmail(null)).toBeNull();
    expect(readEmail({ subject: "x" })).toBeNull();
  });
});

describe("the approvals list", () => {
  it("keeps a workspace email's exact fields on its row", () => {
    const [row] = normalizePending({
      count: 1,
      scope: "own",
      pending: [{ kind: "action", id: "a-1", action_kind: "room.email", question: "room.email: Checking in", detail: "", email: EMAIL, room_id: "r-1", task_id: "t-1" }],
    });
    expect(row.email).toEqual(EMAIL);
  });
});

describe("readEmailReceipt", () => {
  it("reads a sent receipt", () => {
    expect(readEmailReceipt(RECEIPT)).toMatchObject({ status: "sent", to: ["carol.client@example.org"], subject: "Checking in", decidedBy: "Bob", error: null });
  });

  it("reads every outcome, and refuses an unknown one", () => {
    for (const status of ["sent", "failed", "uncertain", "not_sent", "rejected", "expired"]) {
      expect(readEmailReceipt({ ...RECEIPT, status })?.status).toBe(status);
    }
    expect(readEmailReceipt({ ...RECEIPT, status: "maybe" })).toBeNull();
  });
});

describe("history in a workspace", () => {
  it("passes an email receipt turn's kind and receipt through", () => {
    const [m] = historyToMessages([
      { id: 7, role: "assistant", content: "Sent to carol.client@example.org.", kind: "room_email_receipt", receipt: RECEIPT },
    ]);
    expect(m.receipt).toMatchObject({ status: "sent", subject: "Checking in" });
  });

  it("ignores a receipt on any other turn", () => {
    const [m] = historyToMessages([{ id: 8, role: "assistant", content: "hi", receipt: RECEIPT }]);
    expect(m.receipt).toBeUndefined();
  });
});
