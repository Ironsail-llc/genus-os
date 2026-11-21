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
