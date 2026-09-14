import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ComingSoonView } from "../coming-soon-view";

describe("ComingSoonView", () => {
  it("names the view it stands in for and says it is not built yet", () => {
    render(<ComingSoonView view="logs" />);
    const el = screen.getByTestId("coming-soon-logs");
    expect(el.textContent).toMatch(/Logs/);
    expect(el.textContent).toMatch(/soon/i);
  });

  it("describes each not-yet-built screen it stands in for", () => {
    render(<ComingSoonView view="memory" />);
    expect(screen.getByTestId("coming-soon-memory").textContent).toMatch(/Memory/);
  });
});
