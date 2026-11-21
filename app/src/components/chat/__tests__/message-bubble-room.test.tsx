/**
 * In a room a message says who wrote it, and a message that announced an
 * edition draws that edition again (fetched by id), so its charts are there
 * after a reload even though the history strips render markers.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/business/bar-chart", () => ({
  BarChart: ({ title }: { title: string }) => <div data-testid="bar-chart">{title}</div>,
}));
vi.mock("@/components/business/markdown-view", () => ({
  MarkdownView: ({ content }: { content: string }) => <div data-testid="markdown-view">{content}</div>,
}));

import { MessageBubble } from "@/components/chat/message-bubble";
import { EDITION_2, REPORT_ID, edition, reply } from "@/lib/rooms/__tests__/fixtures";

const noop = vi.fn();

afterEach(() => vi.unstubAllGlobals());

describe("MessageBubble in a room", () => {
  it("names the author of each message", () => {
    render(
      <>
        <MessageBubble roomId={REPORT_ID} msg={{ id: "a", role: "user", content: "hi", authorName: "Bob" }} onRecovered={noop} />
        <MessageBubble roomId={REPORT_ID} msg={{ id: "b", role: "assistant", content: "hello", authorName: "Analyst" }} onRecovered={noop} />
        <MessageBubble roomId={REPORT_ID} msg={{ id: "c", role: "user", content: "mine" }} onRecovered={noop} />
      </>,
    );
    const authors = screen.getAllByTestId("message-author").map((el) => el.textContent);
    expect(authors).toEqual(["Bob", "Analyst", "You"]);
  });

  it("names nobody in the main conversation", () => {
    render(<MessageBubble msg={{ id: "a", role: "user", content: "hi", authorName: "Bob" }} onRecovered={noop} />);
    expect(screen.queryByTestId("message-author")).toBeNull();
  });

  it("draws the edition a message announced, fetched by id", async () => {
    const fetchMock = vi.fn(async () => reply(edition()));
    vi.stubGlobal("fetch", fetchMock);
    render(
      <MessageBubble
        roomId={REPORT_ID}
        msg={{ id: "e", role: "assistant", content: "Week 47 is out.", editionId: EDITION_2, authorName: "Analyst" }}
        onRecovered={noop}
      />,
    );
    expect(await screen.findByTestId("bar-chart")).toHaveTextContent("Revenue by day");
    expect(fetchMock).toHaveBeenCalledWith(`/api/bridge/api/rooms/${REPORT_ID}/editions/${EDITION_2}`, expect.anything());
    expect(screen.getByTestId("message-edition")).toBeInTheDocument();
  });

  it("says so, calmly, when the edition cannot be read", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => reply({ error: "gone" }, 404)));
    render(
      <MessageBubble roomId={REPORT_ID} msg={{ id: "e", role: "assistant", content: "x", editionId: EDITION_2 }} onRecovered={noop} />,
    );
    await waitFor(() => expect(screen.getByTestId("message-edition-unavailable")).toBeInTheDocument());
  });

  it("fetches nothing for a message with no edition", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<MessageBubble roomId={REPORT_ID} msg={{ id: "a", role: "assistant", content: "hi" }} onRecovered={noop} />);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId("message-edition")).toBeNull();
  });
});

describe("MessageBubble: a workspace email receipt (E3)", () => {
  const base = {
    action_id: "a-1",
    from: "assistant@example.com",
    reply_to: "bob@example.com",
    to: ["carol.client@example.org", "dave.client@example.org"],
    cc: [],
    subject: "Checking in",
    approved_by: { user_id: "u-bob", display_name: "Bob" },
    decided_by: { user_id: "u-bob", display_name: "Bob" },
    decided_at: "2026-11-21T10:00:00+00:00",
    sent_at: "2026-11-21T10:00:02+00:00",
    message_id: "msg-1",
    error: null,
    room_id: REPORT_ID,
    task_id: "t-1",
  };

  function receiptMsg(overrides: Record<string, unknown>) {
    return { id: "r", role: "assistant" as const, content: "Receipt.", receipt: { ...base, ...overrides } };
  }

  it("says plainly that it was sent, to whom and with what subject", () => {
    render(<MessageBubble roomId={REPORT_ID} msg={receiptMsg({ status: "sent" })} onRecovered={noop} />);
    const receipt = screen.getByTestId("room-email-receipt");
    expect(receipt).toHaveTextContent(/Sent/);
    expect(receipt).toHaveTextContent("carol.client@example.org, dave.client@example.org");
    expect(receipt).toHaveTextContent("Checking in");
    expect(receipt).toHaveTextContent(/Bob/);
  });

  it("says a failed send failed, with the reason", () => {
    render(<MessageBubble roomId={REPORT_ID} msg={receiptMsg({ status: "failed", error: "the mailbox refused it", sent_at: null })} onRecovered={noop} />);
    const receipt = screen.getByTestId("room-email-receipt");
    expect(receipt).toHaveTextContent(/Not sent: it failed/);
    expect(receipt).toHaveTextContent("the mailbox refused it");
  });

  it("says a rejected or unconfirmed email for what it is", () => {
    render(<MessageBubble roomId={REPORT_ID} msg={receiptMsg({ status: "rejected", sent_at: null })} onRecovered={noop} />);
    expect(screen.getByTestId("room-email-receipt")).toHaveTextContent(/Not sent: rejected/);
  });

  it("says an unconfirmed send may have gone out", () => {
    render(<MessageBubble roomId={REPORT_ID} msg={receiptMsg({ status: "uncertain", sent_at: null })} onRecovered={noop} />);
    expect(screen.getByTestId("room-email-receipt")).toHaveTextContent(/may have been sent/i);
  });
});
