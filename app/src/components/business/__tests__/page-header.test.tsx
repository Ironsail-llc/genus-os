import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { PageHeader } from "../page-header";

describe("PageHeader", () => {
  it("renders the title as a heading", () => {
    render(<PageHeader title="Fleet" />);
    expect(screen.getByRole("heading", { name: "Fleet" })).toBeTruthy();
  });

  it("renders a description when provided", () => {
    render(<PageHeader title="Fleet" description="14 agents · 2 flagged" />);
    expect(screen.getByTestId("page-header").textContent).toContain("14 agents · 2 flagged");
  });

  it("renders an action slot", () => {
    render(
      <PageHeader title="Fleet">
        <button>Refresh</button>
      </PageHeader>
    );
    expect(screen.getByRole("button", { name: "Refresh" })).toBeTruthy();
  });
});
