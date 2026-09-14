import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ComingSoonView } from "../coming-soon-view";

describe("ComingSoonView", () => {
  it("names the view it stands in for and says it is not built yet", () => {
    render(<ComingSoonView view="inbox" visible />);
    const el = screen.getByTestId("coming-soon-inbox");
    expect(el.textContent).toMatch(/Inbox/);
    expect(el.textContent).toMatch(/soon/i);
  });

  it("renders nothing when not visible", () => {
    const { container } = render(<ComingSoonView view="memory" visible={false} />);
    expect(container.firstChild).toBeNull();
  });
});
