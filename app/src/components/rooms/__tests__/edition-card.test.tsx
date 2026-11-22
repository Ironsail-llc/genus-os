/**
 * An edition draws its blocks through the dashboard's own component registry
 * and renderer. A block the dashboard cannot draw becomes a visible
 * placeholder; a block that throws is contained. Neither takes the edition,
 * the room or the chat down with it.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/business/bar-chart", () => ({
  BarChart: ({ title }: { title: string }) => <div data-testid="bar-chart">{title}</div>,
}));
vi.mock("@/components/business/line-chart", () => ({
  LineChart: () => {
    throw new Error("bad data");
  },
}));
vi.mock("@/components/business/markdown-view", () => ({
  MarkdownView: ({ content }: { content: string }) => <div data-testid="markdown-view">{content}</div>,
}));
vi.mock("@/lib/dashboard/error-reporter", () => ({ reportDashboardError: vi.fn() }));

import { EditionCard } from "@/components/rooms/edition-card";
import { edition } from "@/lib/rooms/__tests__/fixtures";

describe("EditionCard", () => {
  it("shows the title, the period and the summary", () => {
    render(<EditionCard edition={edition()} />);
    expect(screen.getByTestId("edition-title")).toHaveTextContent("Week 47");
    expect(screen.getByTestId("edition-period")).toHaveTextContent(/16 Nov.*22 Nov 2026/);
    expect(screen.getByTestId("edition-summary")).toHaveTextContent("Revenue up on the week.");
  });

  it("draws each block through the component registry", async () => {
    render(<EditionCard edition={edition()} />);
    await waitFor(() => expect(screen.getByTestId("bar-chart")).toHaveTextContent("Revenue by day"));
    expect(await screen.findByTestId("markdown-view")).toHaveTextContent("Strong finish.");
  });

  it("puts a visible placeholder where a block cannot be drawn", async () => {
    render(
      <EditionCard
        edition={edition({
          blocks: [
            { component: "render_not_a_thing", props: {} },
            { component: "render_form", props: {} },
            "garbage" as never,
            { component: "render_markdown", props: { content: "still here" } },
          ],
        })}
      />,
    );
    const placeholders = screen.getAllByTestId("edition-block-unsupported");
    expect(placeholders).toHaveLength(3);
    expect(placeholders[0]).toHaveTextContent(/unsupported block/i);
    expect(await screen.findByTestId("markdown-view")).toHaveTextContent("still here");
  });

  it("contains a block that throws while drawing", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <EditionCard
        edition={edition({
          blocks: [
            { component: "render_line_chart", props: {} },
            { component: "render_markdown", props: { content: "after" } },
          ],
        })}
      />,
    );
    expect(await screen.findByTestId("component-error")).toBeInTheDocument();
    expect(await screen.findByTestId("markdown-view")).toHaveTextContent("after");
  });

  it("survives an edition with no blocks, no summary and no period", () => {
    render(
      <EditionCard
        edition={edition({ blocks: undefined as never, summary: "", periodStart: null as never, periodEnd: null as never })}
      />,
    );
    expect(screen.getByTestId("edition-title")).toHaveTextContent("Week 47");
    expect(screen.queryByTestId("edition-summary")).toBeNull();
    expect(screen.queryByTestId("edition-period")).toBeNull();
  });
});
